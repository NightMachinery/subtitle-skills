"""Discover current non-streaming Gemini Transcribe versions on Vertex AI.

Catalog discovery is read-only. It does not submit audio or probe inference.
Project-specific inference permissions are still checked by the actual request.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request


def candidate_key(model):
    name = model.get('name', '').rsplit('/', 1)[-1]
    match = re.fullmatch(r'gemini-(\d+(?:\.\d+)*)-transcribe(?:-([a-z0-9-]+))?', name)
    if not match or 'live' in name.split('-'):
        return None
    state = model.get('versionState', '')
    stage = model.get('launchStage', '')
    if state in {'DEPRECATED', 'OBSOLETE', 'RETIRED'} or stage in {'DEPRECATED', 'SHUTDOWN'}:
        return None
    version = tuple(int(part) for part in match.group(1).split('.'))
    stable = int('preview' not in name and 'experimental' not in name and 'exp' not in name.split('-'))
    suffix = tuple(int(value) for value in re.findall(r'\d+', match.group(2) or ''))
    return version, stable, suffix, name


def choose_model(models):
    candidates = [key for model in models if (key := candidate_key(model)) is not None]
    if not candidates:
        raise RuntimeError('No supported Gemini Transcribe model found in the catalog; specify --model after checking current documentation')
    return max(candidates)[-1]


def catalog(token, project, location, opener=urllib.request.urlopen):
    if not token or not project:
        raise RuntimeError('Model discovery requires existing Vertex authentication and a project')
    host = 'aiplatform.googleapis.com' if location == 'global' else location + '-aiplatform.googleapis.com'
    endpoint = f'https://{host}/v1beta1/publishers/google/models'
    models, page, seen = [], '', set()
    for _ in range(20):
        query = {'listAllVersions': 'true', 'pageSize': '100'}
        if page:
            query['pageToken'] = page
        req = urllib.request.Request(endpoint + '?' + urllib.parse.urlencode(query), headers={
            'Authorization': 'Bearer ' + token, 'x-goog-user-project': project,
        })
        try:
            with opener(req, timeout=60) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'Gemini model catalog query failed (HTTP {error.code}); check access or specify a verified --model') from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise RuntimeError('Gemini model catalog query failed; check connectivity or specify a verified --model') from None
        models.extend(data.get('publisherModels', []))
        page = data.get('nextPageToken', '')
        if not page:
            return models
        if page in seen:
            raise RuntimeError('Model catalog returned a repeated pagination token')
        seen.add(page)
    raise RuntimeError('Model catalog exceeded its bounded pagination limit')


def resolve_model(token, project, location='global', requested='auto'):
    if requested != 'auto':
        if not re.fullmatch(r'[a-zA-Z0-9._-]+', requested):
            raise RuntimeError('Invalid explicit model identifier')
        return requested
    return choose_model(catalog(token, project, location))
