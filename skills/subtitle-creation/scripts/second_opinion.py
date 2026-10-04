#!/usr/bin/env python3
"""Bounded independent audio wording evidence. Never edits subtitle files."""
import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import urllib.parse
from models import catalog
from transcribe import (RequestPool, SubtitleError, atomic_json, command, probe,
                        read_json, select_project, source_lock)

DEFAULT_PROMPT = Path(__file__).parent.parent / 'references' / 'clean-stt-prompt.txt'


def private_cache(cache, source_file=None):
    """Resolve symlinks and keep audio/prompt evidence outside public sources."""
    source = Path(source_file or __file__).resolve()
    public_root = source.parent.parent
    for ancestor in source.parents:
        if (ancestor/'.git').exists():
            public_root = ancestor
            break
    resolved = Path(cache).resolve()
    if resolved == public_root or public_root in resolved.parents:
        raise SubtitleError('Audio opinion cache must stay outside the public skill source')
    return resolved


def bounds(start, end, duration):
    if not all(math.isfinite(v) for v in (start, end, duration)) or not (0 <= start < end <= duration) or end-start > 60:
        raise SubtitleError('Clip must be finite, within the media, and at most 60 seconds')


def model_key(model, family):
    name = model.get('name', '').rsplit('/', 1)[-1]
    match = re.fullmatch(r'gemini-(\d+(?:\.\d+)*)-' + re.escape(family) + r'(?:-(preview|exp|experimental)(?:-(\d+(?:-\d+)*))?|-(\d+))?', name)
    if not match or model.get('versionState') in {'DEPRECATED', 'OBSOLETE', 'RETIRED'} or model.get('launchStage') in {'DEPRECATED', 'SHUTDOWN'}:
        return None
    return (tuple(map(int, match[1].split('.'))), int(match[2] is None),
            tuple(map(int, (match[3] or match[4] or '').split('-'))) if (match[3] or match[4]) else (), name)


def choose_model(models, family):
    candidates = [key for model in models if (key := model_key(model, family))]
    if not candidates:
        raise SubtitleError('No supported audio opinion model in catalog; select a verified explicit model')
    return max(candidates)[-1]


def response_text(path):
    data = read_json(path)
    if not isinstance(data, dict):
        raise SubtitleError('Malformed audio opinion response; raw response retained')
    candidates = data.get('candidates')
    if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict) or candidates[0].get('finishReason') != 'STOP':
        raise SubtitleError('Audio opinion was blocked, partial, or malformed; raw response retained')
    content = candidates[0].get('content')
    if not isinstance(content, dict) or not isinstance(content.get('parts'), list):
        raise SubtitleError('Malformed audio opinion content; raw response retained')
    parts = content['parts']
    text = '\n'.join(p['text'] for p in parts if isinstance(p, dict) and not p.get('thought') and isinstance(p.get('text'), str)).strip()
    if not text or len(text) > 65536:
        raise SubtitleError('Audio opinion text is empty or exceeds evidence bound; raw response retained')
    return text


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def opinion(media, cache, start, end, prompt, family='flash-lite', model='auto',
            project=None, location='global', pool=None, max_tokens=2048):
    """Caller may share a RequestPool with other work; each cache is one opinion."""
    if family not in {'flash', 'flash-lite'} or not re.fullmatch(r'[a-zA-Z0-9._-]+', model) or (model != 'auto' and model_key({'name': model}, family) is None):
        raise SubtitleError('Invalid model selection')
    if not isinstance(max_tokens, int) or not 1 <= max_tokens <= 8192:
        raise SubtitleError('Output token limit must be between 1 and 8192')
    if not prompt.strip():
        raise SubtitleError('Prompt must be nonempty')
    media, cache = Path(media), private_cache(cache)
    duration = probe(media)
    bounds(start, end, duration)
    identity = dict(source_sha256=digest(media), start=start, end=end,
                    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    family=family, requested_model=model, location=location,
                    max_output_tokens=max_tokens, temperature=0)
    with source_lock(cache):
        identity_path, output = cache/'identity.json', cache/'response.json'
        if identity_path.exists():
            saved = read_json(identity_path)
            if not isinstance(saved, dict) or saved.get('identity') != identity:
                raise SubtitleError('Audio opinion cache identity mismatch; use a new cache')
            actual = saved.get('model')
            if not isinstance(actual, str) or model_key({'name': actual}, family) is None or (model != 'auto' and actual != model):
                raise SubtitleError('Invalid pinned audio opinion model identity')
        else:
            if any((cache/name).exists() for name in ('response.json','response.inflight.json','request.json','clip.wav')):
                raise SubtitleError('Orphan audio opinion evidence; inspect before proceeding')
            if pool is None:
                pool = RequestPool(1, project=project)
            actual = model if model != 'auto' else choose_model(catalog(pool.token(), pool.project, location), family)
            atomic_json(identity_path, {'identity': identity, 'model': actual})
        # Existing success or unknown inflight is handled before auth/discovery.
        artifacts = cache/'artifacts.json'
        if artifacts.exists():
            hashes = read_json(artifacts)
            if not isinstance(hashes, dict) or set(hashes) != {'clip.wav', 'request.json'} or any(not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None for value in hashes.values()):
                raise SubtitleError('Invalid immutable audio opinion artifact identity')
            if any(not (cache/name).is_file() or digest(cache/name) != value for name, value in hashes.items()):
                raise SubtitleError('Immutable audio opinion artifact mismatch')
        elif output.exists() or output.with_suffix('.inflight.json').exists():
            raise SubtitleError('Missing immutable audio opinion artifact identity')
        if output.exists():
            pool = pool or RequestPool(1, project=project)
            text = pool.cached_request(output, '', {}, response_text)
            write_evidence(cache, identity, actual, text)
            return text
        if output.with_suffix('.inflight.json').exists():
            raise SubtitleError('Previous request outcome is uncertain; inspect evidence, do not replay')
        clip = cache/'clip.wav'
        if not clip.exists():
            temporary = cache/'clip.partial.wav'
            temporary.unlink(missing_ok=True)
            command(['ffmpeg','-nostdin','-v','error','-i',str(media),'-ss',str(start),'-t',str(end-start),'-vn','-ac','1','-ar','16000',str(temporary)])
            temporary.replace(clip)
        payload = {'contents': [{'role': 'user', 'parts': [
            {'text': prompt}, {'inlineData': {'mimeType': 'audio/wav', 'data': base64.b64encode(clip.read_bytes()).decode()}}]}],
            'generationConfig': {'temperature': 0, 'maxOutputTokens': max_tokens}}
        request_path = cache/'request.json'
        if request_path.exists():
            if read_json(request_path) != payload:
                raise SubtitleError('Immutable audio opinion request mismatch')
        else:
            atomic_json(request_path, payload)
        if not artifacts.exists():
            atomic_json(artifacts, {name: digest(cache/name) for name in ('clip.wav','request.json')})
        pool = pool or RequestPool(1, project=project)
        host = 'aiplatform.googleapis.com' if location == 'global' else location+'-aiplatform.googleapis.com'
        url = f'https://{host}/v1/projects/{urllib.parse.quote(pool.project or "", safe="")}/locations/{urllib.parse.quote(location, safe="")}/publishers/google/models/{urllib.parse.quote(actual, safe="")}:generateContent'
        text = pool.cached_request(output, url, payload, response_text)
        write_evidence(cache, identity, actual, text)
        return text


def write_evidence(cache, identity, actual, text):
    raw = read_json(cache/'response.json')
    atomic_json(cache/'evidence.json', {'identity': identity, 'selected_model': actual,
                'model_version': raw.get('modelVersion'), 'usage': raw.get('usageMetadata'),
                'text': text, 'limitations': 'Independent wording evidence only; no verbatim completeness or timing proof'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('media', type=Path)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--start', required=True, type=float)
    parser.add_argument('--end', required=True, type=float)
    parser.add_argument('--family', choices=['flash-lite','flash'], default='flash-lite')
    parser.add_argument('--model', default='auto')
    parser.add_argument('--prompt-file', type=Path, default=DEFAULT_PROMPT)
    parser.add_argument('--project')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--location', default='global')
    parser.add_argument('--max-output-tokens', type=int, default=2048)
    args = parser.parse_args()
    try:
        # Project configuration is local only; reusable evidence needs no auth.
        args.cache = private_cache(args.cache)
        project = args.project
        if not (args.cache/'identity.json').exists():
            project = select_project(args.project, config_path=args.config)
        elif not (args.cache/'response.json').exists() and not (args.cache/'response.inflight.json').exists():
            project = select_project(args.project, config_path=args.config)
        print(opinion(args.media, args.cache, args.start, args.end,
                      args.prompt_file.read_text(encoding='utf-8'), args.family,
                      args.model, project, args.location, max_tokens=args.max_output_tokens))
    except (SubtitleError, OSError, ValueError, RuntimeError) as error:
        print('Audio opinion failed: '+str(error), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
