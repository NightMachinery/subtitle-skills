#!/usr/bin/env python3
"""Deterministic, resumable source-language audio transcription and SRT formatting.

Python standard library, ffmpeg, ffprobe and authenticated gcloud only. No
general text model is used. Importable functions permit offline regression tests.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import wave


DEFAULT_MODEL = 'auto'
FORMAT_VERSION = 1


class SubtitleError(RuntimeError):
    """A controlled failure whose message contains no credentials/project IDs."""


def select_project(explicit=None, environ=None, config_path=None):
    """Explicit/environment values override the private TOML project setting."""
    environ = os.environ if environ is None else environ
    selected = explicit or environ.get('SUBTITLE_GCP_PROJECT')
    if selected:
        if not isinstance(selected, str) or not selected.strip():
            raise SubtitleError('Subtitle project setting must be nonempty text')
        return selected
    config_path = Path(config_path or environ.get('SUBTITLE_CONFIG') or
                       Path.home() / '.config' / 'subtitle-creation' / 'config.toml')
    if config_path.exists():
        try:
            config = tomllib.loads(config_path.read_text(encoding='utf-8'))
            selected = config.get('vertex', {}).get('project')
        except (OSError, ValueError, AttributeError) as error:
            raise SubtitleError('Private subtitle configuration is invalid') from error
        if selected:
            if not isinstance(selected, str) or not selected.strip():
                raise SubtitleError('Subtitle project setting must be nonempty text')
            return selected
    raise SubtitleError('Pass --project or set SUBTITLE_GCP_PROJECT before transcription')


def settings(args):
    return {'model': args.model, 'location': args.location, 'language': args.language,
            'word_timestamps': True, 'diarization': True, 'format_version': FORMAT_VERSION,
            'section_target_seconds': 300, 'section_max_seconds': 900,
            'silence_noise_db': -32, 'silence_min_seconds': 0.35}


def atomic_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2, allow_nan=False) + '\n').encode())


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise SubtitleError('Missing or invalid checkpoint: ' + Path(path).name) from error


def source_key(video):
    return hashlib.sha256(str(Path(video).resolve()).encode()).hexdigest()[:24]


def source_root(video, cache_dir):
    base = Path(cache_dir) if cache_dir else Path(video).parent / '.subtitle-cache'
    return base / 'sources' / source_key(video)


@contextlib.contextmanager
def source_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'source.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def identity_for(video, root, configuration):
    before = video.stat()
    signature = {'bytes': before.st_size, 'mtime_ns': before.st_mtime_ns}
    digest_path = root / 'source-digest.json'
    cached = read_json(digest_path) if digest_path.exists() else {}
    if cached.get('signature') == signature:
        digest = cached['sha256']
    else:
        hasher = hashlib.sha256()
        with video.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                hasher.update(block)
        after = video.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise SubtitleError('Source changed while computing its checksum')
        digest = hasher.hexdigest()
        atomic_json(digest_path, {'signature': signature, 'sha256': digest})
    identity = {**signature, 'source_sha256': digest, 'settings': configuration}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    return identity, root / key


def command(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True)
    except FileNotFoundError as error:
        raise SubtitleError('Required executable unavailable: ' + Path(args[0]).name) from error
    except subprocess.CalledProcessError as error:
        # stderr can contain user paths, project identifiers or credentials.
        raise SubtitleError('Command failed: ' + Path(args[0]).name) from error


def probe(video):
    data = json.loads(command(['ffprobe', '-v', 'error', '-show_streams',
                               '-show_format', '-of', 'json', str(video)]).stdout)
    audio = [stream for stream in data.get('streams', []) if stream.get('codec_type') == 'audio']
    if not audio:
        raise SubtitleError('Input media has no audio stream')
    try:
        duration = float(data.get('format', {}).get('duration', audio[0].get('duration')))
    except (TypeError, ValueError) as error:
        raise SubtitleError('Input media has no usable duration') from error
    if not math.isfinite(duration) or duration <= 0:
        raise SubtitleError('Input media has no usable duration')
    return duration


def choose_boundaries(duration, pauses):
    """Cut only measured silence; expand the search if the 20-second window fails."""
    boundaries = [0.0]
    while duration - boundaries[-1] > 320:
        start = boundaries[-1]
        nominal = start + 300
        eligible = [pause for pause in pauses if start + 60 <= pause < duration
                    and pause - start <= 900]
        nearby = [pause for pause in eligible if abs(pause - nominal) <= 20]
        if nearby:
            boundary = min(nearby, key=lambda point: (abs(point - nominal), point))
        elif eligible:
            boundary = min(eligible, key=lambda point: (abs(point - nominal), point))
        elif duration - start <= 900:
            break  # The whole remaining clip is safe and fits the service limit.
        else:
            raise SubtitleError('No measured silence permits a section of at most 15 minutes')
        boundaries.append(boundary)
    boundaries.append(duration)
    if any(end - start > 900 for start, end in zip(boundaries, boundaries[1:])):
        raise SubtitleError('Section duration exceeds 15 minutes')
    return boundaries


def prepare_audio(video, cache):
    audio_path = cache / 'audio.wav'
    preparation_path = cache / 'preparation.json'
    if preparation_path.exists() and audio_path.exists():
        return read_json(preparation_path)
    probe(video)
    pending = cache / 'audio.pending.wav'
    command(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(video),
             '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(pending)])
    os.replace(pending, audio_path)
    detected = command(['ffmpeg', '-nostdin', '-hide_banner', '-i', str(audio_path),
                        '-af', 'silencedetect=noise=-32dB:d=0.35', '-f', 'null', '-']).stderr
    # ffmpeg silence_end lines contain both interval length and its endpoint.
    silences = [{'start': max(0.0, float(end) - float(length)), 'end': float(end)}
                for end, length in re.findall(
                    r'silence_end: ([\d.]+) \| silence_duration: ([\d.]+)', detected)]
    with wave.open(str(audio_path), 'rb') as audio:
        duration = audio.getnframes() / audio.getframerate()
        rate = audio.getframerate()
    boundaries = choose_boundaries(duration, [(s['start'] + s['end']) / 2 for s in silences])
    chunks = [{'index': index, 'start': round(start * rate) / rate,
               'end': round(end * rate) / rate}
              for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]))]
    preparation = {'duration': duration, 'silences': silences, 'chunks': chunks}
    atomic_json(preparation_path, preparation)
    return preparation


def chunk_audio(audio_path, chunk):
    with wave.open(str(audio_path), 'rb') as audio:
        rate, params = audio.getframerate(), audio.getparams()
        first, end = round(chunk['start'] * rate), round(chunk['end'] * rate)
        audio.setpos(first)
        frames = audio.readframes(end - first)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as clip:
        clip.setparams(params)
        clip.writeframes(frames)
    return buffer.getvalue()


def offset(value):
    try:
        number = float(value[:-1] if isinstance(value, str) and value.endswith('s') else value)
    except (TypeError, ValueError) as error:
        raise SubtitleError('Transcript word has no usable timestamp') from error
    if not math.isfinite(number):
        raise SubtitleError('Transcript contains a non-finite timestamp')
    return number


def timed_words(data, duration):
    candidates = data.get('candidates', [])
    if not candidates or candidates[0].get('finishReason') != 'STOP':
        raise SubtitleError('Transcription did not finish normally')
    words = []
    for part in candidates[0].get('content', {}).get('parts', []):
        transcript = part.get('audioTranscription', {})
        for raw in transcript.get('words', []):
            word = raw.get('word')
            if not isinstance(word, str) or not word.strip() or '\n' in word or '\r' in word:
                raise SubtitleError('Transcript contains an empty or malformed word')
            start, end = offset(raw.get('startOffset')), offset(raw.get('endOffset'))
            if not (0 <= start <= end <= duration + 0.25):
                raise SubtitleError('Word timestamp outside section timeline')
            words.append({'word': word.strip(), 'start': start, 'end': min(end, duration),
                          'speaker': str(raw.get('speakerLabel', transcript.get('speakerLabel', 'narrator')))})
    if not words:
        raise SubtitleError('Transcription returned no timed words')
    return words


def payload_for(audio, configuration):
    transcription = {'wordTimestamp': True, 'diarization': True}
    if configuration['language'] != 'auto':
        transcription['languageCodes'] = [configuration['language']]
    return {'contents': [{'role': 'user', 'parts': [{'inlineData': {
                'mimeType': 'audio/wav', 'data': base64.b64encode(audio).decode('ascii')}}]}],
            'generationConfig': {'audioTranscriptionConfig': transcription}}


def request_json(url, payload, token):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)


class RequestPool:
    """One pool shared by all episode/section threads in this runner process."""
    def __init__(self, workers, project=None, gcloud='gcloud', request=None, sleep=None):
        self.limit = threading.BoundedSemaphore(workers)
        self.project, self.gcloud = project, gcloud
        self.request = request or request_json
        self.sleep = sleep or time.sleep
        self._token = None
        self._token_lock = threading.Lock()
        self._models = {}
        self._model_lock = threading.Lock()

    def resolve_model(self, location, requested):
        if requested != 'auto':
            return requested
        with self._model_lock:
            key = (location, requested)
            if key not in self._models:
                try:
                    from models import resolve_model
                    self._models[key] = resolve_model(self.token(), self.project, location, requested)
                except SubtitleError:
                    raise
                except Exception as error:
                    raise SubtitleError('Automatic transcription model discovery failed; pass --model to select explicitly') from error
            return self._models[key]

    def token(self):
        with self._token_lock:
            if self._token is None:
                env = dict(os.environ, CLOUDSDK_CORE_DISABLE_FILE_LOGGING='true',
                           CLOUDSDK_CORE_DISABLE_USAGE_REPORTING='true')
                try:
                    result = subprocess.run([self.gcloud, 'auth', 'print-access-token'],
                                            env=env, capture_output=True, text=True, check=True)
                except (OSError, subprocess.CalledProcessError) as error:
                    raise SubtitleError('gcloud authentication failed') from error
                self._token = result.stdout.strip()
                if not self._token:
                    raise SubtitleError('gcloud returned no access token')
            return self._token

    def transcribe(self, audio, output, duration, configuration):
        if output.exists():
            data = read_json(output)
            timed_words(data, duration)
            return data
        marker = output.with_suffix('.inflight.json')
        if marker.exists():
            raise SubtitleError('Previous request outcome is uncertain; review its inflight checkpoint before retrying')
        location = configuration['location']
        host = 'aiplatform.googleapis.com' if location == 'global' else location + '-aiplatform.googleapis.com'
        project = urllib.parse.quote(self.project or '', safe='')
        model = urllib.parse.quote(configuration['model'], safe='')
        url = f'https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent'
        payload = payload_for(audio, configuration)
        with self.limit:
            token = self.token()
            for attempt in range(3):
                atomic_json(marker, {'state': 'request-started', 'attempt': attempt + 1})
                try:
                    data = self.request(url, payload, token)
                except urllib.error.HTTPError as error:
                    # A known HTTP rejection may be retried. Unknown outcomes retain
                    # the marker so a restart cannot silently repeat the paid request.
                    marker.unlink(missing_ok=True)
                    retryable = error.code == 429 or 500 <= error.code <= 599
                    error.close()
                    if retryable and attempt < 2:
                        self.sleep(2 ** attempt)
                        continue
                    raise SubtitleError(f'API request failed: HTTP {error.code}') from error
                except Exception as error:
                    raise SubtitleError('API request outcome is uncertain; request was not replayed') from error
                # Keep the raw response even if validation fails: never pay twice
                # merely because formatting or API validation failed after success.
                atomic_json(output, data)
                marker.unlink(missing_ok=True)
                timed_words(data, duration)
                return data


def wrap_words(words):
    text = ' '.join(words)
    if len(text) <= 42:
        return text
    choices = []
    for position in range(1, len(words)):
        left, right = ' '.join(words[:position]), ' '.join(words[position:])
        if max(len(left), len(right)) <= 42:
            penalty = abs(len(left) - len(right))
            if words[position - 1].endswith((',', ';', ':')):
                penalty -= 8
            if words[position - 1].lower() in {'a', 'an', 'the', 'to', 'of', 'and'}:
                penalty += 15
            choices.append((penalty, left + '\n' + right))
    return min(choices)[1] if choices else None


def cue_text(words):
    speakers = list(dict.fromkeys(word['speaker'] for word in words))
    if len(speakers) == 1:
        return wrap_words([word['word'] for word in words])
    if len(speakers) > 2:
        return None
    lines = ['- ' + ' '.join(word['word'] for word in words if word['speaker'] == speaker)
             for speaker in speakers]
    return '\n'.join(lines) if max(map(len, lines)) <= 42 else None


def make_cues(words, duration):
    cues, position = [], 0
    while position < len(words):
        choices = []
        for end in range(position + 1, min(len(words), position + 22) + 1):
            group = words[position:end]
            text = cue_text(group)
            latest_end = max(word['end'] for word in group)
            if text is None or latest_end - group[0]['start'] > 6.5:
                break
            elapsed = latest_end - group[0]['start']
            last = group[-1]['word']
            gap = words[end]['start'] - latest_end if end < len(words) else 99
            score = end - position
            if last.endswith(('.', '?', '!')):
                score += 12
            elif last.endswith((',', ';', ':')):
                score += 4
            if gap >= 0.55:
                score += 12
            if elapsed < 1:
                score -= 12
            # Accept a boundary only after every word in this cue has finished.
            if gap >= 0 and (elapsed > 0 or gap > 0):
                choices.append((score, end, text))
            if gap >= 0.8:
                break
        if not choices:
            raise SubtitleError('Timed speaker phrase cannot fit readable, non-overlapping subtitle cues; review words.json')
        _, end, text = max(choices)
        group = words[position:end]
        latest_end = max(word['end'] for word in group)
        next_start = words[end]['start'] if end < len(words) else duration
        cue_end = min(duration, next_start, max(latest_end + 0.15, group[0]['start'] + 1))
        cues.append({'start': group[0]['start'], 'end': cue_end, 'text': text,
                     'word_ids': [word['id'] for word in group]})
        position = end
    return cues


def repair_long_words(words, silences):
    corrections, flags = [], []
    for word in words:
        interval = word['end'] - word['start']
        if interval < 0.04 or interval > 2:
            flags.append({'kind': 'implausible_word_interval', 'word_id': word['id'], 'seconds': interval})
        if interval <= 2:
            continue
        pauses = [pause for pause in silences if pause['end'] - pause['start'] >= 0.35
                  and word['start'] <= pause['start'] < pause['end'] < word['end']
                  and 0.04 <= word['end'] - pause['end'] <= 1.5]
        # Do not infer an onset through another word's already measured speech.
        if pauses:
            onset = max(pause['end'] for pause in pauses)
            if any(other['id'] != word['id'] and word['start'] < other['start'] < onset for other in words):
                continue
            word['original_start'] = word['start']
            word['start'] = onset
            word['timing_note'] = 'Start adjusted to measured silence end; review inferred onset.'
            correction = {'kind': 'measured_pause_onset', 'word_id': word['id'],
                          'original_start': word['original_start'], 'start': onset}
            corrections.append(correction)
            print(f"Timing correction for word {word['id']}: {word['original_start']:.3f}s to {onset:.3f}s", flush=True)
    return corrections, flags


def validate_cues(cues, words, duration):
    previous_end, flags = 0.0, []
    if not cues or not words:
        raise SubtitleError('Empty subtitle output')
    for index, cue in enumerate(cues, 1):
        start, end = cue['start'], cue['end']
        if not (math.isfinite(start) and math.isfinite(end) and previous_end <= start < end <= duration):
            raise SubtitleError('Overlapping or invalid subtitle cue')
        # Rounded SRT times must remain positive too.
        if round(end * 1000) <= round(start * 1000):
            raise SubtitleError('Subtitle cue duration vanishes when rounded to milliseconds')
        lines = cue['text'].splitlines()
        if not lines or len(lines) > 2 or any(not line or len(line) > 42 for line in lines):
            raise SubtitleError('Subtitle cue exceeds line limits')
        cps = sum(map(len, lines)) / (end - start)
        if cps > 25:
            flags.append({'kind': 'reading_speed', 'cue': index, 'characters_per_second': round(cps, 2)})
        previous_end = end
    if [word_id for cue in cues for word_id in cue['word_ids']] != [word['id'] for word in words]:
        raise SubtitleError('Subtitle formatting dropped or duplicated words')
    return flags


def timestamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}'


def render_srt(cues):
    return '\n\n'.join(f"{index}\n{timestamp(cue['start'])} --> {timestamp(cue['end'])}\n{cue['text']}"
                        for index, cue in enumerate(cues, 1)) + '\n'


def valid_srt(path):
    """Strict structural check, independent of auth, extraction and checkpoints."""
    try:
        text = Path(path).read_text(encoding='utf-8-sig').replace('\r\n', '\n').strip()
        blocks = re.split(r'\n\s*\n', text)
        previous_end = 0
        pattern = r'(\d{2,}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2,}):(\d{2}):(\d{2}),(\d{3})'
        for index, block in enumerate(blocks, 1):
            lines = block.splitlines()
            if len(lines) not in (3, 4) or lines[0] != str(index):
                return False
            match = re.fullmatch(pattern, lines[1])
            if not match or any(not line.strip() or len(line) > 42 for line in lines[2:]):
                return False
            values = list(map(int, match.groups()))
            if any(value > 59 for value in (values[1], values[2], values[5], values[6])):
                return False
            start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
            end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
            if not previous_end <= start < end:
                return False
            previous_end = end
        return bool(blocks)
    except (OSError, UnicodeError):
        return False


def usage_summary(results):
    totals = {'input_audio_tokens': 0, 'input_text_tokens': 0, 'output_text_tokens': 0,
              'prompt_tokens_reported': 0, 'output_tokens_reported': 0,
              'responses_with_usage': 0, 'responses_missing_usage': 0,
              'input_modality_breakdown_missing': 0, 'output_modality_breakdown_missing': 0}
    for data in results:
        usage = data.get('usageMetadata')
        if not usage:
            totals['responses_missing_usage'] += 1
            continue
        totals['responses_with_usage'] += 1
        totals['prompt_tokens_reported'] += usage.get('promptTokenCount', 0)
        totals['output_tokens_reported'] += usage.get('candidatesTokenCount', 0)
        prompt_details = usage.get('promptTokensDetails', [])
        output_details = usage.get('candidatesTokensDetails', [])
        if not prompt_details:
            totals['input_modality_breakdown_missing'] += 1
        if not output_details:
            totals['output_modality_breakdown_missing'] += 1
        for detail in prompt_details:
            if detail.get('modality') == 'AUDIO':
                totals['input_audio_tokens'] += detail.get('tokenCount', 0)
            elif detail.get('modality') == 'TEXT':
                totals['input_text_tokens'] += detail.get('tokenCount', 0)
        for detail in output_details:
            if detail.get('modality') == 'TEXT':
                totals['output_text_tokens'] += detail.get('tokenCount', 0)
    return totals


def empty_result(video, output, configuration):
    return {'source_name': video.name, 'output': str(output), 'status': 'failed',
            'model': configuration['model'], 'settings': configuration,
            'cue_count': 0, 'word_count': 0, 'corrections': [], 'review_flags': [],
            'usage': usage_summary([])}


def resolved_configuration(video, args, pool, root):
    """Pin a discovered model for this source so restarts reuse paid checkpoints."""
    configuration = settings(args)
    if args.model != 'auto':
        return configuration
    stat = video.stat()
    signature = {'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    selected_path = root / 'model-selection.json'
    if selected_path.exists():
        selected = read_json(selected_path)
        if selected.get('signature') == signature and selected.get('requested_settings') == configuration:
            configuration['model'] = selected['resolved_model']
            return configuration
    if args.format_only:
        raise SubtitleError('No cached automatic model selection; pass the original --model in format-only mode')
    model = pool.resolve_model(args.location, args.model)
    if not isinstance(model, str) or not model or model == 'auto':
        raise SubtitleError('Model discovery did not select a transcription model')
    atomic_json(selected_path, {'signature': signature, 'requested_settings': configuration,
                                'resolved_model': model})
    configuration['model'] = model
    return configuration


def destination(video, args):
    return args.output or Path(video).with_suffix('.' + (args.output_language or 'source') + '.srt')


def process_episode(video, args, pool):
    video = Path(video).resolve()
    output = destination(video, args).resolve()
    configuration = settings(args)
    result = empty_result(video, output, configuration)
    root, cache = source_root(video, args.cache_dir), None
    try:
        with source_lock(root):
            try:
                if output.exists() and not args.overwrite:
                    if not valid_srt(output):
                        raise SubtitleError('Subtitle destination exists but is invalid; use --overwrite only after review')
                    output_digest = hashlib.sha256(output.read_bytes()).hexdigest()
                    prior_path = root / 'result.json'
                    prior = read_json(prior_path) if prior_path.exists() else {}
                    if prior.get('output_sha256') == output_digest and prior.get('output') == str(output):
                        result.update(prior)
                    else:
                        result.update(cue_count=len(re.split(r'\n\s*\n', output.read_text().strip())), word_count=None)
                    result.update(status='skipped', reason='existing_valid_srt', output_sha256=output_digest)
                    atomic_json(root / 'result.json', result)
                    return result
                if not video.is_file():
                    raise SubtitleError('Input video does not exist')
                configuration = resolved_configuration(video, args, pool, root)
                result.update(model=configuration['model'], settings=configuration)
                identity, cache = identity_for(video, root, configuration)
                cache.mkdir(parents=True, exist_ok=True)
                atomic_json(cache / 'identity.json', identity)
                result['cache'] = str(cache)
                if args.format_only:
                    preparation = read_json(cache / 'preparation.json')
                else:
                    preparation = prepare_audio(video, cache)
                duration = preparation['duration']
                result['duration_seconds'] = duration
                chunks = preparation['chunks']
                results = {}
                missing = []
                # Validate all saved state before submitting any new paid request.
                for chunk in chunks:
                    index = chunk['index']
                    checkpoint = cache / f'chunk-{index:03}.json'
                    if checkpoint.exists():
                        data = read_json(checkpoint)
                        timed_words(data, chunk['end'] - chunk['start'])
                        results[index] = data
                    elif args.format_only:
                        raise SubtitleError('Missing transcript checkpoint in format-only mode')
                    elif checkpoint.with_suffix('.inflight.json').exists():
                        raise SubtitleError('Previous request outcome is uncertain; review its inflight checkpoint before retrying')
                    else:
                        missing.append(chunk)
                with ThreadPoolExecutor(max_workers=args.request_workers) as workers:
                    futures = {}
                    for chunk in missing:
                        index = chunk['index']
                        checkpoint = cache / f'chunk-{index:03}.json'
                        clip = chunk_audio(cache / 'audio.wav', chunk)
                        future = workers.submit(pool.transcribe, clip, checkpoint,
                                                chunk['end'] - chunk['start'], configuration)
                        futures[future] = index
                    for future in as_completed(futures):
                        results[futures[future]] = future.result()
                result['usage'] = usage_summary(results.values())
                words = []
                for chunk in chunks:
                    index = chunk['index']
                    for word in timed_words(results[index], chunk['end'] - chunk['start']):
                        word.update(start=word['start'] + chunk['start'], end=word['end'] + chunk['start'],
                                    speaker=f"{index}:{word['speaker']}", id=len(words))
                        words.append(word)
                corrections, flags = repair_long_words(words, preparation['silences'])
                words.sort(key=lambda word: (word['start'], word['id']))
                atomic_json(cache / 'words.json', words)
                result.update(word_count=len(words), corrections=corrections, review_flags=flags)
                cues = make_cues(words, duration)
                result['review_flags'].extend(validate_cues(cues, words, duration))
                atomic_json(cache / 'cues.json', cues)
                text = render_srt(cues)
                # Validate the actual serialization before publishing final output.
                check = cache / 'validated.srt'
                atomic_bytes(check, text.encode('utf-8'))
                if not valid_srt(check):
                    raise SubtitleError('Serialized subtitle failed structural validation')
                command(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(check)])
                output.parent.mkdir(parents=True, exist_ok=True)
                if args.overwrite:
                    atomic_bytes(output, text.encode('utf-8'))
                else:
                    # Hard-link a complete file from a temporary on the destination
                    # filesystem, so another process cannot cause an overwrite race.
                    fd, temporary = tempfile.mkstemp(prefix='.' + output.name + '-', dir=output.parent)
                    try:
                        with os.fdopen(fd, 'wb') as handle:
                            handle.write(text.encode('utf-8'))
                            handle.flush()
                            os.fsync(handle.fileno())
                        try:
                            os.link(temporary, output)
                        except FileExistsError as error:
                            raise SubtitleError('Subtitle destination appeared during transcription; it was preserved') from error
                    finally:
                        os.unlink(temporary)
                result.update(status='completed', cue_count=len(cues),
                              output_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())
            except Exception as error:
                result['error'] = str(error) if isinstance(error, SubtitleError) else 'Unexpected local processing failure'
            # Failed episodes include every saved response, including responses that
            # failed timed-word validation. Nothing is retranscribed to obtain usage.
            if cache and result['status'] == 'failed':
                saved = []
                for checkpoint in sorted(cache.glob('chunk-[0-9][0-9][0-9].json')):
                    try:
                        saved.append(read_json(checkpoint))
                    except SubtitleError:
                        pass
                result['usage'] = usage_summary(saved)
            if cache:
                atomic_json(cache / 'result.json', result)
            atomic_json(root / 'result.json', result)
    except Exception as error:
        result['error'] = str(error) if isinstance(error, SubtitleError) else 'Cannot access subtitle cache'
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('videos', nargs='+', type=Path)
    result.add_argument('--project')
    result.add_argument('--config', type=Path, help='Private TOML configuration (or SUBTITLE_CONFIG)')
    result.add_argument('--model', default=DEFAULT_MODEL)
    result.add_argument('--location', default='global')
    result.add_argument('--language', default='auto', help='Source language hint; auto preserves detected speech language')
    result.add_argument('--output-language', help='Confirmed source language tag for the final filename; default: source draft')
    result.add_argument('--gcloud', default='gcloud')
    result.add_argument('--cache-dir', type=Path)
    result.add_argument('--episode-workers', type=int, default=4)
    result.add_argument('--request-workers', type=int, default=4)
    result.add_argument('--format-only', action='store_true')
    result.add_argument('--dry-run', action='store_true')
    result.add_argument('--overwrite', action='store_true')
    result.add_argument('--output', type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.episode_workers < 1 or args.request_workers < 1:
        raise SubtitleError('Worker counts must be positive')
    if args.output and len(args.videos) != 1:
        raise SubtitleError('--output requires exactly one input video')
    if not re.fullmatch(r'[a-z0-9-]+', args.location):
        raise SubtitleError('Invalid location')
    if args.output_language and not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', args.output_language):
        raise SubtitleError('Output language must be a safe BCP47 language tag')
    # Determine whether paid requests could be needed without authenticating,
    # extracting audio or creating a cache. Complete SRTs are honored first.
    needs_requests = not args.format_only and any(
        args.overwrite or not destination(video, args).exists()
        for video in args.videos)
    project = select_project(args.project, config_path=args.config) if needs_requests or args.dry_run else None
    if args.dry_run:
        jobs = []
        for video in args.videos:
            output = destination(video, args)
            jobs.append({'source': str(video), 'duration_seconds': probe(video),
                         'existing_valid_srt': output.exists() and valid_srt(output)})
        print(json.dumps({'project': project, 'settings': settings(args), 'jobs': jobs,
                          'episode_workers': min(args.episode_workers, len(jobs)),
                          'request_workers': args.request_workers}, indent=2))
        return 0
    pool = RequestPool(args.request_workers, project, args.gcloud)
    results = [None] * len(args.videos)
    with ThreadPoolExecutor(max_workers=min(args.episode_workers, len(args.videos))) as workers:
        futures = {workers.submit(process_episode, video, args, pool): index
                   for index, video in enumerate(args.videos)}
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
            result = results[index]
            print(f"{result['source_name']}: {result['status']}" +
                  (f" ({result['error']})" if result.get('error') else ''), flush=True)
    summary = {'results': results, 'counts': {state: sum(r['status'] == state for r in results)
                                            for state in ('completed', 'skipped', 'failed')}}
    bases = {args.cache_dir.resolve()} if args.cache_dir else {
        video.resolve().parent / '.subtitle-cache' for video in args.videos}
    for base in bases:
        atomic_json(base / 'summary.json', summary)
    return 1 if summary['counts']['failed'] else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SubtitleError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
