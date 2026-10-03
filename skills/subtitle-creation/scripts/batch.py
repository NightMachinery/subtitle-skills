#!/usr/bin/env python3
"""Resumable, serial transcription queue with reviewed priority-wave barriers.

Manifests, checkpoints, events and reviews contain private media information.
Keep --state-dir outside public repositories. This helper never reviews language
or translates text itself, and never forces an English source label.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import time
import threading

import transcribe as t

VERSION = 1
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def notification_argv(args):
    if not args.notify_command_file:
        return None
    value = t.read_json(args.notify_command_file)
    if not isinstance(value, list) or not value or any(not isinstance(part, str) or '\x00' in part for part in value) or not value[0].strip():
        raise t.SubtitleError('Notification command file must contain a JSON argv string array')
    return value


def notify(argv, event):
    if argv is None:
        return True
    try:
        subprocess.run(argv, input=json.dumps(event) + '\n', text=True, check=True,
                       shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def emit(directory, identity, kind, **fields):
    event = {'version': VERSION, 'time': time.time(), 'manifest_sha256': identity,
             'event': kind, **fields}
    data = (json.dumps(event, allow_nan=False) + '\n').encode()
    with (directory / 'events.jsonl').open('ab', buffering=0) as output:
        output.write(data)  # One append, under the exclusive coordinator lock.
        os.fsync(output.fileno())
    print(json.dumps(event), flush=True)
    return event


def load(path):
    value = t.read_json(path)
    if not isinstance(value, dict):
        raise t.SubtitleError('Expected a JSON object')
    return value


def manifest_for(path):
    value = load(path)
    if set(value) != {'version', 'jobs'} or type(value['version']) is not int or value['version'] != VERSION:
        raise t.SubtitleError('Unsupported manifest schema')
    if not isinstance(value['jobs'], list) or not value['jobs']:
        raise t.SubtitleError('Manifest needs explicit media jobs')
    ids, sources, normalized = set(), set(), []
    for job in value['jobs']:
        if not isinstance(job, dict) or set(job) - {'id', 'wave', 'group', 'source', 'accepted_existing'}:
            raise t.SubtitleError('Invalid manifest job schema')
        if not {'id', 'wave', 'group', 'source'} <= set(job):
            raise t.SubtitleError('Manifest job is missing required fields')
        if any(not isinstance(job[k], str) or not TOKEN.fullmatch(job[k]) for k in ('id', 'group')):
            raise t.SubtitleError('Job IDs and groups must be safe, nonempty tokens')
        if type(job['wave']) is not int or job['wave'] < 1:
            raise t.SubtitleError('Waves must be positive integers')
        if type(job.get('accepted_existing', False)) is not bool:
            raise t.SubtitleError('accepted_existing must be boolean')
        if not isinstance(job['source'], str) or not Path(job['source']).is_absolute():
            raise t.SubtitleError('Manifest sources must be explicit absolute paths')
        source = Path(job['source']).resolve()
        if not source.is_file():
            raise t.SubtitleError('Manifest source is not an existing file')
        if job['id'] in ids or str(source) in sources:
            raise t.SubtitleError('Duplicate job ID or source path')
        # Different media extensions sharing a stem would own the same output.
        if any(Path(other['source']).with_suffix('') == source.with_suffix('') for other in normalized):
            raise t.SubtitleError('Manifest media have overlapping subtitle destinations')
        ids.add(job['id'])
        sources.add(str(source))
        normalized.append({**job, 'source': str(source),
                           'accepted_existing': job.get('accepted_existing', False)})
    normalized.sort(key=lambda job: (job['wave'], job['group']))  # Stable input order within a group.
    manifest = {'version': VERSION, 'jobs': normalized}
    return manifest, canonical(manifest)


def fingerprint(path):
    before = Path(path).stat()
    value = {'bytes': before.st_size, 'mtime_ns': before.st_mtime_ns, 'sha256': digest(path)}
    after = Path(path).stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise t.SubtitleError('Source changed while computing its identity')
    return value


def timeline(path):
    text = Path(path).read_text(encoding='utf-8-sig').replace('\r\n', '\n').strip()
    return [block.splitlines()[:2] for block in re.split(r'\n\s*\n', text)]


def checked_subtitle(path):
    path = Path(path).resolve()
    if not t.valid_srt(path):
        raise t.SubtitleError('Subtitle failed structural validation')
    data = json.loads(t.command(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path)]).stdout)
    if not any(stream.get('codec_type') == 'subtitle' for stream in data.get('streams', [])):
        raise t.SubtitleError('ffprobe did not recognize a subtitle stream')
    return {'path': str(path), 'sha256': digest(path)}


@contextlib.contextmanager
def queue_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    with (directory / 'queue.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise t.SubtitleError('Another coordinator owns this queue') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class GuardedRequestPool(t.RequestPool):
    """Inspect ready reviews after auth/cooldown, before a request is marked sent."""
    def __init__(self, workers, project=None, gcloud='gcloud', queue=None, **kwargs):
        super().__init__(workers, project, gcloud, **kwargs)
        self.queue = queue

    def token(self):
        token = super().token()
        if self.queue is not None:
            with self.queue.guard_lock:
                wave = self.queue.state.get('current_wave')
                if wave is not None:
                    self.queue.wave_decisions(wave, [job for job in self.queue.jobs if job['wave'] == wave])
        return token


class Queue:
    def __init__(self, args, pool=None):
        self.args, self.directory = args, args.state_dir.resolve()
        self.notify_argv = notification_argv(args)
        self.subtitle_checks = {}
        self.guard_lock = threading.RLock()
        self.active_source = None
        if (self.directory / 'failed.json').exists():
            raise t.SubtitleError('Queue is halted after a previous failure; inspect its private failed marker')
        self.manifest, self.identity = manifest_for(args.manifest)
        self.jobs = self.manifest['jobs']
        self.configuration = {'model': args.model, 'location': args.location,
                              'require_english': not args.no_english}
        self.pool = pool if pool is not None else GuardedRequestPool(args.request_workers, None, args.gcloud, self)
        self.path = self.directory / 'queue-state.json'
        if self.path.exists():
            self.state = load(self.path)
            if self.state.get('version') != VERSION or self.state.get('manifest_sha256') != self.identity:
                raise t.SubtitleError('Queue manifest identity changed')
            if self.state.get('configuration') != self.configuration:
                raise t.SubtitleError('Queue model, location or English requirement changed')
            if set(self.state.get('jobs', {})) != {job['id'] for job in self.jobs}:
                raise t.SubtitleError('Queue job state does not match its manifest')
            for job in self.jobs:
                if fingerprint(job['source']) != self.entry(job)['source_identity']:
                    raise t.SubtitleError('Source changed since queue creation')
                self.verify_protected(job)
        else:
            self.state = {'version': VERSION, 'manifest_sha256': self.identity,
                          'configuration': self.configuration, 'status': 'running', 'jobs': {}}
            # Check the entire manifest before any paid episode. Protect all existing
            # tagged SRTs belonging to explicit media, including translations.
            for job in self.jobs:
                source = Path(job['source'])
                t.probe(source)
                protected = {str(path.resolve()): digest(path) for path in source.parent.iterdir()
                             if path.is_file() and path.name.startswith(source.stem + '.')
                             and path.name.endswith('.srt')}
                self.state['jobs'][job['id']] = {'status': 'pending', 'source_identity': fingerprint(source),
                                               'protected_outputs': protected}
                native, language = self.native(job)
                if native:
                    self.checked(native)
                if job['accepted_existing'] and native is None:
                    raise t.SubtitleError('accepted_existing requires a valid confirmed native subtitle')
                if job['accepted_existing']:
                    self.entry(job)['prior_accepted_existing'] = True
                    self.entry(job)['existing_native'] = self.checked(native)
            self.save()
            self.event('queue_created')
        self.assert_safe()

    def checked(self, path):
        path = Path(path).resolve()
        key = (str(path), digest(path))
        if key not in self.subtitle_checks:
            checked = checked_subtitle(path)
            if checked['sha256'] != key[1]:
                raise t.SubtitleError('Subtitle changed during structural validation')
            self.subtitle_checks[key] = checked
        return dict(self.subtitle_checks[key])

    def wave_decisions(self, wave, jobs):
        for group in sorted({job['group'] for job in jobs}):
            self.decision(wave, group, [job for job in jobs if job['group'] == group])

    def entry(self, job):
        return self.state['jobs'][job['id']]

    def save(self):
        t.atomic_json(self.path, self.state)

    def event(self, kind, **fields):
        return emit(self.directory, self.identity, kind, **fields)

    def milestone(self, kind, **fields):
        event = self.event(kind, **fields)
        if not notify(self.notify_argv, event):
            self.event('notify_failed', milestone=kind, reason='Notification command failed or timed out')

    def verify_protected(self, job):
        for path, expected in self.entry(job)['protected_outputs'].items():
            if not Path(path).is_file() or digest(path) != expected:
                raise t.SubtitleError('An existing subtitle was changed or removed')

    def assert_safe(self):
        statuses = {'pending', 'processing', 'awaiting_review', 'accepted', 'accepted_existing', 'failed'}
        if self.state.get('status') not in {'running', 'waiting_review', 'completed', 'failed'}:
            raise t.SubtitleError('Invalid queue status')
        for job in self.jobs:
            source, entry = Path(job['source']), self.entry(job)
            if entry.get('status') not in statuses:
                raise t.SubtitleError('Invalid episode status')
            if self.state['status'] == 'completed' and entry['status'] not in {'accepted', 'accepted_existing'}:
                raise t.SubtitleError('Completed queue contains an unaccepted episode')
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != (entry['source_identity']['bytes'], entry['source_identity']['mtime_ns']):
                raise t.SubtitleError('Source changed since queue creation')
            self.verify_protected(job)
            root = t.source_root(source, None)
            prior_result = root / 'result.json'
            if prior_result.exists() and load(prior_result).get('status') == 'failed':
                raise t.SubtitleError('A previous episode failed; inspect its private checkpoint')
            # A crash after a response was saved can leave a harmless marker.
            # During this process's active episode, other request workers can
            # legitimately hold markers. Resume and post-episode checks still
            # reject unresolved markers before additional episodes start.
            markers = [] if str(source) == self.active_source else root.glob('*/chunk-*.inflight.json')
            for marker in markers:
                if not marker.with_name(marker.name.replace('.inflight.json', '.json')).exists():
                    raise t.SubtitleError('Previous request outcome is uncertain; inspect its inflight checkpoint')
            if entry['status'] in ('accepted', 'accepted_existing'):
                self.verify_acceptance(job)

    def native(self, job):
        source = Path(job['source'])
        metadata = t.read_metadata(source)
        language = metadata.get('original_language')
        name = metadata.get('original_subtitle')
        if not language or not name:
            return None, language
        native = (source.parent / name).resolve()
        if native.parent != source.parent or native.name != source.with_suffix('.' + language + '.srt').name:
            raise t.SubtitleError('Native metadata must identify the media language-tagged subtitle')
        if not native.exists():
            return None, language
        if not t.valid_srt(native):
            raise t.SubtitleError('Existing native subtitle is invalid')
        return native, language

    def english_required(self, language):
        return self.configuration['require_english'] and language.split('-')[0].lower() != 'en'

    def verify_acceptance(self, job):
        entry = self.entry(job)
        accepted = entry.get('acceptance')
        if not isinstance(accepted, dict):
            raise t.SubtitleError('Accepted job has no output hashes')
        if entry['status'] == 'accepted_existing':
            if not job['accepted_existing'] or accepted.get('native') != entry.get('existing_native'):
                raise t.SubtitleError('Prior acceptance is not supported by the manifest and original hash')
        else:
            review = entry.get('decision', {})
            provenance = review.get('provenance', {})
            if (review.get('outcome') != 'accepted' or review.get('source_sha256') != entry['source_identity']['sha256']
                    or any(review.get(key) != accepted.get(key) for key in ('native', 'english', 'original_language'))
                    or not isinstance(review.get('flags'), list) or not isinstance(provenance, dict)
                    or any(not isinstance(provenance.get(key), str) or not provenance[key].strip()
                           for key in ('reviewer', 'method', 'reviewed_at'))
                    or not isinstance(provenance.get('samples'), list)
                    or not {'opening', 'middle', 'ending'} <= set(provenance['samples'])):
                raise t.SubtitleError('Accepted episode has no valid saved review decision')
        native, language = self.native(job)
        if language != accepted.get('original_language') or native is None:
            raise t.SubtitleError('Accepted native metadata changed')
        if self.checked(native) != accepted.get('native'):
            raise t.SubtitleError('Accepted native subtitle changed')
        if self.english_required(language):
            english = Path(job['source']).with_suffix('.en.srt')
            if self.checked(english) != accepted.get('english') or timeline(native) != timeline(english):
                raise t.SubtitleError('Accepted English subtitle changed or has a different timeline')

    def request(self, wave, group, jobs):
        path = self.directory / 'review-requests' / f'{wave}-{group}.json'
        if path.exists():
            existing = load(path)
            if existing.get('manifest_sha256') != self.identity:
                raise t.SubtitleError('Review request belongs to another manifest')
            return
        items = []
        for job in jobs:
            entry = self.entry(job)
            native, language = self.native(job)
            items.append({'id': job['id'], 'source': job['source'],
                          'source_sha256': entry['source_identity']['sha256'],
                          'accepted_existing': job['accepted_existing'], 'status': entry['status'],
                          'original_language': language,
                          'native': {'path': str(native), 'sha256': digest(native)} if native else None,
                          'english_required': self.english_required(language) if language else not self.args.no_english,
                          'english_path': str(Path(job['source']).with_suffix('.en.srt')),
                          'result': entry.get('result', {}),
                          'decision_required': entry['status'] != 'accepted_existing'})
        t.atomic_json(path, {'version': VERSION, 'manifest_sha256': self.identity,
                             'wave': wave, 'group': group, 'jobs': items})
        self.milestone('review_requested', wave=wave, group=group, request=str(path))

    def process(self, job):
        entry = self.entry(job)
        if entry['status'] not in ('pending', 'processing'):
            return
        self.assert_safe()
        native, language = self.native(job)
        if native:
            result = {'status': 'skipped', 'reason': 'existing_valid_srt',
                      'output': str(native), 'original_language': language}
        else:
            entry['status'] = 'processing'
            self.save()
            self.event('episode_started', id=job['id'], wave=job['wave'], group=job['group'])
            if self.pool.project is None:
                self.pool.project = t.select_project(self.args.project, config_path=self.args.config)
            options = [job['source'], '--request-workers', str(self.args.request_workers),
                       '--episode-workers', '1', '--model', self.args.model, '--location', self.args.location,
                       '--gcloud', self.args.gcloud]
            episode_args = t.parser().parse_args(options)
            # Deliberately leave source-language auto and output-language unset.
            self.active_source = job['source']
            try:
                result = t.process_episode(Path(job['source']), episode_args, self.pool)
            finally:
                self.active_source = None
        entry['result'] = result
        entry['status'] = 'awaiting_review'
        if result.get('status') not in ('completed', 'skipped', 'needs_language'):
            entry['status'] = 'failed'
            self.save()
            raise t.SubtitleError('Episode failed; inspect its private result checkpoint')
        if job['accepted_existing'] and not self.english_required(language):
            entry['status'] = 'accepted_existing'
            entry['acceptance'] = {'original_language': language, 'native': self.checked(native), 'english': None}
        self.save()
        self.event('episode_transcribed', id=job['id'], wave=job['wave'], status=entry['status'])

    def decision(self, wave, group, jobs):
        path = self.directory / 'review-decisions' / f'{wave}-{group}.json'
        if not path.exists():
            return
        decision = load(path)
        if type(decision.get('version')) is not int or type(decision.get('wave')) is not int:
            raise t.SubtitleError('Invalid review version or wave')
        if (decision.get('version'), decision.get('manifest_sha256'), decision.get('wave'), decision.get('group')) != (
                VERSION, self.identity, wave, group):
            raise t.SubtitleError('Review decision identity does not match its request')
        values = decision.get('jobs')
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise t.SubtitleError('Review decision needs job objects')
        required = {job['id'] for job in jobs if self.entry(job)['status'] != 'accepted_existing'}
        ids = [value.get('id') for value in values]
        if len(ids) != len(set(ids)) or set(ids) != required:
            raise t.SubtitleError('Review decision job IDs do not match the group')
        by_id = {job['id']: job for job in jobs}
        accepted = []
        for value in values:
            job = by_id[value['id']]
            entry = self.entry(job)
            provenance = value.get('provenance')
            if not isinstance(value.get('flags'), list) or not isinstance(provenance, dict):
                raise t.SubtitleError('Review requires flags and provenance')
            if any(not isinstance(provenance.get(key), str) or not provenance[key].strip()
                   for key in ('reviewer', 'method', 'reviewed_at')):
                raise t.SubtitleError('Review provenance is incomplete')
            if value.get('source_sha256') != entry['source_identity']['sha256']:
                raise t.SubtitleError('Review source identity does not match')
            if value.get('outcome') == 'failed':
                if not isinstance(value.get('reason'), str) or not value['reason'].strip():
                    raise t.SubtitleError('Failed review requires a reason')
                entry['status'], entry['decision'] = 'failed', value
                self.save()
                raise t.SubtitleError('Review failed; no further episodes will start')
            if value.get('outcome') != 'accepted' or not isinstance(provenance.get('samples'), list) or not {
                    'opening', 'middle', 'ending'} <= set(provenance['samples']):
                raise t.SubtitleError('Accepted review must cover opening, middle and ending')
            native, language = self.native(job)
            if not native or value.get('original_language') != language:
                raise t.SubtitleError('Review needs a confirmed native language and subtitle')
            if self.checked(native) != value.get('native'):
                raise t.SubtitleError('Review native subtitle hash or path does not match')
            if job['accepted_existing'] and value['native'] != entry['existing_native']:
                raise t.SubtitleError('Previously accepted native subtitle changed')
            english = None
            if self.english_required(language):
                english = self.checked(Path(job['source']).with_suffix('.en.srt'))
                if english != value.get('english') or timeline(native) != timeline(english['path']):
                    raise t.SubtitleError('Review English hash, path or timeline does not match')
            elif value.get('english') is not None:
                raise t.SubtitleError('Unexpected English output in this review')
            accepted.append((entry, value, {'original_language': language, 'native': value['native'], 'english': english}))
        # Validate every decision before accepting the group.
        self.assert_safe()
        for entry, value, outputs in accepted:
            newly_accepted = entry['status'] != 'accepted'
            entry.update(status='accepted', decision=value, acceptance=outputs)
            if newly_accepted:
                self.event('review_accepted', id=value['id'], wave=wave, group=group)
        self.save()

    def step(self):
        if self.state['status'] == 'failed':
            raise t.SubtitleError('Queue is halted after a previous failure')
        self.assert_safe()
        if self.state['status'] == 'completed':
            return 0
        for wave in sorted({job['wave'] for job in self.jobs}):
            jobs = [job for job in self.jobs if job['wave'] == wave]
            if all(self.entry(job)['status'] in ('accepted', 'accepted_existing') for job in jobs):
                continue
            self.state.update(status='running', current_wave=wave)
            self.save()
            groups = sorted({job['group'] for job in jobs})
            self.wave_decisions(wave, jobs)
            for group in groups:
                group_jobs = [job for job in jobs if job['group'] == group]
                # Consume an available failed review before any more paid work.
                self.decision(wave, group, group_jobs)
                for job in group_jobs:
                    # Reviews can arrive while the preceding episode transcribes.
                    # Inspect every ready group decision before starting another.
                    self.wave_decisions(wave, jobs)
                    self.process(job)
                self.request(wave, group, group_jobs)
                self.decision(wave, group, group_jobs)
            self.assert_safe()
            if not all(self.entry(job)['status'] in ('accepted', 'accepted_existing') for job in jobs):
                self.state['status'] = 'waiting_review'
                self.save()
                return 2
            self.event('wave_completed', wave=wave)
        self.state['status'] = 'completed'
        self.save()
        t.atomic_json(self.directory / 'completed.json', {'version': VERSION, 'manifest_sha256': self.identity})
        self.milestone('queue_completed')
        return 0

    def fail(self, error):
        self.state.update(status='failed', error=str(error))
        self.save()
        t.atomic_json(self.directory / 'failed.json', {'version': VERSION, 'manifest_sha256': self.identity,
                                                     'error': str(error)})
        self.milestone('queue_failed', error=str(error))


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument('--manifest', type=Path, required=True)
    value.add_argument('--state-dir', type=Path, required=True)
    value.add_argument('--request-workers', type=int, default=1)
    value.add_argument('--notify-command-file', type=Path, help='Private JSON argv array; milestone event JSON goes to stdin')
    value.add_argument('--project')
    value.add_argument('--config', type=Path)
    value.add_argument('--model', default='auto')
    value.add_argument('--location', default='global')
    value.add_argument('--gcloud', default='gcloud')
    value.add_argument('--no-english', action='store_true', help='Explicitly opt out of English translation')
    value.add_argument('--poll-seconds', type=float, default=5)
    value.add_argument('--once', action='store_true', help='Exit 2 instead of waiting when reviews are pending')
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    if args.request_workers < 1 or not 0 < args.poll_seconds <= 60 or not re.fullmatch(r'[a-z0-9-]+', args.location):
        raise t.SubtitleError('Invalid worker count, poll interval or location')
    previous_umask = os.umask(0o077)
    try:
        with queue_lock(args.state_dir.resolve()):
            queue = None
            try:
                queue = Queue(args)
                while True:
                    status = queue.step()
                    if status != 2 or args.once:
                        return status
                    time.sleep(args.poll_seconds)
            except Exception as error:
                controlled = error if isinstance(error, t.SubtitleError) else t.SubtitleError('Invalid local queue state or output')
                if queue is not None:
                    queue.fail(controlled)
                else:
                    t.atomic_json(args.state_dir.resolve() / 'failed.json', {'version': VERSION, 'error': str(controlled)})
                    event = emit(args.state_dir.resolve(), None, 'queue_failed', error=str(controlled))
                    try:
                        if not notify(notification_argv(args), event):
                            emit(args.state_dir.resolve(), None, 'notify_failed', milestone='queue_failed',
                                 reason='Notification command failed or timed out')
                    except t.SubtitleError:
                        pass
                print(str(controlled), file=sys.stderr)
                return 1
    finally:
        os.umask(previous_umask)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except t.SubtitleError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
