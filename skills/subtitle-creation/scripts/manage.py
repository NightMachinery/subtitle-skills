#!/usr/bin/env python3
"""Offline inventory and reviewer handoff for the reviewed priority-wave queue."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import tempfile

import batch as b
import transcribe as t

PUBLIC_ROOT = Path(__file__).resolve().parents[3]
MEDIA = set('mp4 mkv mov avi webm m4v mpg mpeg ts mts m2ts wmv flv ogv wav mp3 m4a aac flac ogg opus aiff aif wma'.split())
EPISODE = r'^(?:\d{4,}[-_ .]+)?(?:(?:episode|lecture|lesson|ep)[-_ .]*)?(?P<episode>\d+)(?:\D|$)'


def private(path):
    path = Path(path).resolve()
    if path == PUBLIC_ROOT or PUBLIC_ROOT in path.parents:
        raise t.SubtitleError('Private artifacts must stay outside this public skill repository')
    return path


def publish(path, value):
    """Atomic no-clobber publication; identical JSON is safe to reuse."""
    path = private(path)
    if path.exists():
        if t.read_json(path) == value:
            return path
        raise t.SubtitleError('Existing private artifact differs; use a deliberately new state/work directory')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            if t.read_json(path) != value:
                raise t.SubtitleError('Private artifact appeared with different contents') from None
    finally:
        os.unlink(name)
    return path


def outputs(source):
    prefix = source.stem + '.'
    return {str(private(path)): b.digest(path) for path in sorted(source.parent.iterdir())
            if path.is_file() and path.name.startswith(prefix) and path.suffix == '.srt'}


def token(text):
    slug = re.sub(r'[^A-Za-z0-9_.-]+', '-', text).strip('.-')[:70] or 'series'
    return slug + '-' + b.canonical(text)[:10]


def plan(args):
    root, state = Path(args.root).resolve(), private(args.state_dir)
    if not root.is_dir() or args.wave_size < 1:
        raise t.SubtitleError('Plan requires an existing media directory and positive wave size')
    pattern = re.compile(args.episode_pattern, re.I)
    if 'episode' not in pattern.groupindex:
        raise t.SubtitleError('Episode pattern must have a named (?P<episode>...) integer group')
    inventory, errors, eligible = [], [], []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.lower() not in {
            '__pycache__', 'node_modules', 'cache', 'caches'} and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            if name.startswith('.') or Path(name).suffix.lower().lstrip('.') not in MEDIA:
                continue
            candidate = Path(folder) / name
            source = candidate.resolve()
            item = {'discovered': str(candidate), 'source': str(source)}
            inventory.append(item)
            try:
                private(source)
            except t.SubtitleError:
                item.update(status='error', reason='media_inside_public_skill_repository')
                errors.append('Media sources must stay outside the public skill repository')
                continue
            if not source.is_relative_to(root):
                item.update(status='skipped', reason='symlink_outside_root')
                continue
            try:
                duration = t.probe(source)
                item.update(duration_seconds=duration, metadata=t.read_metadata(source), existing_outputs=outputs(source),
                            source_identity=b.fingerprint(source))
            except t.SubtitleError as error:
                if str(error) == 'Input media has no audio stream':
                    item.update(status='skipped', reason='no_audio')
                else:
                    item.update(status='error', reason=str(error))
                    errors.append('Unusable media in inventory')
                continue
            matches = list(pattern.finditer(source.stem))
            try:
                if len(matches) != 1:
                    raise ValueError()
                episode = int(matches[0]['episode'])
                if episode < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                item.update(status='error', reason='unparsed_or_ambiguous_episode')
                errors.append('Unparsed or ambiguous episode in inventory')
                continue
            group = token(str(source.parent.relative_to(root)))
            item.update(status='eligible', episode=episode, group=group)
            eligible.append(item)
    owners, stems, sources = set(), set(), set()
    for item in eligible:
        key, stem = (item['group'], item['episode']), str(Path(item['source']).with_suffix(''))
        if key in owners or stem in stems or item['source'] in sources:
            item.update(status='error', reason='duplicate_episode_or_output_ownership')
            errors.append('Duplicate ownership in inventory')
        owners.add(key)
        stems.add(stem)
        sources.add(item['source'])
    evidence = {'version': 1, 'root': str(root), 'wave_size': args.wave_size,
                'episode_pattern': args.episode_pattern, 'media': inventory, 'errors': sorted(set(errors))}
    manifest_path = state / 'manifest.json'
    # Never add evidence to an active/unrelated queue during a failed re-plan.
    if errors or not eligible:
        if manifest_path.exists() or (state / 'queue-state.json').exists():
            raise t.SubtitleError('Existing plan is protected; inspect in a new private state directory')
        publish(state / 'inventory.json', evidence)
        raise t.SubtitleError('Inventory needs resolution before planning; inspect private inventory.json')
    last = max(((item['episode'] - 1) // args.wave_size + 1 for item in eligible if item['episode']), default=0)
    jobs = [{'id': token(item['source']), 'wave': (item['episode'] - 1) // args.wave_size + 1 if item['episode'] else last + 1,
             'group': item['group'], 'source': item['source'], 'accepted_existing': False}
            for item in sorted(eligible, key=lambda item: (item['episode'] == 0, item['episode'], item['group']))]
    manifest = {'version': 1, 'jobs': sorted(jobs, key=lambda job: (job['wave'], job['group']))}
    # Validate via the queue contract on an isolated private file.
    with tempfile.TemporaryDirectory(prefix='subtitle-plan-') as directory:
        check = Path(directory) / 'manifest.json'
        t.atomic_json(check, manifest)
        manifest, identity = b.manifest_for(check)
    if (state / 'queue-state.json').exists() and b.load(state / 'queue-state.json').get('manifest_sha256') != identity:
        raise t.SubtitleError('Existing queue identity differs')
    for path, value in ((manifest_path, manifest), (state / 'inventory.json', evidence)):
        if path.exists() and t.read_json(path) != value:
            raise t.SubtitleError('Existing plan differs; use a deliberately new private state directory')
    publish(state / 'inventory.json', evidence)
    return publish(manifest_path, manifest)


def context(request_path, manifest_path=None):
    """Read active identity and reuse Queue checks without its constructor/writes."""
    path = private(request_path)
    directory = private(path.parent.parent)
    request = b.load(path)
    if path.parent.name != 'review-requests':
        raise t.SubtitleError('Request must be inside the queue review-requests directory')
    manifest, identity = b.manifest_for(private(manifest_path) if manifest_path else directory / 'manifest.json')
    state = b.load(directory / 'queue-state.json')
    if (type(request.get('version')) is not int or request.get('version') != b.VERSION
            or type(request.get('wave')) is not int or request['wave'] < 1
            or not isinstance(request.get('group'), str) or not b.TOKEN.fullmatch(request['group'])
            or request.get('manifest_sha256') != identity or state.get('manifest_sha256') != identity
            or state.get('version') != b.VERSION or (directory / 'failed.json').exists()
            or state.get('status') == 'failed' or path.name != f"{request['wave']}-{request['group']}.json"):
        raise t.SubtitleError('Request does not match a usable current queue')
    queue = b.Queue.__new__(b.Queue)
    queue.directory, queue.manifest, queue.identity = directory, manifest, identity
    queue.jobs, queue.state = manifest['jobs'], state
    queue.configuration, queue.subtitle_checks, queue.active_source = state['configuration'], {}, None
    if set(state.get('jobs', {})) != {job['id'] for job in queue.jobs}:
        raise t.SubtitleError('Queue jobs differ from manifest')
    queue.jobs = [job for job in queue.jobs if (job['wave'], job['group']) == (request['wave'], request['group'])]
    for job in queue.jobs:
        source = private(job['source'])
        private(t.source_root(source, None))
        if b.fingerprint(source) != queue.entry(job)['source_identity']:
            raise t.SubtitleError('Source changed since queue creation')
    queue.assert_safe()
    jobs = [job for job in queue.jobs if (job['wave'], job['group']) == (request['wave'], request['group'])]
    items = request.get('jobs')
    if not jobs or not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise t.SubtitleError('Invalid request jobs')
    if [item.get('id') for item in items] != [job['id'] for job in jobs]:
        raise t.SubtitleError('Request job IDs differ from manifest group')
    for job, item in zip(jobs, items):
        entry = queue.entry(job)
        if (item.get('source') != job['source'] or item.get('source_sha256') != entry['source_identity']['sha256']
                or type(item.get('decision_required')) is not bool
                or item['decision_required'] != (entry['status'] != 'accepted_existing')
                or item.get('accepted_existing') != job['accepted_existing']
                or item.get('result') != entry.get('result', {})
                or item.get('english_required') != (queue.english_required(item['original_language'])
                    if item.get('original_language') else queue.configuration['require_english'])
                or item.get('english_path') != str(Path(job['source']).with_suffix('.en.srt'))
                or entry['status'] not in {'awaiting_review', 'accepted', 'accepted_existing'}):
            raise t.SubtitleError('Request source or review ownership differs from queue')
        recorded = item.get('native')
        if recorded:
            expected_path = str(Path(job['source']).with_suffix('.' + item['original_language'] + '.srt'))
            expected_hash = entry['protected_outputs'].get(expected_path) or entry.get('result', {}).get('output_sha256')
            if recorded.get('path') != expected_path or (expected_hash and recorded.get('sha256') != expected_hash):
                raise t.SubtitleError('Request native snapshot differs from queue ownership')
        # Newly generated files may receive audio-supported corrections. Their
        # request hashes are snapshots; existing protected outputs remain exact.

    return queue, request, jobs


def cached_result(source, requested):
    root = private(t.source_root(source, None))
    values = [requested]
    if (root / 'result.json').exists():
        values.append(b.load(root / 'result.json'))
    # A queue skip summary can omit cache/flags. Recover the original cache result.
    complete = []
    source_sha256 = b.digest(source)
    for path in sorted(root.glob('*/result.json')):
        identity = path.parent / 'identity.json'
        if identity.exists() and b.load(identity).get('source_sha256') == source_sha256:
            value = b.load(private(path))
            value.setdefault('cache', str(path.parent))
            if private(value['cache']) != path.parent.resolve():
                raise t.SubtitleError('Transcript cache result points outside its source-owned cache')
            complete.append(value)
    caches = {value.get('cache') for value in complete}
    hinted = next((value.get('cache') for value in reversed(values) if value.get('cache')), None)
    if hinted:
        hinted_path = private(hinted)
        if (hinted_path.parent != root or not (hinted_path / 'identity.json').exists()
                or b.load(hinted_path / 'identity.json').get('source_sha256') != source_sha256):
            raise t.SubtitleError('Cached result does not belong to the assigned source')
        complete = [value for value in complete if private(value.get('cache')) == hinted_path]
    elif len(caches) > 1:
        raise t.SubtitleError('Multiple transcript caches; choose the correct request/cache explicitly')
    return complete[-1] if complete else next((value for value in reversed(values) if value.get('cache')), values[-1])


def samples(values):
    """Bounded representatives, not a semantic review or guaranteed dialogue."""
    if not isinstance(values, list):
        raise t.SubtitleError('Cached samples must be arrays')
    positions = [('opening', 0), ('middle_early', .25), ('middle', .5), ('middle_late', .75), ('ending', 1)]
    result = []
    for region, fraction in positions:
        start = min(max(0, int((len(values) - 1) * fraction)), max(0, len(values) - 3))
        result.append({'region': region, 'items': values[start:start + 3]})
    speakers = [index for index in range(1, len(values)) if values[index].get('speaker') != values[index - 1].get('speaker')]
    if speakers:
        index = speakers[len(speakers) // 2]
        result.append({'region': 'speaker_change_candidate', 'items': values[max(0, index - 1):index + 2]})
    return result


def srt_samples(path):
    blocks = re.split(r'\n\s*\n', Path(path).read_text(encoding='utf-8-sig').strip())
    return samples([{'lines': block.splitlines()} for block in blocks])


def packet(args):
    queue, request, jobs = context(args.request, args.manifest)
    work = private(args.work_dir)
    if work.exists() and (not work.is_dir() or any(work.iterdir())):
        raise t.SubtitleError('Packet requires an empty private work directory')
    entries = []
    for job, item in zip(jobs, request['jobs']):
        source = Path(job['source'])
        result = cached_result(source, item.get('result', {}))
        native, language = queue.native(job)
        entry = {'id': job['id'], 'source': job['source'], 'source_sha256': item['source_sha256'],
                 'decision_required': item['decision_required'], 'metadata': t.read_metadata(source),
                 'existing_outputs': outputs(source), 'result': result, 'samples': {},
                 'review_flags': result.get('review_flags', []), 'corrections': result.get('corrections', [])}
        cache = private(result['cache']) if result.get('cache') else None
        if cache:
            for kind in ('cues', 'words'):
                path = cache / (kind + '.json')
                if path.exists():
                    entry['samples'][kind] = samples(t.read_json(path))
        if native:
            entry['samples']['native_final'] = srt_samples(native)
        english = source.with_suffix('.en.srt')
        if english.exists() and language and queue.english_required(language):
            entry['samples']['english_final'] = srt_samples(english)
        entry['label_command'] = [str(Path(__file__).resolve()), 'label', '--request', str(Path(args.request).resolve()),
                                  '--job', job['id'], '--language', 'ACTUAL_BCP47_SELECTED_BY_REVIEWER']
        if args.manifest:
            entry['label_command'].extend(['--manifest', str(Path(args.manifest).resolve())])
        entries.append(entry)
    return publish(work / 'packet.json', {'version': 1, 'request': str(Path(args.request).resolve()),
        'manifest_sha256': queue.identity, 'wave': request['wave'], 'group': request['group'],
        'review_instructions': str(PUBLIC_ROOT / 'skills/subtitle-creation/references/batch-review.md'),
        'assignment': 'Review only these jobs; record actual findings and samples in reviewer-authored notes. Code has not reviewed semantics.',
        'jobs': entries})


def label(args):
    queue, request, jobs = context(args.request, args.manifest)
    job = next((job for job in jobs if job['id'] == args.job), None)
    if job is None or not t.valid_language_tag(args.language):
        raise t.SubtitleError('Label needs an assigned job and actual confirmed BCP47 language')
    source = Path(job['source'])
    native, language = queue.native(job)
    if language and language != args.language:
        raise t.SubtitleError('Existing native language differs; preserve confirmed original and translations')
    if native:
        return native
    target = private(source.with_suffix('.' + args.language + '.srt'))
    if target.exists():
        raise t.SubtitleError('Unconfirmed destination exists; preserve it for review')
    item = next(item for item in request['jobs'] if item['id'] == job['id'])
    result = cached_result(source, item.get('result', {}))
    settings = result.get('settings')
    if not isinstance(settings, dict) or not result.get('cache') or result.get('status') == 'failed':
        raise t.SubtitleError('No usable cached transcription configuration')
    options = [str(source), '--format-only', '--output-language', args.language, '--model', settings['model'],
               '--location', settings['location'], '--language', settings['language']]
    episode_args = t.parser().parse_args(options)
    pool = t.RequestPool(1)
    # Fail closed if format-only ever accidentally requests authentication/network.
    def forbidden(*_args, **_kwargs):
        raise t.SubtitleError('Labeling is offline; authentication and requests are forbidden')
    pool.token = pool.resolve_model = pool.transcribe = forbidden
    value = t.process_episode(source, episode_args, pool)
    if value.get('status') not in {'completed', 'skipped'}:
        raise t.SubtitleError('Cached publication failed; inspect the private result checkpoint')
    return target


def accept(args):
    queue, request, jobs = context(args.request, args.manifest)
    notes = b.load(private(args.notes))
    values = notes.get('jobs')
    required = {item['id'] for item in request['jobs'] if item['decision_required']}
    if (not isinstance(values, list) or any(not isinstance(value, dict) for value in values)
            or any(not isinstance(value.get('id'), str) for value in values)
            or len({value['id'] for value in values}) != len(values) or {value['id'] for value in values} != required):
        raise t.SubtitleError('Reviewer notes must explicitly cover exactly the required job IDs')
    by_id, decisions = {job['id']: job for job in jobs}, []
    for note in values:
        if set(note) - {'id', 'outcome', 'flags', 'provenance', 'reason'}:
            raise t.SubtitleError('Notes contain unsupported fields; hashes and identities are generated from current files')
        value = copy.deepcopy(note)
        value['source_sha256'] = queue.entry(by_id[note['id']])['source_identity']['sha256']
        if note.get('outcome') == 'accepted':
            native, language = queue.native(by_id[note['id']])
            if native is None:
                raise t.SubtitleError('Accepted note requires confirmed native publication')
            value.update(original_language=language, native=queue.checked(native),
                         english=queue.checked(Path(by_id[note['id']]['source']).with_suffix('.en.srt'))
                         if queue.english_required(language) else None)
        decisions.append(value)
    decision = {'version': 1, 'manifest_sha256': queue.identity, 'wave': request['wave'],
                'group': request['group'], 'jobs': decisions}
    # Validate each explicit note with the public queue checker on an isolated copy.
    # Failure decisions intentionally raise after their provenance/reason checks.
    with tempfile.TemporaryDirectory(prefix='subtitle-review-check-') as temporary:
        check = copy.copy(queue)
        check.directory, check.state = Path(temporary), copy.deepcopy(queue.state)
        check.save = lambda: None
        check.event = lambda *_args, **_kwargs: None
        for value in decisions:
            partial = dict(decision, jobs=[value])
            t.atomic_json(check.directory / 'review-decisions' / f"{request['wave']}-{request['group']}.json", partial)
            try:
                check.decision(request['wave'], request['group'], [by_id[value['id']]])
            except t.SubtitleError as error:
                if value.get('outcome') != 'failed' or str(error) != 'Review failed; no further episodes will start':
                    raise
            check.state = copy.deepcopy(queue.state)
    current, fresh, _ = context(args.request, args.manifest)
    if (fresh != request or current.configuration != queue.configuration
            or any(current.entry(job) != queue.entry(job) for job in jobs)):
        raise t.SubtitleError('Queue changed during review validation; retry from current request')
    # Recheck source/output bytes at publication, never alter active queue state.
    for value in decisions:
        if value.get('outcome') == 'accepted':
            for key in ('native', 'english'):
                if value.get(key) and current.checked(value[key]['path']) != value[key]:
                    raise t.SubtitleError('Final subtitle changed during acceptance')
    return publish(queue.directory / 'review-decisions' / f"{request['wave']}-{request['group']}.json", decision)


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest='command', required=True)
    command = commands.add_parser('plan', help='Inventory only the explicitly authorized media root; no API calls')
    command.add_argument('--root', type=Path, required=True)
    command.add_argument('--state-dir', type=Path, required=True)
    command.add_argument('--wave-size', type=int, default=4)
    command.add_argument('--episode-pattern', default=EPISODE, help='Regex with named episode integer group; zero denotes extras')
    for name in ('packet', 'label', 'accept'):
        command = commands.add_parser(name)
        command.add_argument('--request', type=Path, required=True)
        command.add_argument('--manifest', type=Path, help='Existing explicit manifest; defaults to queue state-dir/manifest.json')
        if name == 'packet':
            command.add_argument('--work-dir', type=Path, required=True)
        elif name == 'label':
            command.add_argument('--job', required=True)
            command.add_argument('--language', required=True)
        else:
            command.add_argument('--notes', type=Path, required=True)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    previous = os.umask(0o077)
    try:
        print(globals()[args.command](args))
        return 0
    finally:
        os.umask(previous)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (t.SubtitleError, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, t.SubtitleError) else 'Invalid local inventory or review data', file=b.sys.stderr)
        raise SystemExit(1)
