#!/usr/bin/env python3
"""Deterministic, resumable source-language audio transcription and SRT formatting.

Python standard library, ffmpeg, ffprobe and authenticated gcloud only. No
general text model is used. Importable functions permit offline regression tests.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import copy
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import random as random_module
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
MAX_RETRY_DELAY = 300.0
TOKEN_REFRESH_SECONDS = 2700
HTTP_STATUSES = {'INVALID_ARGUMENT', 'UNAUTHENTICATED', 'PERMISSION_DENIED', 'NOT_FOUND',
                 'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION', 'ABORTED', 'OUT_OF_RANGE',
                 'UNIMPLEMENTED', 'INTERNAL', 'UNAVAILABLE', 'DEADLINE_EXCEEDED', 'UNKNOWN'}
HTTP_REASONS = {'RATE_LIMIT_EXCEEDED', 'QUOTA_EXCEEDED', 'RESOURCE_EXHAUSTED',
                'SERVICE_DISABLED', 'BILLING_DISABLED', 'ACCESS_TOKEN_EXPIRED',
                'AUTHENTICATION_ERROR', 'CREDENTIALS_MISSING', 'IAM_PERMISSION_DENIED'}
QUOTA_METRICS = {'aiplatform.googleapis.com/' + name for name in (
    'generate_content_requests', 'generate_content_input_tokens',
    'generate_content_output_tokens', 'generate_content_total_tokens',
    'generate_content_tokens', 'generate_content_audio_input_seconds',
    'generate_content_audio_input_tokens', 'online_prediction_requests', 'publisher_model_requests',
    'generate_content_requests_per_minute_per_project_per_base_model',
    'generate_content_input_tokens_per_minute_per_base_model',
    'generate_content_output_tokens_per_minute_per_base_model',
    'generate_content_audio_input_per_base_model_id_and_resolution',
    'generate_content_audio_input_per_base_model_id_and_resolution_global')}
QUOTA_LIMIT_PATTERN = re.compile(
    r'(?:GenerateContent|OnlinePrediction|PublisherModel)'
    r'(?:Requests|InputTokens|OutputTokens|TotalTokens|Tokens|AudioInputSeconds|AudioInputTokens)'
    r'(?:Per(?:Minute|Day|Second|Hour|Project|Region|Model|BaseModel|User|Organization|Location)){1,8}')
QUOTA_SNAKE_LIMIT_PATTERN = re.compile(
    r'(?:generate_content|online_prediction|publisher_model)_'
    r'(?:requests|input_tokens|output_tokens|total_tokens|tokens|audio_input_seconds|audio_input_tokens)'
    r'(?:_per_(?:minute|day|second|hour|project|region|model|base_model|user|organization|location)){1,8}')


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


def checkpoint_word(data, part_index, word_index):
    """Index the first candidate's exact part/word, without Python bool indices."""
    if any(type(value) is not int or value < 0 for value in (part_index, word_index)):
        raise SubtitleError('Timing override indices must be nonnegative integers')
    try:
        word = data['candidates'][0]['content']['parts'][part_index]['audioTranscription']['words'][word_index]
    except (KeyError, IndexError, TypeError):
        raise SubtitleError('Timing override word index does not identify a transcript word') from None
    if not isinstance(word, dict):
        raise SubtitleError('Timing override target is not a transcript word')
    return word


def same_timing_word(original, anchor):
    """Ignore only trailing sentence punctuation in a bounded timing anchor."""
    if not isinstance(original, str) or not isinstance(anchor, str):
        return False
    original, anchor = original.rstrip('.,;:!?'), anchor.rstrip('.,;:!?')
    return bool(original) and original == anchor


def timing_number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise SubtitleError('Timing override times must be finite numbers')
    return value


def reviewed_exclusion(data, path, raw_bytes, duration):
    """Remove only a verified terminal malformed speaker token in a private copy."""
    overlay_path = path.with_suffix('.word-exclusions.json')
    overlay = read_json(overlay_path)
    if (not isinstance(overlay, dict) or set(overlay) != {'version', 'source_sha256', 'exclusions'}
            or type(overlay['version']) is not int or overlay['version'] != 1
            or overlay['source_sha256'] != hashlib.sha256(raw_bytes).hexdigest()
            or not isinstance(overlay['exclusions'], list) or len(overlay['exclusions']) != 1):
        raise SubtitleError('Word exclusion schema or raw checksum is invalid')
    entry = overlay['exclusions'][0]
    if not isinstance(entry, dict) or set(entry) != {'part_index', 'word_index', 'raw_word', 'reviewer', 'reason', 'evidence'}:
        raise SubtitleError('Word exclusion entry schema is invalid')
    target = checkpoint_word(data, entry['part_index'], entry['word_index'])
    if (not isinstance(entry['raw_word'], dict)
            or json.dumps(entry['raw_word'], sort_keys=True) != json.dumps(target, sort_keys=True)
            or not isinstance(target.get('word'), str) or re.fullmatch(r'uid:[0-9]+', target['word']) is None
            or 'endOffset' in target):
        raise SubtitleError('Word exclusion requires the exact malformed speaker token')
    start = offset(target.get('startOffset'))
    if not 0 <= start <= duration:
        raise SubtitleError('Word exclusion token start is outside the section')
    parts = data['candidates'][0]['content']['parts']
    indexed = [(pi, wi, word) for pi, part in enumerate(parts)
               for wi, word in enumerate(part.get('audioTranscription', {}).get('words', []))]
    if len(indexed) < 4 or indexed[-1][:2] != (entry['part_index'], entry['word_index']):
        raise SubtitleError('Word exclusion target must be the terminal timed-word entry')
    if any(not isinstance(entry[field], str) or not entry[field].strip() for field in ('reviewer', 'reason')):
        raise SubtitleError('Word exclusion reviewer and reason are required')
    evidence = entry['evidence']
    if not isinstance(evidence, dict) or set(evidence) != {'recheck_path', 'recheck_sha256', 'clip_start_seconds', 'clip_end_seconds'}:
        raise SubtitleError('Word exclusion bounded evidence is incomplete')
    clip_start, clip_end = (timing_number(evidence[field]) for field in ('clip_start_seconds', 'clip_end_seconds'))
    if (not 0 <= clip_start < clip_end <= duration or clip_end - clip_start > 60
            or not math.isclose(clip_end, duration, rel_tol=0, abs_tol=1e-6)
            or not clip_start <= start <= clip_end):
        raise SubtitleError('Word exclusion requires a bounded clip covering the section end and token')
    if (not isinstance(evidence['recheck_path'], str) or not Path(evidence['recheck_path']).is_absolute()
            or Path(evidence['recheck_path']).suffix != '.json' or not isinstance(evidence['recheck_sha256'], str)):
        raise SubtitleError('Word exclusion requires an absolute raw recheck JSON path and checksum')
    recheck_bytes = Path(evidence['recheck_path']).read_bytes()
    if evidence['recheck_sha256'] != hashlib.sha256(recheck_bytes).hexdigest():
        raise SubtitleError('Word exclusion raw recheck checksum changed')
    recheck = json.loads(recheck_bytes)
    checked = timed_words(recheck, clip_end - clip_start)
    for part in recheck['candidates'][0]['content']['parts']:
        transcript = part.get('audioTranscription', {})
        if any(isinstance(value, str) and re.search(r'(?<![\w:])uid:[0-9]+(?![\w:])', value)
               for value in (part.get('text'), transcript.get('text'))):
            raise SubtitleError('Word exclusion recheck text contains a speaker token')
        for word in part.get('audioTranscription', {}).get('words', []):
            if (not 0 <= offset(word['startOffset']) <= offset(word['endOffset']) <= clip_end - clip_start
                    or re.fullmatch(r'uid:[0-9]+', word['word']) is not None):
                raise SubtitleError('Word exclusion recheck has invalid timing or a speaker token')
    preceding = [item[2] for item in indexed[-4:-1]]
    if (len(checked) < 3 or any(not same_timing_word(original.get('word'), anchor['word'])
            or not clip_start <= offset(original.get('startOffset')) <= clip_end
            for original, anchor in zip(preceding, checked[-3:]))):
        raise SubtitleError('Word exclusion lacks the final three matching audio anchors')
    copied = copy.deepcopy(data)
    del copied['candidates'][0]['content']['parts'][entry['part_index']]['audioTranscription']['words'][entry['word_index']]
    return copied, {'kind': 'reviewed_word_exclusion', **copy.deepcopy(entry),
                    'checkpoint': str(path.resolve()), 'overlay': str(overlay_path.resolve()),
                    'source_sha256': overlay['source_sha256']}


def reviewed_join(data, path, raw_bytes, duration):
    """Join one audio-verified malformed decimal/percent pair in a private copy."""
    overlay_path = path.with_suffix('.word-joins.json')
    overlay = read_json(overlay_path)
    if (not isinstance(overlay, dict) or set(overlay) != {'version', 'source_sha256', 'joins'}
            or type(overlay['version']) is not int or overlay['version'] != 1
            or overlay['source_sha256'] != hashlib.sha256(raw_bytes).hexdigest()
            or not isinstance(overlay['joins'], list) or len(overlay['joins']) != 1):
        raise SubtitleError('Word join schema or raw checksum is invalid')
    entry = overlay['joins'][0]
    keys = {'part_index', 'word_index', 'raw_words', 'start_seconds', 'end_seconds', 'reviewer', 'reason', 'evidence'}
    if not isinstance(entry, dict) or set(entry) != keys:
        raise SubtitleError('Word join entry schema is invalid')
    left = checkpoint_word(data, entry['part_index'], entry['word_index'])
    right = checkpoint_word(data, entry['part_index'], entry['word_index'] + 1)
    if (not isinstance(entry['raw_words'], list) or len(entry['raw_words']) != 2
            or json.dumps(entry['raw_words'], sort_keys=True) != json.dumps([left, right], sort_keys=True)
            or not isinstance(left.get('word'), str) or re.fullmatch(r'[0-9]+(?:[.,][0-9]+)?', left['word']) is None
            or right.get('word') != '%'):
        raise SubtitleError('Word join requires exact adjacent decimal and percent identities')
    left_start, left_end = offset(left.get('startOffset')), offset(left.get('endOffset'))
    right_start, right_end = offset(right.get('startOffset')), offset(right.get('endOffset'))
    transcript = data['candidates'][0]['content']['parts'][entry['part_index']]['audioTranscription']
    if (not 0 <= left_start <= left_end <= duration or not 0 <= right_start <= duration
            or not right_end < right_start
            or not math.isclose(left_end, right_start, rel_tol=0, abs_tol=1e-6)
            or str(left.get('speakerLabel', transcript.get('speakerLabel', 'narrator')))
               != str(right.get('speakerLabel', transcript.get('speakerLabel', 'narrator')))):
        raise SubtitleError('Word join requires a touching same-speaker pair with reversed percent timing')
    if any(not isinstance(entry[field], str) or not entry[field].strip() for field in ('reviewer', 'reason')):
        raise SubtitleError('Word join reviewer and reason are required')
    start, end = (timing_number(entry[field]) for field in ('start_seconds', 'end_seconds'))
    if not 0 <= start < end <= duration:
        raise SubtitleError('Word join endpoints must be positive and within the section')
    evidence = entry['evidence']
    if not isinstance(evidence, dict) or set(evidence) != {'recheck_path', 'recheck_sha256', 'clip_start_seconds', 'clip_end_seconds', 'recheck_part_index', 'recheck_word_index'}:
        raise SubtitleError('Word join bounded evidence is incomplete')
    clip_start, clip_end = (timing_number(evidence[field]) for field in ('clip_start_seconds', 'clip_end_seconds'))
    if (not 0 <= clip_start < clip_end <= duration or clip_end - clip_start > 60
            or not clip_start <= left_start <= clip_end or not clip_start <= right_start <= clip_end):
        raise SubtitleError('Word join requires a bounded clip covering both raw starts')
    if (not isinstance(evidence['recheck_path'], str) or not Path(evidence['recheck_path']).is_absolute()
            or Path(evidence['recheck_path']).suffix != '.json' or not isinstance(evidence['recheck_sha256'], str)):
        raise SubtitleError('Word join requires an absolute raw recheck JSON path and checksum')
    recheck_bytes = Path(evidence['recheck_path']).read_bytes()
    if evidence['recheck_sha256'] != hashlib.sha256(recheck_bytes).hexdigest():
        raise SubtitleError('Word join raw recheck checksum changed')
    recheck = json.loads(recheck_bytes)
    timed_words(recheck, clip_end - clip_start)
    for part in recheck['candidates'][0]['content']['parts']:
        for word in part.get('audioTranscription', {}).get('words', []):
            if not 0 <= offset(word['startOffset']) <= offset(word['endOffset']) <= clip_end - clip_start:
                raise SubtitleError('Word join recheck timestamp is outside the exact clip')
    anchor = checkpoint_word(recheck, evidence['recheck_part_index'], evidence['recheck_word_index'])
    merged = left['word'] + '%'
    if (not same_timing_word(merged, anchor.get('word'))
            or not math.isclose(start, clip_start + offset(anchor.get('startOffset')), rel_tol=0, abs_tol=1e-6)
            or not math.isclose(end, clip_start + offset(anchor.get('endOffset')), rel_tol=0, abs_tol=1e-6)):
        raise SubtitleError('Word join lacks exact merged-word audio endpoint support')
    copied = copy.deepcopy(data)
    words = copied['candidates'][0]['content']['parts'][entry['part_index']]['audioTranscription']['words']
    words[entry['word_index']].update(word=merged, startOffset=start, endOffset=end)
    del words[entry['word_index'] + 1]
    parts = data['candidates'][0]['content']['parts']
    local_index = sum(len(part.get('audioTranscription', {}).get('words', []))
                      for part in parts[:entry['part_index']]) + entry['word_index']
    return copied, {'kind': 'reviewed_word_join', **copy.deepcopy(entry),
                    'checkpoint': str(path.resolve()), 'overlay': str(overlay_path.resolve()),
                    'source_sha256': overlay['source_sha256'], 'local_word_index': local_index}


def read_checkpoint(path, duration, include_corrections=False):
    """Validate immutable raw JSON, applying only explicitly verified private overlays.

    By default returns transcript data. include_corrections returns (data, audit).
    The audit contains private paths and reviewer evidence; never publish it.
    """
    try:
        return _read_checkpoint(path, duration, include_corrections)
    except SubtitleError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError, RecursionError):
        raise SubtitleError('Malformed transcript checkpoint or timing override') from None


def _read_checkpoint(path, duration, include_corrections):
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
        data = json.loads(raw_bytes)
    except (OSError, ValueError, UnicodeError):
        raise SubtitleError('Missing or invalid transcript checkpoint') from None
    adjustments = []
    overlay_path = path.with_suffix('.timing-overrides.json')
    exclusion_path = path.with_suffix('.word-exclusions.json')
    join_path = path.with_suffix('.word-joins.json')
    if sum(item.exists() for item in (overlay_path, exclusion_path, join_path)) > 1:
        raise SubtitleError('Reviewed overlays cannot coexist on one checkpoint')
    if join_path.exists():
        data, adjustment = reviewed_join(data, path, raw_bytes, duration)
        adjustments.append(adjustment)
    if exclusion_path.exists():
        data, adjustment = reviewed_exclusion(data, path, raw_bytes, duration)
        adjustments.append(adjustment)
    if overlay_path.exists():
        overlay = read_json(overlay_path)
        if (not isinstance(overlay, dict) or set(overlay) != {'version', 'source_sha256', 'corrections'}
                or type(overlay['version']) is not int or overlay['version'] != 1
                or overlay['source_sha256'] != hashlib.sha256(raw_bytes).hexdigest()
                or not isinstance(overlay['corrections'], list) or not overlay['corrections']):
            raise SubtitleError('Timing override schema or raw checkpoint checksum is invalid')
        data = copy.deepcopy(data)
        seen = set()
        keys = {'part_index', 'word_index', 'word', 'old_start_offset', 'old_end_offset',
                'start_seconds', 'end_seconds', 'reviewer', 'reason', 'evidence'}
        evidence_keys = {'recheck_path', 'recheck_sha256', 'clip_start_seconds', 'clip_end_seconds',
                         'recheck_part_index', 'recheck_word_index'}
        for correction in overlay['corrections']:
            if not isinstance(correction, dict) or set(correction) != keys:
                raise SubtitleError('Timing override correction schema is invalid')
            word = checkpoint_word(data, correction['part_index'], correction['word_index'])
            key = (correction['part_index'], correction['word_index'])
            if key in seen:
                raise SubtitleError('Duplicate timing override word identity')
            seen.add(key)
            if (not isinstance(correction['word'], str) or not correction['word'].strip()
                    or correction['word'] != word.get('word')
                    or any(type(correction[field]) is not type(word.get(raw_field))
                           or correction[field] != word.get(raw_field)
                           for field, raw_field in (('old_start_offset', 'startOffset'),
                                                    ('old_end_offset', 'endOffset')))):
                raise SubtitleError('Timing override original word or offsets do not match')
            if any(not isinstance(correction[field], str) or not correction[field].strip()
                   for field in ('reviewer', 'reason')):
                raise SubtitleError('Timing override reviewer and reason are required')
            start, end = (timing_number(correction[field]) for field in ('start_seconds', 'end_seconds'))
            if not 0 <= start <= end <= duration:
                raise SubtitleError('Timing override endpoints are outside the section or reversed')
            evidence = correction['evidence']
            if not isinstance(evidence, dict) or set(evidence) != evidence_keys:
                raise SubtitleError('Timing override bounded audio recheck evidence is incomplete')
            clip_start, clip_end = (timing_number(evidence[field])
                                   for field in ('clip_start_seconds', 'clip_end_seconds'))
            if not 0 <= clip_start < clip_end <= duration or clip_end - clip_start > 60:
                raise SubtitleError('Timing override recheck clip must be bounded within the section')
            if (not isinstance(evidence['recheck_path'], str)
                    or not Path(evidence['recheck_path']).is_absolute()
                    or Path(evidence['recheck_path']).suffix != '.json'
                    or not isinstance(evidence['recheck_sha256'], str)):
                raise SubtitleError('Timing override requires an absolute raw recheck JSON path and checksum')
            try:
                recheck_bytes = Path(evidence['recheck_path']).read_bytes()
                recheck = json.loads(recheck_bytes)
                if evidence['recheck_sha256'] != hashlib.sha256(recheck_bytes).hexdigest():
                    raise SubtitleError('Timing override raw recheck checksum changed')
                clip_duration = clip_end - clip_start
                timed_words(recheck, clip_duration)
                # Evidence uses exact clip bounds, without the base raw validator's
                # quarter-second tolerance or end clamping.
                for part in recheck['candidates'][0]['content']['parts']:
                    for anchor in part.get('audioTranscription', {}).get('words', []):
                        if not 0 <= offset(anchor['startOffset']) <= offset(anchor['endOffset']) <= clip_duration:
                            raise SubtitleError('Timing override recheck word is outside the exact clip')
                anchor = checkpoint_word(recheck, evidence['recheck_part_index'], evidence['recheck_word_index'])
            except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError):
                raise SubtitleError('Timing override raw recheck evidence is malformed or unavailable') from None
            if (not same_timing_word(correction['word'], anchor.get('word'))
                    or not math.isclose(start, clip_start + offset(anchor.get('startOffset')), rel_tol=0, abs_tol=1e-6)
                    or not math.isclose(end, clip_start + offset(anchor.get('endOffset')), rel_tol=0, abs_tol=1e-6)):
                raise SubtitleError('Timing override endpoints or word lack matching raw recheck support')
            # The raw file and every other copied field stay untouched.
            word['startOffset'], word['endOffset'] = start, end
            parts = data['candidates'][0]['content']['parts']
            local_index = sum(len(part.get('audioTranscription', {}).get('words', []))
                              for part in parts[:correction['part_index']]) + correction['word_index']
            adjustments.append({'kind': 'reviewed_timing_override', **copy.deepcopy(correction),
                                'checkpoint': str(path.resolve()), 'overlay': str(overlay_path.resolve()),
                                'source_sha256': overlay['source_sha256'], 'local_word_index': local_index})
    try:
        timed_words(data, duration)
    except (TypeError, KeyError, AttributeError, ValueError):
        raise SubtitleError('Malformed transcript checkpoint') from None
    return (data, adjustments) if include_corrections else data


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


def bounded_delay(value):
    try:
        number = float(value)
        return min(number, MAX_RETRY_DELAY) if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def http_diagnostic(error, wall_clock=time.time):
    """Read a bounded body in memory; return only positively allowlisted fields."""
    diagnostic = {'http_status': error.code}
    retry_delays = []
    header = error.headers.get('Retry-After') if error.headers else None
    delay = bounded_delay(header)
    if delay is None and header:
        try:
            date = parsedate_to_datetime(header)
            delay = bounded_delay(max(0, date.timestamp() - wall_clock()))
        except (TypeError, ValueError, OverflowError):
            pass
    if delay is not None:
        retry_delays.append(delay)
    try:
        raw = error.read(65537)
        data = json.loads(raw) if len(raw) <= 65536 else {}
        api = data.get('error', {}) if isinstance(data, dict) else {}
        if not isinstance(api, dict):
            api = {}
    except (OSError, ValueError, TypeError, AttributeError):
        api = {}
    status = api.get('status')
    if isinstance(status, str) and status in HTTP_STATUSES:
        diagnostic['status'] = status
    details = api.get('details', [])
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        if detail.get('@type') == 'type.googleapis.com/google.rpc.RetryInfo':
            value = detail.get('retryDelay')
            if isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+)?s', value):
                delay = bounded_delay(value[:-1])
            elif isinstance(value, dict):
                try:
                    delay = bounded_delay(float(value.get('seconds', 0)) + float(value.get('nanos', 0)) / 1e9)
                except (TypeError, ValueError, OverflowError):
                    delay = None
            else:
                delay = None
            if delay is not None:
                retry_delays.append(delay)
        if detail.get('@type') == 'type.googleapis.com/google.rpc.ErrorInfo':
            reason = detail.get('reason')
            if isinstance(reason, str) and reason in HTTP_REASONS:
                diagnostic['reason'] = reason
            metadata = detail.get('metadata', {})
            if not isinstance(metadata, dict):
                continue
            metric, limit = metadata.get('quota_metric'), metadata.get('quota_limit')
            if isinstance(metric, str) and metric in QUOTA_METRICS:
                diagnostic['quota_metric'] = metric
            if isinstance(limit, str) and (QUOTA_LIMIT_PATTERN.fullmatch(limit) or QUOTA_SNAKE_LIMIT_PATTERN.fullmatch(limit)):
                diagnostic['quota_limit'] = limit
    # Messages are never retained; only known phrases classify the rejection.
    message = api.get('message', '')
    message = message.lower() if isinstance(message, str) else ''
    if 'quota_metric' not in diagnostic:
        for metric in sorted(QUOTA_METRICS):
            if metric in message:
                diagnostic['quota_metric'] = metric
                break
    if 'quota_limit' not in diagnostic:
        original = api.get('message', '')
        if isinstance(original, str):
            for match in re.finditer(r"(?:quota )?limit ['\"]([^'\"]+)['\"]", original, re.IGNORECASE):
                name = match.group(1)
                if QUOTA_LIMIT_PATTERN.fullmatch(name) or QUOTA_SNAKE_LIMIT_PATTERN.fullmatch(name):
                    diagnostic['quota_limit'] = name
                    break
    if diagnostic.get('quota_metric'):
        metric = diagnostic['quota_metric']
        category = 'request_quota' if 'requests' in metric else 'token_quota' if 'tokens' in metric else 'audio_quota'
    elif diagnostic.get('reason') == 'QUOTA_EXCEEDED' or any(
            phrase in message for phrase in ('quota exceeded', 'exceeded your quota', 'quota limit')):
        category = 'quota_exceeded'
    elif diagnostic.get('reason') == 'RATE_LIMIT_EXCEEDED' or any(
            phrase in message for phrase in ('rate limit', 'too many requests')):
        category = 'rate_limited'
    elif any(phrase in message for phrase in ('model overloaded', 'service overloaded', 'out of capacity', 'no available capacity')):
        category = 'service_capacity'
    elif error.code == 429:
        category = 'resource_exhausted_unspecified'
    elif 500 <= error.code <= 599:
        category = 'server_error'
    elif error.code == 401:
        category = 'authentication_rejected'
    else:
        category = 'request_rejected'
    if category.endswith('quota') or category == 'quota_exceeded':
        if 'PerDay' in diagnostic.get('quota_limit', '') or '_per_day' in diagnostic.get('quota_limit', ''):
            category = 'daily_quota'
    diagnostic['category'] = category
    if retry_delays:
        diagnostic['retry_after_seconds'] = max(retry_delays)
    return diagnostic


def diagnostic_text(diagnostic):
    return '; '.join(f'{key}={diagnostic[key]}' for key in (
        'http_status', 'status', 'reason', 'category', 'quota_metric', 'quota_limit', 'retry_after_seconds')
                     if key in diagnostic)


def request_audit(path):
    return read_json(path) if path.exists() else {
        'attempts': 0, 'rejections': 0, 'retries': 0, 'uncertain_outcomes': 0,
        'rejection_counts': {}, 'diagnostics': []}


class RequestPool:
    """One pool shared by all episode/section threads in this runner process."""
    def __init__(self, workers, project=None, gcloud='gcloud', request=None, sleep=None,
                 clock=None, random=None, wall_clock=None):
        self.limit = threading.BoundedSemaphore(workers)
        self.project, self.gcloud = project, gcloud
        self.request = request or request_json
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.random = random or random_module.random
        self.wall_clock = wall_clock or time.time
        self._token = None
        self._token_acquired_at = None
        self._token_lock = threading.Lock()
        self._models = {}
        self._model_lock = threading.Lock()
        self._cooldown_lock = threading.Lock()
        self._cooldown_until = 0.0
        self._fatal_error = None
        self._fatal_lock = threading.Lock()

    def wait_for_cooldown(self):
        while True:
            with self._cooldown_lock:
                remaining = self._cooldown_until - self.clock()
            if remaining <= 0:
                return
            # Short slices respond to a concurrent extension without an unbounded
            # sleep. Offline tests inject a clock-advancing sleeper, never a no-op.
            self.sleep(min(remaining, 60.0))

    def cooldown(self, seconds):
        with self._cooldown_lock:
            self._cooldown_until = max(self._cooldown_until, self.clock() + seconds)

    def retry_delay(self, attempt, diagnostic):
        jitter = min(1.0, max(0.0, self.random()))
        exponential = 60 * (2 ** attempt) * (1 + 0.2 * jitter)
        return min(MAX_RETRY_DELAY, max(exponential, diagnostic.get('retry_after_seconds', 0)))

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
            if self._token is None or (self._token_acquired_at is not None and
                                      self.clock() - self._token_acquired_at >= TOKEN_REFRESH_SECONDS):
                env = dict(os.environ, CLOUDSDK_CORE_DISABLE_FILE_LOGGING='true',
                           CLOUDSDK_CORE_DISABLE_USAGE_REPORTING='true')
                try:
                    # gcloud may otherwise return an old cached token with little
                    # lifetime left. This acquisition timestamp must mean fresh auth.
                    result = subprocess.run([self.gcloud, 'config', 'config-helper',
                                             '--force-auth-refresh', '--format=value(credential.access_token)'],
                                            env=env, capture_output=True, text=True, check=True)
                except (OSError, subprocess.CalledProcessError) as error:
                    raise SubtitleError('gcloud authentication failed') from error
                self._token = result.stdout.strip()
                if not self._token:
                    raise SubtitleError('gcloud returned no access token')
                self._token_acquired_at = self.clock()
            return self._token

    def assert_running(self):
        with self._fatal_lock:
            if self._fatal_error is not None:
                raise SubtitleError(self._fatal_error)

    def checked_checkpoint(self, path, duration):
        try:
            return read_checkpoint(path, duration)
        except SubtitleError:
            with self._fatal_lock:
                self._fatal_error = 'API request pool stopped after transcript validation failed; inspect its checkpoint'
            raise

    def transcribe(self, audio, output, duration, configuration):
        if (output.exists() or output.with_suffix('.timing-overrides.json').exists()
                or output.with_suffix('.word-exclusions.json').exists()
                or output.with_suffix('.word-joins.json').exists()):
            return self.checked_checkpoint(output, duration)
        marker = output.with_suffix('.inflight.json')
        if marker.exists():
            raise SubtitleError('Previous request outcome is uncertain; review its inflight checkpoint before retrying')
        location = configuration['location']
        host = 'aiplatform.googleapis.com' if location == 'global' else location + '-aiplatform.googleapis.com'
        project = urllib.parse.quote(self.project or '', safe='')
        model = urllib.parse.quote(configuration['model'], safe='')
        url = f'https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent'
        payload = payload_for(audio, configuration)
        audit_path = output.with_suffix('.requests.json')
        audit = request_audit(audit_path)
        for attempt in range(3):
            with self.limit:
                while True:
                    self.assert_running()
                    self.wait_for_cooldown()
                    self.assert_running()
                    token = self.token()  # Refresh proactively, before submitting audio.
                    self.assert_running()
                    with self._cooldown_lock:
                        # Authentication can outlast another thread's rejection.
                        if self.clock() < self._cooldown_until:
                            continue
                        atomic_json(marker, {'state': 'request-started', 'attempt': attempt + 1})
                        audit['attempts'] += 1
                        audit['retries'] += int(attempt > 0)
                        audit['last_outcome'] = 'request_started'
                        atomic_json(audit_path, audit)
                        break
                try:
                    data = self.request(url, payload, token)
                except urllib.error.HTTPError as error:
                    # A known HTTP rejection may be retried. Unknown outcomes retain
                    # the marker so a restart cannot silently repeat the paid request.
                    retryable = error.code == 429 or 500 <= error.code <= 599
                    try:
                        diagnostic = http_diagnostic(error, self.wall_clock)
                    finally:
                        error.close()
                    diagnostic.update(attempt=audit['attempts'], retry_scheduled=retryable and attempt < 2)
                    if retryable:
                        diagnostic['cooldown_seconds'] = self.retry_delay(attempt, diagnostic)
                        self.cooldown(diagnostic['cooldown_seconds'])
                    audit['rejections'] += 1
                    key = str(error.code)
                    audit['rejection_counts'][key] = audit['rejection_counts'].get(key, 0) + 1
                    audit['diagnostics'].append(diagnostic)
                    audit['last_outcome'] = 'rejected'
                    atomic_json(audit_path, audit)
                    marker.unlink(missing_ok=True)
                    text = diagnostic_text(diagnostic)
                    wait = f"; retry after shared cooldown {diagnostic['cooldown_seconds']:.1f}s" if diagnostic['retry_scheduled'] else '; no further retry'
                    print(f'API rejection: {text}; attempt {attempt + 1}/3{wait}', flush=True)
                    if error.code == 401:
                        with self._token_lock:
                            if self._token == token:
                                self._token = None
                    if diagnostic['retry_scheduled']:
                        continue
                    with self._fatal_lock:
                        self._fatal_error = f'API request pool stopped after terminal rejection: {text}'
                    raise SubtitleError(f'API request failed: {text}') from None
                except Exception as error:
                    audit['uncertain_outcomes'] += 1
                    audit['last_outcome'] = 'uncertain'
                    atomic_json(audit_path, audit)
                    with self._fatal_lock:
                        self._fatal_error = 'API request pool stopped after an uncertain outcome; inspect its checkpoint'
                    raise SubtitleError('API request outcome is uncertain; request was not replayed') from None
                # Keep the raw response even if validation fails: never pay twice
                # merely because formatting or API validation failed after success.
                atomic_json(output, data)
                audit['last_outcome'] = 'succeeded'
                atomic_json(audit_path, audit)
                marker.unlink(missing_ok=True)
                return self.checked_checkpoint(output, duration)


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
            # Speaker IDs are section-local. Do not invent a speaker change by
            # combining independent diarization sections into one dialogue cue.
            if group[-1].get('section') != group[0].get('section'):
                break
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


def repair_long_words(words, silences, reviewed_word_ids=None):
    corrections, flags = [], []
    reviewed_word_ids = set(reviewed_word_ids or ())
    for word in words:
        interval = word['end'] - word['start']
        if interval < 0.04 or interval > 2:
            flags.append({'kind': 'implausible_word_interval', 'word_id': word['id'], 'seconds': interval})
    # IDs retain raw transcript order even after callers sort by timestamp.
    # Work backwards so a later supported regression stops masquerading as
    # genuinely intervening speech during an earlier word's repair.
    raw_order = sorted(words, key=lambda word: word['id'])
    raw_ids = [word['id'] for word in raw_order]
    sequence_valid = (all(type(value) is int for value in raw_ids)
                      and len(set(raw_ids)) == len(raw_ids))
    positions = {word['id']: index for index, word in enumerate(raw_order)}
    for word in reversed(raw_order):
        if word['id'] in reviewed_word_ids or word['end'] - word['start'] <= 2:
            continue
        pauses = [pause for pause in silences if pause['end'] - pause['start'] >= 0.35
                  and word['start'] <= pause['start'] < pause['end'] < word['end']
                  and 0.04 <= word['end'] - pause['end'] <= 1.5]
        if not pauses:
            continue
        pause = max(pauses, key=lambda value: value['end'])
        onset = pause['end']
        blockers = [other for other in words if other['id'] != word['id']
                    and (word['start'] < other['start'] < onset
                         or other['start'] <= word['start'] < other['end']
                         or (sequence_valid and other['id'] > word['id'] and other['start'] < onset))]
        evidence = {'kind': 'measured_silence', 'pause': dict(pause)}
        # Ordinarily, never infer through another word's measured speech.
        # The narrow exception requires raw-order regression, same-speaker
        # neighbors and all intersecting intervals ending before this onset.
        if blockers:
            index = positions[word['id']]
            if not sequence_valid or word.get('section') is None or not 0 < index < len(raw_order) - 1:
                continue
            previous, following = raw_order[index - 1], raw_order[index + 1]
            same_track = lambda other: (other.get('section') == word['section']
                                        and other['speaker'] == word['speaker'])
            if (not same_track(previous) or not same_track(following)
                    or not word['start'] < previous['end'] <= onset <= following['start']):
                continue
            intervening = [other for other in words if other['id'] != word['id']
                           and other['start'] < onset
                           and (other['end'] > word['start'] or other in blockers)]
            if any(other['id'] >= word['id'] or not same_track(other) or other['end'] > onset
                   for other in intervening):
                continue
            evidence.update(kind='same_speaker_raw_order_regression',
                            previous_word_id=previous['id'], previous_end=previous['end'],
                            following_word_id=following['id'], following_start=following['start'],
                            intervening_word_ids=[other['id'] for other in intervening])
        word['original_start'] = word['start']
        word['start'] = onset
        word['timing_note'] = 'Start adjusted to measured silence end; review inferred onset.'
        correction = {'kind': 'measured_pause_onset', 'word_id': word['id'],
                      'original_start': word['original_start'], 'start': onset, 'evidence': evidence}
        corrections.append(correction)
        flags.append({'kind': 'inferred_onset', 'word_id': word['id'],
                      'original_start': word['original_start'], 'start': onset, 'evidence': evidence})
        print(f"Timing correction for word {word['id']}: {word['original_start']:.3f}s to {onset:.3f}s", flush=True)
    return sorted(corrections, key=lambda value: value['word_id']), flags


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


def request_summary(cache):
    totals = {'attempts': 0, 'rejections': 0, 'retries': 0, 'uncertain_outcomes': 0,
              'rejection_counts': {}, 'chunks': [], 'invalid_audit_files': 0}
    if cache is None:
        return totals
    for path in sorted(cache.glob('chunk-[0-9][0-9][0-9].requests.json')):
        try:
            audit = request_audit(path)
            for name in ('attempts', 'rejections', 'retries', 'uncertain_outcomes'):
                totals[name] += audit[name]
            for status, count in audit['rejection_counts'].items():
                totals['rejection_counts'][status] = totals['rejection_counts'].get(status, 0) + count
            totals['chunks'].append({'chunk': path.name.removesuffix('.requests.json'), **audit})
        except (SubtitleError, KeyError, TypeError, ValueError):
            totals['invalid_audit_files'] += 1
    return totals


def empty_result(video, output, configuration):
    return {'source_name': video.name, 'output': str(output) if output else None, 'status': 'failed',
            'model': configuration['model'], 'settings': configuration,
            'cue_count': 0, 'word_count': 0, 'corrections': [], 'review_flags': [],
            'usage': usage_summary([]), 'requests': request_summary(None)}


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


def valid_language_tag(value):
    return isinstance(value, str) and value.lower() not in {'auto', 'source', 'unknown', 'und'} and bool(
        re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', value))


def metadata_path(video):
    return Path(video).with_suffix('.subtitles.json5')


def read_metadata(video):
    """Metadata uses strict JSON syntax, a JSON5 subset with no dependency."""
    path = metadata_path(video)
    if not path.exists():
        return {}
    try:
        def reject_constant(_):
            raise ValueError('Non-finite metadata value')
        data = json.loads(path.read_text(encoding='utf-8'), parse_constant=reject_constant)
        json.dumps(data, allow_nan=False)  # Also reject overflowing numeric literals in nested fields.
        if not isinstance(data, dict):
            raise ValueError('Expected metadata object')
        language = data.get('original_language')
        if language is not None and not valid_language_tag(language):
            raise ValueError('Invalid native language')
        name = data.get('original_subtitle')
        if name is not None and (not isinstance(name, str) or not re.fullmatch(r'[^\x00-\x1f/\\]+\.srt', name)
                                 or name.lower() == 'source.srt' or name.lower().endswith('.source.srt')):
            raise ValueError('Expected subtitle basename')
        return data
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise SubtitleError('Malformed subtitle metadata; use a JSON object with a native language tag and subtitle basename') from None


def publication(video, args, metadata=None):
    """Choose a native target without guessing language or following external paths."""
    metadata = read_metadata(video) if metadata is None else metadata
    language = args.output_language or (args.language if args.language != 'auto' else None) or metadata.get('original_language')
    if language is not None and not valid_language_tag(language):
        raise SubtitleError('Native language must be a confirmed, safe BCP47 language tag')
    if args.output:
        output = args.output
    elif language:
        output = Path(video).with_suffix('.' + language + '.srt')
        name = metadata.get('original_subtitle')
        if metadata.get('original_language') == language and name:
            candidate = Path(video).parent / name
            if candidate.exists():
                output = candidate
    else:
        output = None
    if output and not args.output and output.resolve().parent != Path(video).resolve().parent:
        raise SubtitleError('Default native subtitle target must remain inside the media folder')
    if output and (output.name.lower() == 'source.srt' or output.name.lower().endswith('.source.srt')):
        raise SubtitleError('Unlabelled source subtitle drafts are not supported')
    return output, language


def destination(video, args):
    return publication(video, args)[0]


def write_metadata(video, output, language):
    # Re-read before updating so unrelated fields added during transcription survive.
    metadata = read_metadata(video)
    metadata.update(original_language=language, original_subtitle=output.name)
    atomic_json(metadata_path(video), metadata)


def process_episode(video, args, pool):
    video = Path(video).resolve()
    output = None
    configuration = settings(args)
    result = empty_result(video, output, configuration)
    root, cache = source_root(video, args.cache_dir), None
    try:
        with source_lock(root):
            try:
                output, language = publication(video, args)
                output = output.resolve() if output else None
                result.update(output=str(output) if output else None, original_language=language)
                if output and output.exists() and not args.overwrite:
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
                    result.update(output=str(output), original_language=language)
                    write_metadata(video, output, language)
                    result['metadata'] = str(metadata_path(video))
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
                    if (checkpoint.exists() or checkpoint.with_suffix('.timing-overrides.json').exists()
                            or checkpoint.with_suffix('.word-exclusions.json').exists()
                            or checkpoint.with_suffix('.word-joins.json').exists()):
                        data = read_checkpoint(checkpoint, chunk['end'] - chunk['start'])
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
                words, overlay_corrections = [], []
                for chunk in chunks:
                    index = chunk['index']
                    data, adjustments = read_checkpoint(cache / f'chunk-{index:03}.json',
                                                        chunk['end'] - chunk['start'], include_corrections=True)
                    for adjustment in adjustments:
                        if adjustment['kind'] == 'reviewed_word_exclusion':
                            adjustment.update(section=index, global_start=chunk['start'] + offset(adjustment['raw_word']['startOffset']))
                            continue
                        adjustment.update(section=index, word_id=len(words) + adjustment['local_word_index'],
                                          global_start=chunk['start'] + adjustment['start_seconds'],
                                          global_end=chunk['start'] + adjustment['end_seconds'])
                    overlay_corrections.extend(adjustments)
                    for word in timed_words(data, chunk['end'] - chunk['start']):
                        word.update(start=word['start'] + chunk['start'], end=word['end'] + chunk['start'],
                                    speaker=f"{index}:{word['speaker']}", section=index, id=len(words))
                        words.append(word)
                corrections, flags = repair_long_words(words, preparation['silences'],
                                                       reviewed_word_ids={item['word_id'] for item in overlay_corrections
                                                                          if item['kind'] in ('reviewed_timing_override', 'reviewed_word_join')})
                words.sort(key=lambda word: (word['start'], word['id']))
                atomic_json(cache / 'words.json', words)
                result.update(word_count=len(words), corrections=overlay_corrections + corrections,
                              review_flags=copy.deepcopy(overlay_corrections) + flags)
                cues = make_cues(words, duration)
                result['review_flags'].extend(validate_cues(cues, words, duration))
                atomic_json(cache / 'cues.json', cues)
                result['cue_count'] = len(cues)
                if output is None:
                    result.update(status='needs_language', instruction=(
                        'Inspect words.json in the reported cache to confirm the spoken language, then repeat '
                        'the same model/language settings with --format-only --output-language TAG.'))
                else:
                    text = render_srt(cues)
                    # SRT serialization exists only during validation and in the
                    # chosen final output. Timed words/cues remain the checkpoints.
                    with tempfile.TemporaryDirectory(prefix='subtitle-validation-', dir=cache) as temporary:
                        check = Path(temporary) / 'subtitle.srt'
                        check.write_text(text, encoding='utf-8')
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
                    write_metadata(video, output, language)
                    result.update(status='completed', metadata=str(metadata_path(video)),
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
                result['requests'] = request_summary(cache)
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
    result.add_argument('--output-language', help='Confirmed native language tag; otherwise use the language hint or metadata')
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
    if args.output_language and not valid_language_tag(args.output_language):
        raise SubtitleError('Output language must be a safe BCP47 language tag')
    if args.language != 'auto' and not valid_language_tag(args.language):
        raise SubtitleError('Source language hint must be a safe BCP47 language tag')
    # Determine whether paid requests could be needed without authenticating,
    # extracting audio or creating a cache. Complete SRTs are honored first.
    outputs = [destination(video, args) for video in args.videos]  # Validate every metadata file before any paid work.
    needs_requests = not args.format_only and any(
        args.overwrite or output is None or not output.exists() for output in outputs)
    project = select_project(args.project, config_path=args.config) if needs_requests or args.dry_run else None
    if args.dry_run:
        jobs = []
        for video in args.videos:
            output, language = publication(video, args)
            jobs.append({'source': str(video), 'duration_seconds': probe(video),
                         'output': str(output) if output else None, 'original_language': language,
                         'existing_valid_srt': bool(output and output.exists() and valid_srt(output))})
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
                  (f" ({result['error']})" if result.get('error') else '') +
                  (f" ({result['instruction']})" if result.get('instruction') else ''), flush=True)
    summary = {'results': results, 'counts': {state: sum(r['status'] == state for r in results)
                                            for state in ('completed', 'skipped', 'needs_language', 'failed')}}
    bases = {args.cache_dir.resolve()} if args.cache_dir else {
        video.resolve().parent / '.subtitle-cache' for video in args.videos}
    for base in bases:
        atomic_json(base / 'summary.json', summary)
    return 1 if summary['counts']['failed'] else 2 if summary['counts']['needs_language'] else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SubtitleError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
