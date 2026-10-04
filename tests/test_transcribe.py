"""Offline invariants. Run: python3 -B tests/test_transcribe.py [path/to/transcribe.py]"""
import importlib.util
import contextlib
import copy
import hashlib
from email.utils import formatdate
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import sys
import unittest
import urllib.error
import wave
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

RUNNER = (Path(sys.argv.pop(1)).resolve() if len(sys.argv) > 1 and sys.argv[1].endswith('transcribe.py')
          else Path(__file__).resolve().parents[1] / 'skills' / 'subtitle-creation' / 'scripts' / 'transcribe.py')
sys.path.insert(0, str(RUNNER.parent))
spec = importlib.util.spec_from_file_location('subtitle_runner', RUNNER)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


def response():
    words = [{'word': word, 'startOffset': f'{0.3 + i * 0.5}s',
              'endOffset': f'{0.7 + i * 0.5}s'}
             for i, word in enumerate(['A', 'small', 'offline', 'test.'])]
    return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
        {'audioTranscription': {'speakerLabel': '1', 'words': words}}]}}],
        'usageMetadata': {'promptTokenCount': 322, 'candidatesTokenCount': 40,
            'promptTokensDetails': [{'modality': 'AUDIO', 'tokenCount': 321},
                                   {'modality': 'TEXT', 'tokenCount': 1}],
            'candidatesTokensDetails': [{'modality': 'TEXT', 'tokenCount': 40}]}}


def args_for(video, directory, *extra):
    return t.parser().parse_args([str(video), '--cache-dir', str(directory / 'cache'),
                                  '--model', 'test-transcribe', '--output-language', 'en', *extra])


def fixture(directory):
    video = directory / 'episode.wav'
    with wave.open(str(video), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b'\0\0' * 16000 * 10)
    args = args_for(video, directory)
    root = t.source_root(video, args.cache_dir)
    with t.source_lock(root):
        identity, cache = t.identity_for(video, root, t.settings(args))
        cache.mkdir(parents=True)
        t.atomic_json(cache / 'identity.json', identity)
        (cache / 'audio.wav').write_bytes(video.read_bytes())
        t.atomic_json(cache / 'preparation.json', {'duration': 10, 'silences': [],
            'chunks': [{'index': 0, 'start': 0, 'end': 10}]})
    return video, args, cache


def counted_process(video, directory, counter, result_path):
    def fake(*_):
        with open(counter, 'a') as output:
            output.write('request\n')
        time.sleep(0.15)
        return response()
    pool = t.RequestPool(1, 'test-project', request=fake)
    pool._token = 'offline-token'
    result = t.process_episode(Path(video), args_for(Path(video), Path(directory)), pool)
    Path(result_path).write_text(json.dumps(result))


class FakeClock:
    def __init__(self):
        self.seconds = 0.0
        self.sleeps = []
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.seconds

    def sleep(self, seconds):
        with self.lock:
            self.sleeps.append(seconds)
            self.seconds += seconds


def rejection(status=429, retry_after=None, retry_info=None, sensitive='private-sentinel'):
    details = [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo',
                'reason': 'RATE_LIMIT_EXCEEDED', 'metadata': {
                    'quota_metric': 'aiplatform.googleapis.com/generate_content_requests',
                    'quota_limit': 'GenerateContentRequestsPerMinutePerProjectPerBaseModel',
                    'consumer': 'projects/' + sensitive,
                    'project': sensitive, 'credential': sensitive}}]
    if retry_info:
        details.append({'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': retry_info})
    body = {'error': {'code': status, 'status': 'RESOURCE_EXHAUSTED',
                     'message': 'Quota exceeded for projects/' + sensitive + ' bearer ' + sensitive,
                     'details': details}}
    headers = {'Retry-After': retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError('https://invalid.test/projects/' + sensitive, status,
                                  sensitive, headers, io.BytesIO(json.dumps(body).encode()))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='subtitle-offline-')
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def pool(self, fake=None, workers=1):
        clock = FakeClock()
        pool = t.RequestPool(workers, 'test-project', request=fake or (lambda *_: response()),
                             sleep=clock.sleep, clock=clock.now, random=lambda: 0)
        pool._token = 'offline-token'
        pool.test_clock = clock
        return pool

    def test_project_precedence_private_toml_and_no_hostname(self):
        config = self.directory / 'private.toml'
        config.write_text('[vertex]\nproject = "config-project"\n')
        env = {'SUBTITLE_GCP_PROJECT': 'env-project', 'SUBTITLE_CONFIG': str(config)}
        self.assertEqual(t.select_project('explicit-project', env), 'explicit-project')
        self.assertEqual(t.select_project(None, env), 'env-project')
        self.assertEqual(t.select_project(None, {'SUBTITLE_CONFIG': str(config)}), 'config-project')
        with self.assertRaises(t.SubtitleError):
            t.select_project(None, {}, config_path=self.directory / 'missing.toml')

    def test_explicit_config_overrides_environment_config(self):
        a, b = self.directory / 'a.toml', self.directory / 'b.toml'
        a.write_text('[vertex]\nproject = "project-a"\n')
        b.write_text('[vertex]\nproject = "project-b"\n')
        self.assertEqual(t.select_project(None, {'SUBTITLE_CONFIG': str(a)}, b), 'project-b')

    def test_first_pass_timestamps_and_diarization(self):
        config = t.settings(t.parser().parse_args(['episode.wav', '--language', 'en-US']))
        payload = t.payload_for(b'offline', config)
        self.assertEqual(payload['generationConfig']['audioTranscriptionConfig'],
                         {'wordTimestamp': True, 'languageCodes': ['en-US'], 'diarization': True})
        config['language'] = 'auto'
        self.assertNotIn('languageCodes', t.payload_for(b'offline', config)['generationConfig']['audioTranscriptionConfig'])

    def test_no_duplicate_request_after_restart(self):
        calls = []
        pool = self.pool(lambda *_: calls.append(1) or response())
        path = self.directory / 'chunk-000.json'
        for _ in range(2):
            pool.transcribe(b'offline', path, 10, t.settings(t.parser().parse_args(['episode.wav'])))
        self.assertEqual(calls, [1])

    def test_invalid_response_saved_without_replay(self):
        calls = []
        bad = response()
        bad['candidates'][0]['content']['parts'][0]['audioTranscription']['words'] = []
        pool = self.pool(lambda *_: calls.append(1) or bad)
        path = self.directory / 'chunk-000.json'
        for _ in range(2):
            with self.assertRaises(t.SubtitleError):
                pool.transcribe(b'offline', path, 10, t.settings(t.parser().parse_args(['episode.wav'])))
        self.assertEqual(calls, [1])
        self.assertTrue(path.exists())

    def join_overlay(self, path):
        raw = response()
        words = raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words']
        words[1:2] = [{'word': '12.5', 'startOffset': '0.8s', 'endOffset': '1s', 'extra': 'retained'},
                      {'word': '%', 'startOffset': '1s', 'endOffset': '0.9s'}]
        t.atomic_json(path, raw)
        recheck = path.parent / 'synthetic-join-recheck.json'
        checked = response()
        checked['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][1].update(
            word='12.5%', startOffset='0.8s', endOffset='1.2s')
        t.atomic_json(recheck, checked)
        overlay = {'version': 1, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'joins': [{'part_index': 0, 'word_index': 1, 'raw_words': copy.deepcopy(words[1:3]),
                       'start_seconds': 0.8, 'end_seconds': 1.2,
                       'reviewer': 'synthetic reviewer', 'reason': 'bounded audio numeric join',
                       'evidence': {'recheck_path': str(recheck.resolve()),
                           'recheck_sha256': hashlib.sha256(recheck.read_bytes()).hexdigest(),
                           'clip_start_seconds': 0, 'clip_end_seconds': 4,
                           'recheck_part_index': 0, 'recheck_word_index': 1}}]}
        t.atomic_json(path.with_suffix('.word-joins.json'), overlay)
        return overlay, recheck

    def test_verified_numeric_join_native_offline_preserves_raw_and_provenance(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        overlay, recheck = self.join_overlay(path)
        before, evidence_before = path.read_bytes(), recheck.read_bytes()
        pool = self.pool(lambda *_: self.fail('network called'))
        with patch.object(pool, 'token', side_effect=AssertionError('authentication called')):
            copied = pool.transcribe(b'offline', path, 10, t.settings(args))
            expected = json.loads(before)
            words = expected['candidates'][0]['content']['parts'][0]['audioTranscription']['words']
            words[1].update(word='12.5%', startOffset=0.8, endOffset=1.2)
            del words[2]
            self.assertEqual(copied, expected)
            args.format_only = True
            result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(recheck.read_bytes(), evidence_before)
        audit = result['corrections'][0]
        self.assertEqual(audit['kind'], 'reviewed_word_join')
        self.assertEqual(audit['raw_words'], overlay['joins'][0]['raw_words'])
        self.assertEqual(audit['evidence'], overlay['joins'][0]['evidence'])
        self.assertEqual((audit['word_id'], audit['section'], audit['global_start'], audit['global_end']), (1, 0, 0.8, 1.2))
        self.assertEqual(result['review_flags'][0], audit)
        self.assertIn('A 12.5% offline test.', video.with_suffix('.en.srt').read_text())
        self.assertTrue(all(cue['end'] > cue['start'] for cue in t.read_json(cache / 'cues.json')))
        self.assertEqual(t.FORMAT_VERSION, 1)

    def test_numeric_join_comma_decimal_maps_clip_and_protects_reviewed_onset(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        overlay, recheck = self.join_overlay(path)
        raw = t.read_json(path)
        words = raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words']
        words[1].update(word='12,5', startOffset='0.8s', endOffset='4s')
        words[2].update(startOffset='4s', endOffset='0.9s')
        words[3].update(startOffset='4.5s', endOffset='4.8s')
        words[4].update(startOffset='5s', endOffset='5.3s')
        t.atomic_json(path, raw)
        checked = response()
        checked['candidates'][0]['content']['parts'][0]['audioTranscription']['words'] = [
            {'word': '12,5%.', 'startOffset': '0.3s', 'endOffset': '3.7s'}]
        t.atomic_json(recheck, checked)
        overlay['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        entry = overlay['joins'][0]
        entry.update(raw_words=copy.deepcopy(words[1:3]), start_seconds=0.8, end_seconds=4.2)
        entry['evidence'].update(clip_start_seconds=0.5, clip_end_seconds=4.5, recheck_word_index=0,
            recheck_sha256=hashlib.sha256(recheck.read_bytes()).hexdigest())
        t.atomic_json(path.with_suffix('.word-joins.json'), overlay)
        t.atomic_json(cache / 'preparation.json', {'duration': 10,
            'silences': [{'start': 0.9, 'end': 3.8}], 'chunks': [{'index': 0, 'start': 0, 'end': 10}]})
        args.format_only = True
        result = t.process_episode(video, args, self.pool(lambda *_: self.fail('network called')))
        self.assertEqual(result['status'], 'completed')
        merged = t.read_json(cache / 'words.json')[1]
        self.assertEqual((merged['word'], merged['start'], merged['end']), ('12,5%', 0.8, 4.2))
        self.assertEqual(result['corrections'][0]['global_start'], 0.8)
        self.assertEqual(len(result['corrections']), 1)

    def test_numeric_join_rejects_schema_identity_indices_and_unsupported_evidence(self):
        path = self.directory / 'chunk-000.json'
        valid, recheck = self.join_overlay(path)
        mutations = [lambda x: x.update(version=True), lambda x: x.update(source_sha256='bad'),
            lambda x: x.update(extra=1), lambda x: x['joins'].append(copy.deepcopy(x['joins'][0])),
            lambda x: x['joins'][0].update(part_index=True), lambda x: x['joins'][0].update(word_index=-1),
            lambda x: x['joins'][0].update(word_index=99), lambda x: x['joins'][0].update(word_index=0),
            lambda x: x['joins'][0].update(reviewer=' '), lambda x: x['joins'][0].update(end_seconds=0.8),
            lambda x: x['joins'][0].update(start_seconds=0.9), lambda x: x['joins'][0].update(end_seconds=1.3),
            lambda x: x['joins'][0].update(start_seconds=True),
            lambda x: x['joins'][0]['raw_words'][0].update(startOffset=0.8),
            lambda x: x['joins'][0]['evidence'].update(recheck_sha256='bad'),
            lambda x: x['joins'][0]['evidence'].update(recheck_path='relative.json'),
            lambda x: x['joins'][0]['evidence'].update(recheck_word_index=True),
            lambda x: x['joins'][0]['evidence'].update(clip_start_seconds=0.9),
            lambda x: x['joins'][0]['evidence'].update(clip_end_seconds=11),
            lambda x: x['joins'][0]['evidence'].update(extra=1)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                overlay = copy.deepcopy(valid)
                mutate(overlay)
                t.atomic_json(path.with_suffix('.word-joins.json'), overlay)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)

    def test_numeric_join_narrow_character_timing_speaker_and_part_boundary(self):
        path = self.directory / 'chunk-000.json'
        for mode in ('negative', 'lexical', 'unit', 'unicode', 'valid_percent', 'gap', 'speaker', 'cross_part'):
            with self.subTest(mode=mode):
                overlay, recheck = self.join_overlay(path)
                raw = t.read_json(path)
                parts = raw['candidates'][0]['content']['parts']
                words = parts[0]['audioTranscription']['words']
                if mode == 'negative': words[1]['word'] = '-12.5'
                elif mode == 'lexical': words[1]['word'] = 'twelve'
                elif mode == 'unit': words[2]['word'] = 'kg'
                elif mode == 'unicode': words[1]['word'] = '１２'
                elif mode == 'valid_percent': words[2]['endOffset'] = '1.1s'
                elif mode == 'gap': words[1]['endOffset'] = '0.99s'
                elif mode == 'speaker': words[2]['speakerLabel'] = 'different'
                else:
                    parts.append({'audioTranscription': {'words': words[2:]}})
                    del words[2:]
                t.atomic_json(path, raw)
                overlay['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
                if mode != 'cross_part': overlay['joins'][0]['raw_words'] = copy.deepcopy(words[1:3])
                t.atomic_json(path.with_suffix('.word-joins.json'), overlay)
                with self.assertRaises(t.SubtitleError): t.read_checkpoint(path, 10)

    def test_numeric_join_recheck_bounds_checksum_completion_and_word_support(self):
        path = self.directory / 'chunk-000.json'
        for mode in ('unfinished', 'overflow', 'changed', 'missing', 'punctuation'):
            with self.subTest(mode=mode):
                overlay, recheck = self.join_overlay(path)
                checked = t.read_json(recheck)
                words = checked['candidates'][0]['content']['parts'][0]['audioTranscription']['words']
                if mode == 'unfinished': checked['candidates'][0]['finishReason'] = 'MAX_TOKENS'
                elif mode == 'overflow': words[-1]['endOffset'] = '4.1s'
                elif mode == 'changed': words[1]['word'] = '13%'
                elif mode == 'missing': del words[1]['endOffset']
                else: words[1]['word'] = '12.5%.'
                t.atomic_json(recheck, checked)
                overlay['joins'][0]['evidence']['recheck_sha256'] = hashlib.sha256(recheck.read_bytes()).hexdigest()
                t.atomic_json(path.with_suffix('.word-joins.json'), overlay)
                if mode == 'punctuation':
                    self.assertEqual(t.timed_words(t.read_checkpoint(path, 10), 10)[1]['word'], '12.5%')
                else:
                    with self.assertRaises(t.SubtitleError): t.read_checkpoint(path, 10)
        overlay, recheck = self.join_overlay(path)
        overlay['joins'][0]['evidence']['clip_end_seconds'] = 100
        t.atomic_json(path.with_suffix('.word-joins.json'), overlay)
        with self.assertRaises(t.SubtitleError): t.read_checkpoint(path, 100)

    def test_numeric_join_orphans_and_overlay_coexistence_stop_before_network(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        for suffix in ('.timing-overrides.json', '.word-exclusions.json'):
            self.join_overlay(path)
            other = path.with_suffix(suffix)
            t.atomic_json(other, {})
            with self.assertRaises(t.SubtitleError): t.read_checkpoint(path, 10)
            other.unlink()
        path.unlink()
        pool = self.pool(lambda *_: self.fail('orphan allowed network'))
        with self.assertRaises(t.SubtitleError): pool.transcribe(b'offline', path, 10, t.settings(args))
        self.assertEqual(t.process_episode(video, args, pool)['status'], 'failed')
        self.assertFalse(path.with_suffix('.requests.json').exists())

    def test_numeric_join_validates_cached_pair_before_only_missing_requests(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        self.join_overlay(path)
        t.atomic_json(cache / 'preparation.json', {'duration': 10, 'silences': [],
            'chunks': [{'index': 0, 'start': 0, 'end': 5}, {'index': 1, 'start': 5, 'end': 10}]})
        calls = []
        result = t.process_episode(video, args, self.pool(lambda *_: calls.append(1) or response()))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(calls, [1])
        video.with_suffix('.en.srt').unlink()
        (cache / 'chunk-001.json').unlink()
        overlay = t.read_json(path.with_suffix('.word-joins.json'))
        overlay['source_sha256'] = 'invalid'
        t.atomic_json(path.with_suffix('.word-joins.json'), overlay)
        result = t.process_episode(video, args, self.pool(lambda *_: self.fail('invalid cached join allowed network')))
        self.assertEqual(result['status'], 'failed')

    def exclusion_overlay(self, path):
        raw = response()
        words = raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words']
        words.append({'word': 'uid:123', 'startOffset': '9s', 'speakerLabel': 'artifact'})
        t.atomic_json(path, raw)
        recheck = path.parent / 'synthetic-recheck.json'
        t.atomic_json(recheck, response())
        overlay = {'version': 1, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'exclusions': [{'part_index': 0, 'word_index': 4, 'raw_word': copy.deepcopy(words[-1]),
                       'reviewer': 'synthetic reviewer', 'reason': 'bounded terminal audio check',
                       'evidence': {'recheck_path': str(recheck.resolve()),
                           'recheck_sha256': hashlib.sha256(recheck.read_bytes()).hexdigest(),
                           'clip_start_seconds': 0, 'clip_end_seconds': 10}}]}
        t.atomic_json(path.with_suffix('.word-exclusions.json'), overlay)
        return overlay, recheck

    def test_verified_exclusion_native_formatting_preserves_raw_and_provenance(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        overlay, recheck = self.exclusion_overlay(path)
        before, evidence_before = path.read_bytes(), recheck.read_bytes()
        original = json.loads(before)
        pool = self.pool(lambda *_: self.fail('network called'))
        with patch.object(pool, 'token', side_effect=AssertionError('authentication called')):
            copied = pool.transcribe(b'offline', path, 10, t.settings(args))
            expected = copy.deepcopy(original)
            expected['candidates'][0]['content']['parts'][0]['audioTranscription']['words'].pop()
            self.assertEqual(copied, expected)
            args.format_only = True
            result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(recheck.read_bytes(), evidence_before)
        audit = result['corrections'][0]
        self.assertEqual(audit['kind'], 'reviewed_word_exclusion')
        self.assertEqual(audit['raw_word'], overlay['exclusions'][0]['raw_word'])
        self.assertEqual(audit['evidence'], overlay['exclusions'][0]['evidence'])
        self.assertEqual(audit['global_start'], 9)
        self.assertEqual(audit['section'], 0)
        self.assertNotIn('word_id', audit)
        self.assertEqual(result['review_flags'][0], audit)
        self.assertEqual(copied['usageMetadata'], original['usageMetadata'])
        cues = t.read_json(cache / 'cues.json')
        self.assertTrue(all(cue['end'] > cue['start'] for cue in cues))
        self.assertIn('A small offline test.', video.with_suffix('.en.srt').read_text())
        self.assertEqual(t.FORMAT_VERSION, 1)

    def test_word_exclusion_rejects_schema_identity_and_nonterminal_targets(self):
        path = self.directory / 'chunk-000.json'
        valid, recheck = self.exclusion_overlay(path)
        mutations = [lambda x: x.update(version=True), lambda x: x.update(source_sha256='bad'),
                     lambda x: x.update(extra=1), lambda x: x['exclusions'].append(copy.deepcopy(x['exclusions'][0])),
                     lambda x: x['exclusions'][0].update(part_index=True),
                     lambda x: x['exclusions'][0].update(word_index=-1),
                     lambda x: x['exclusions'][0].update(word_index=0),
                     lambda x: x['exclusions'][0].update(word_index=99),
                     lambda x: x['exclusions'][0].update(reviewer=' '),
                     lambda x: x['exclusions'][0]['raw_word'].update(endOffset=None),
                     lambda x: x['exclusions'][0]['evidence'].update(clip_end_seconds=9),
                     lambda x: x['exclusions'][0]['evidence'].update(clip_start_seconds=1),
                     lambda x: x['exclusions'][0]['evidence'].update(recheck_sha256='bad'),
                     lambda x: x['exclusions'][0]['evidence'].update(extra=1)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                overlay = copy.deepcopy(valid)
                mutate(overlay)
                t.atomic_json(path.with_suffix('.word-exclusions.json'), overlay)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)
        for target_change in ({'word': 'spoken'}, {'word': 'uid:123.'}, {'endOffset': '9.1s'}):
            valid, recheck = self.exclusion_overlay(path)
            raw = t.read_json(path)
            raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][-1].update(target_change)
            t.atomic_json(path, raw)
            valid['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            valid['exclusions'][0]['raw_word'] = raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][-1]
            t.atomic_json(path.with_suffix('.word-exclusions.json'), valid)
            with self.assertRaises(t.SubtitleError):
                t.read_checkpoint(path, 10)

    def test_orphan_exclusion_stops_before_audio_submission(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        self.exclusion_overlay(path)
        path.unlink()
        pool = self.pool(lambda *_: self.fail('orphan overlay allowed paid request'))
        with self.assertRaises(t.SubtitleError):
            pool.transcribe(b'offline', path, 10, t.settings(args))
        result = t.process_episode(video, args, self.pool(lambda *_: self.fail('orphan episode overlay allowed request')))
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(path.with_suffix('.inflight.json').exists())
        self.assertFalse(path.with_suffix('.requests.json').exists())

    def test_exclusion_across_parts_and_nonterminal_boundary(self):
        path = self.directory / 'chunk-000.json'
        overlay, recheck = self.exclusion_overlay(path)
        raw = t.read_json(path)
        parts = raw['candidates'][0]['content']['parts']
        tail = parts[0]['audioTranscription']['words'].pop()
        parts.append({'text': 'retained text', 'audioTranscription': {'words': [tail]}})
        parts.append({'text': 'retained metadata'})
        t.atomic_json(path, raw)
        overlay['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        overlay['exclusions'][0].update(part_index=1, word_index=0)
        t.atomic_json(path.with_suffix('.word-exclusions.json'), overlay)
        copied = t.read_checkpoint(path, 10)
        self.assertEqual(copied['candidates'][0]['content']['parts'][1]['text'], 'retained text')
        self.assertEqual(copied['candidates'][0]['content']['parts'][1]['audioTranscription']['words'], [])
        parts[2]['audioTranscription'] = {'words': [{'word': 'later', 'startOffset': '9.1s', 'endOffset': '9.2s'}]}
        t.atomic_json(path, raw)
        overlay['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        t.atomic_json(path.with_suffix('.word-exclusions.json'), overlay)
        with self.assertRaises(t.SubtitleError):
            t.read_checkpoint(path, 10)
        overlay, recheck = self.exclusion_overlay(path)
        overlay['exclusions'][0]['evidence']['clip_end_seconds'] = 100
        t.atomic_json(path.with_suffix('.word-exclusions.json'), overlay)
        with self.assertRaises(t.SubtitleError):
            t.read_checkpoint(path, 100)

    def test_reviewed_exclusion_allows_only_missing_paid_sections_after_validation(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        overlay, recheck = self.exclusion_overlay(path)
        raw = t.read_json(path)
        raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][-1]['startOffset'] = '4.5s'
        t.atomic_json(path, raw)
        overlay['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        overlay['exclusions'][0]['raw_word']['startOffset'] = '4.5s'
        overlay['exclusions'][0]['evidence']['clip_end_seconds'] = 5
        t.atomic_json(path.with_suffix('.word-exclusions.json'), overlay)
        t.atomic_json(cache / 'preparation.json', {'duration': 10, 'silences': [],
            'chunks': [{'index': 0, 'start': 0, 'end': 5}, {'index': 1, 'start': 5, 'end': 10}]})
        calls = []
        pool = self.pool(lambda *_: calls.append(1) or response())
        result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(calls, [1])
        video.with_suffix('.en.srt').unlink()
        (cache / 'chunk-001.json').unlink()
        overlay = t.read_json(path.with_suffix('.word-exclusions.json'))
        overlay['source_sha256'] = 'invalid'
        t.atomic_json(path.with_suffix('.word-exclusions.json'), overlay)
        result = t.process_episode(video, args, self.pool(lambda *_: self.fail('invalid cached evidence allowed paid request')))
        self.assertEqual(result['status'], 'failed')

    def test_word_exclusion_requires_successful_exact_bounded_final_audio_anchors(self):
        path = self.directory / 'chunk-000.json'
        for mode in ('unfinished', 'overflow', 'uid', 'uid_text', 'different', 'short', 'missing'):
            with self.subTest(mode=mode):
                overlay, recheck = self.exclusion_overlay(path)
                raw = t.read_json(recheck)
                words = raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words']
                if mode == 'unfinished':
                    raw['candidates'][0]['finishReason'] = 'MAX_TOKENS'
                elif mode == 'overflow':
                    words[-1]['endOffset'] = '10.1s'
                elif mode == 'uid':
                    words[0]['word'] = 'uid:123'
                elif mode == 'uid_text':
                    raw['candidates'][0]['content']['parts'][0]['audioTranscription']['text'] = 'A small offline test. uid:123'
                elif mode == 'different':
                    words[-1]['word'] = 'guess'
                elif mode == 'short':
                    del words[:2]
                else:
                    del words[-1]['endOffset']
                t.atomic_json(recheck, raw)
                overlay['exclusions'][0]['evidence']['recheck_sha256'] = hashlib.sha256(recheck.read_bytes()).hexdigest()
                t.atomic_json(path.with_suffix('.word-exclusions.json'), overlay)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)
        overlay, recheck = self.exclusion_overlay(path)
        t.atomic_json(path.with_suffix('.timing-overrides.json'), {})
        with self.assertRaises(t.SubtitleError):
            t.read_checkpoint(path, 10)

    def timing_overlay(self, path, *, raw=None):
        raw = response() if raw is None else raw
        target = raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]
        target['endOffset'] = '0.1s'
        t.atomic_json(path, raw)
        recheck = response()
        anchor = recheck['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]
        anchor.update(startOffset='0.2s', endOffset='0.6s')
        evidence = path.parent / 'bounded-recheck.json'
        t.atomic_json(evidence, recheck)
        overlay = {'version': 1, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'corrections': [{'part_index': 0, 'word_index': 0, 'word': target['word'],
                       'old_start_offset': target['startOffset'], 'old_end_offset': target['endOffset'],
                       'start_seconds': 0.2, 'end_seconds': 0.6, 'reviewer': 'synthetic reviewer',
                       'reason': 'bounded audio timing check',
                       'evidence': {'recheck_path': str(evidence.resolve()),
                           'recheck_sha256': hashlib.sha256(evidence.read_bytes()).hexdigest(),
                           'clip_start_seconds': 0, 'clip_end_seconds': 4,
                           'recheck_part_index': 0, 'recheck_word_index': 0}}]}
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        return overlay

    def numeric_overlay(self, path, original='7,', anchor_word='seven.'):
        raw = response()
        raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['word'] = original
        overlay = self.timing_overlay(path, raw=raw)
        evidence = overlay['corrections'][0]['evidence']
        recheck_path = Path(evidence['recheck_path'])
        recheck = t.read_json(recheck_path)
        recheck['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['word'] = anchor_word
        t.atomic_json(recheck_path, recheck)
        evidence.update(numeric_word_anchor=True,
                        recheck_sha256=hashlib.sha256(recheck_path.read_bytes()).hexdigest())
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        return overlay

    def test_numeric_timing_anchor_preserves_entire_response_and_evidence(self):
        path = self.directory / 'chunk-000.json'
        for digit, spelling in zip('0123456789',
                                  ('zero', 'one', 'two', 'three', 'four', 'five',
                                   'six', 'seven', 'eight', 'nine')):
            for original, anchor in ((digit + ',', spelling + '.'), (spelling, digit)):
                with self.subTest(original=original, anchor=anchor):
                    overlay = self.numeric_overlay(path, original, anchor)
                    recheck_path = Path(overlay['corrections'][0]['evidence']['recheck_path'])
                    before, recheck_before = path.read_bytes(), recheck_path.read_bytes()
                    expected = t.read_json(path)
                    expected['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0].update(
                        startOffset=0.2, endOffset=0.6)
                    data, audit = t.read_checkpoint(path, 10, include_corrections=True)
                    self.assertEqual(data, expected)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(recheck_path.read_bytes(), recheck_before)
                    self.assertEqual(audit[0]['evidence'], overlay['corrections'][0]['evidence'])

    def test_numeric_timing_anchor_rejects_other_text(self):
        path = self.directory / 'chunk-000.json'
        for original, anchor in [('7', 'eight'), ('7', 'Seven'), ('2', 'to'),
                                 ('4', 'quatre'), ('10', 'ten'), ('07', 'seven'),
                                 ('7.0', 'seven'), ('-7', 'seven'), ('+7', 'seven'),
                                 ('7th', 'seven'), ('7 apples', 'seven'),
                                 ('7', 'seven apples'), ('７', 'seven'),
                                 ('7', '7'), ('seven', 'seven')]:
            with self.subTest(original=original, anchor=anchor):
                self.numeric_overlay(path, original, anchor)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)

    def test_numeric_timing_anchor_requires_opt_in_and_all_existing_guards(self):
        path = self.directory / 'chunk-000.json'
        mutations = [
            ('no opt-in', lambda c: c['evidence'].pop('numeric_word_anchor')),
            *[(repr(value), lambda c, v=value: c['evidence'].update(numeric_word_anchor=v))
              for value in (False, None, 1, 0, 'true', [], {})],
            ('span conflict', lambda c: c['evidence'].update(recheck_end_word_index=1)),
            ('checksum', lambda c: c['evidence'].update(recheck_sha256='invalid')),
            ('start', lambda c: c.update(start_seconds=0.3)),
            ('end', lambda c: c.update(end_seconds=0.7)),
            ('original text', lambda c: c.update(word='seven')),
            ('old offset', lambda c: c.update(old_start_offset=0)),
            ('boolean index', lambda c: c['evidence'].update(recheck_word_index=True)),
            ('bounds', lambda c: c['evidence'].update(clip_end_seconds=0.5)),
        ]
        for label, mutate in mutations:
            with self.subTest(case=label):
                overlay = self.numeric_overlay(path)
                mutate(overlay['corrections'][0])
                t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)

    def phone_span_overlay(self, path, source='+1-555-234-ABCD.', tokens=None):
        raw = response()
        raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['word'] = source
        overlay = self.timing_overlay(path, raw=raw)
        recheck = response()
        tokens = ['+1-', '555-', '234-', 'abcd.'] if tokens is None else tokens
        words = [{'word': token, 'startOffset': 0.2 + index * 0.5,
                  'endOffset': 0.6 + index * 0.5} for index, token in enumerate(tokens)]
        recheck['candidates'][0]['content']['parts'][0]['audioTranscription']['words'] = words
        evidence = overlay['corrections'][0]['evidence']
        evidence['recheck_end_word_index'] = len(words) - 1
        evidence['clip_start_seconds'] = 0.5
        evidence['clip_end_seconds'] = 4.5
        t.atomic_json(Path(evidence['recheck_path']), recheck)
        evidence['recheck_sha256'] = hashlib.sha256(Path(evidence['recheck_path']).read_bytes()).hexdigest()
        overlay['corrections'][0].update(start_seconds=0.7, end_seconds=0.5 + words[-1]['endOffset'])
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        return overlay, recheck

    def test_reviewed_phone_span_preserves_raw_text_and_indexed_provenance(self):
        path = self.directory / 'chunk-000.json'
        for source, tokens in [('+1-555-234-ABCD.', ['+1-', '555-', '234-', 'abcd.']),
                               ('555-234-6789', ['555', '234', '6789']),
                               ('1-555-234-ABCD.', ['1-', '55', '5-', '23', '4-', 'ABCD.']),
                               ('1-555-234-ABCD.', ['1', '555', '234', 'AbCd!']),
                               ('1-555-234-ABCD.', ['1555', '234', 'abcd.'])]:
            with self.subTest(source=source):
                overlay, recheck = self.phone_span_overlay(path, source, tokens)
                before = path.read_bytes()
                evidence_before = Path(overlay['corrections'][0]['evidence']['recheck_path']).read_bytes()
                data, adjustments = t.read_checkpoint(path, 10, include_corrections=True)
                words = data['candidates'][0]['content']['parts'][0]['audioTranscription']['words']
                self.assertEqual(words[0]['word'], source)
                self.assertAlmostEqual(words[0]['startOffset'], 0.7)
                self.assertAlmostEqual(words[0]['endOffset'], overlay['corrections'][0]['end_seconds'])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(Path(overlay['corrections'][0]['evidence']['recheck_path']).read_bytes(), evidence_before)
                span = adjustments[0]['recheck_anchor_span']
                self.assertEqual([item['word_index'] for item in span], list(range(len(tokens))))
                self.assertEqual([item['word'] for item in span],
                                 recheck['candidates'][0]['content']['parts'][0]['audioTranscription']['words'])

    def test_reviewed_phone_span_rejects_unsupported_evidence(self):
        path = self.directory / 'chunk-000.json'
        cases = [
            ('changed digit', lambda c, w: w[1].update(word='556-')),
            ('missing plus', lambda c, w: w[0].update(word='1-')),
            ('changed sign', lambda c, w: w[0].update(word='-1-')),
            ('missing digit', lambda c, w: w[1].update(word='55-')),
            ('reordered digits', lambda c, w: w[2].update(word='243-')),
            ('internal punctuation', lambda c, w: w[1].update(word='555,')),
            ('unicode digits', lambda c, w: w[1].update(word='５５５-')),
            ('zero width', lambda c, w: w[1].update(word='555\u200b-')),
            ('wrong endpoint', lambda c, w: c.update(end_seconds=2.5)),
            ('wrong start', lambda c, w: c.update(start_seconds=0.8)),
            ('boolean end', lambda c, w: c['evidence'].update(recheck_end_word_index=True)),
            ('boolean start', lambda c, w: c['evidence'].update(recheck_word_index=False)),
            ('boolean part', lambda c, w: c['evidence'].update(recheck_part_index=False)),
            ('single index span', lambda c, w: c['evidence'].update(recheck_end_word_index=0)),
            ('negative end', lambda c, w: c['evidence'].update(recheck_end_word_index=-1)),
            ('negative start', lambda c, w: c['evidence'].update(recheck_word_index=-1)),
            ('out of bounds', lambda c, w: c['evidence'].update(recheck_end_word_index=4)),
            ('float end', lambda c, w: c['evidence'].update(recheck_end_word_index=3.0)),
            ('oversized', lambda c, w: c['evidence'].update(recheck_end_word_index=6)),
            ('reversed indices', lambda c, w: c['evidence'].update(recheck_word_index=2, recheck_end_word_index=1)),
            ('speaker mismatch', lambda c, w: w[2].update(speakerLabel='other')),
            ('overlap', lambda c, w: w[1].update(startOffset=0.5)),
            ('point', lambda c, w: w[1].update(endOffset=w[1]['startOffset'])),
            ('reversed interval', lambda c, w: w[1].update(endOffset=0.3)),
            ('plus in middle', lambda c, w: w[1].update(word='+555-')),
        ]
        for name, change in cases:
            with self.subTest(case=name):
                overlay, recheck = self.phone_span_overlay(path)
                correction = overlay['corrections'][0]
                words = recheck['candidates'][0]['content']['parts'][0]['audioTranscription']['words']
                change(correction, words)
                evidence = correction['evidence']
                t.atomic_json(Path(evidence['recheck_path']), recheck)
                evidence['recheck_sha256'] = hashlib.sha256(Path(evidence['recheck_path']).read_bytes()).hexdigest()
                t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)
        for tokens in [['-1-', '555-', '234-', 'ABCD.'],
                       ['1--', '555-', '234-', 'ABCD.'],
                       ['1', '-', '555', '234', 'ABCD.']]:
            with self.subTest(tokens=tokens):
                self.phone_span_overlay(path, '1-555-234-ABCD.', tokens)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)
        for source, tokens in [('some-word', ['some', 'word']),
                               ('CALL-555-234-ABCD', ['CALL', '555', '234', 'ABCD']),
                               ('555-234-ABCDEF', ['555', '234', 'ABCDEF']),
                               ('555-234-6789%', ['555', '234', '6789%'])]:
            with self.subTest(source=source):
                self.phone_span_overlay(path, source, tokens)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)

    def test_single_phone_anchor_keeps_strict_case_preserving_matching(self):
        path = self.directory / 'chunk-000.json'
        raw = response()
        raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['word'] = '1-555-234-ABCD.'
        overlay = self.timing_overlay(path, raw=raw)
        evidence = overlay['corrections'][0]['evidence']
        recheck = t.read_json(evidence['recheck_path'])
        anchor = recheck['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]
        for token, accepted in [('1-555-234-ABCD!', True), ('1-555-234-abcd.', False)]:
            with self.subTest(token=token):
                anchor['word'] = token
                t.atomic_json(Path(evidence['recheck_path']), recheck)
                evidence['recheck_sha256'] = hashlib.sha256(Path(evidence['recheck_path']).read_bytes()).hexdigest()
                t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
                if accepted:
                    t.read_checkpoint(path, 10)
                else:
                    with self.assertRaises(t.SubtitleError):
                        t.read_checkpoint(path, 10)

    def test_verified_timing_overlay_keeps_raw_bytes_and_other_fields(self):
        path = self.directory / 'chunk-000.json'
        raw = response()
        raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['speakerLabel'] = 'original'
        raw['candidates'][0]['content']['parts'].insert(0, {'text': 'synthetic non-audio part'})
        # Build a fixture whose exact target part is the audio part at index one.
        audio = raw['candidates'][0]['content']['parts'].pop(1)
        raw['candidates'][0]['content']['parts'].pop(0)
        raw['candidates'][0]['content']['parts'].append(audio)
        overlay = self.timing_overlay(path, raw=raw)
        raw = t.read_json(path)
        raw['candidates'][0]['content']['parts'].insert(0, {'text': 'synthetic non-audio part'})
        t.atomic_json(path, raw)
        overlay['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        overlay['corrections'][0]['part_index'] = 1
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        before = path.read_bytes()
        data, corrections = t.read_checkpoint(path, 10, include_corrections=True)
        expected = copy.deepcopy(raw)
        expected['candidates'][0]['content']['parts'][1]['audioTranscription']['words'][0].update(
            startOffset=0.2, endOffset=0.6)
        self.assertEqual(data, expected)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(corrections[0]['local_word_index'], 0)
        self.assertEqual(corrections[0]['evidence'], overlay['corrections'][0]['evidence'])

    def test_timing_anchor_allows_only_trailing_sentence_punctuation(self):
        path = self.directory / 'chunk-000.json'
        raw = response()
        raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['word'] = '42,'
        overlay = self.timing_overlay(path, raw=raw)
        evidence = Path(overlay['corrections'][0]['evidence']['recheck_path'])
        recheck = t.read_json(evidence)
        recheck['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['word'] = '42'
        t.atomic_json(evidence, recheck)
        overlay['corrections'][0]['evidence']['recheck_sha256'] = hashlib.sha256(evidence.read_bytes()).hexdigest()
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        before, evidence_before = path.read_bytes(), evidence.read_bytes()
        data = t.read_checkpoint(path, 10)
        word = data['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]
        self.assertEqual(word['word'], '42,')
        self.assertEqual((word['startOffset'], word['endOffset']), (0.2, 0.6))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(evidence.read_bytes(), evidence_before)
        for original, anchor in [('42%', '42'), ('-42', '42'), ('4.2', '42'),
                                 ("can't", 'cant'), ('Word', 'word'),
                                 ('word', 'words'), ('!', '?'), ('42', None)]:
            with self.subTest(original=original, anchor=anchor):
                self.assertFalse(t.same_timing_word(original, anchor))
        recheck['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['word'] = '43'
        t.atomic_json(evidence, recheck)
        overlay['corrections'][0]['evidence']['recheck_sha256'] = hashlib.sha256(evidence.read_bytes()).hexdigest()
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        with self.assertRaises(t.SubtitleError):
            t.read_checkpoint(path, 10)

    def test_timing_overlay_maps_bounded_clip_offsets_into_section_time(self):
        path = self.directory / 'chunk-000.json'
        overlay = self.timing_overlay(path)
        correction = overlay['corrections'][0]
        correction.update(start_seconds=2.2, end_seconds=2.6)
        correction['evidence'].update(clip_start_seconds=2, clip_end_seconds=6)
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        data = t.read_checkpoint(path, 10)
        word = t.timed_words(data, 10)[0]
        self.assertEqual((word['start'], word['end']), (2.2, 2.6))

    def test_verified_zero_word_overlay_formats_positive_cues_and_keeps_flag(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        overlay = self.timing_overlay(path)
        raw = t.read_json(path)
        raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['endOffset'] = '9s'
        t.atomic_json(path, raw)
        correction = overlay['corrections'][0]
        correction.update(old_end_offset='9s', end_seconds=0.2)
        evidence_path = Path(correction['evidence']['recheck_path'])
        evidence = t.read_json(evidence_path)
        evidence['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['endOffset'] = '0.2s'
        t.atomic_json(evidence_path, evidence)
        correction['evidence']['recheck_sha256'] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        overlay['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        before = path.read_bytes()
        args.format_only = True
        pool = self.pool(lambda *_: self.fail('paid call'))
        with patch.object(pool, 'token', side_effect=AssertionError('authentication called')):
            result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(path.read_bytes(), before)
        words = t.read_json(cache / 'words.json')
        self.assertEqual((words[0]['start'], words[0]['end']), (0.2, 0.2))
        self.assertEqual([word['word'] for word in words], ['A', 'small', 'offline', 'test.'])
        self.assertTrue(any(flag.get('kind') == 'implausible_word_interval'
                            and flag.get('word_id') == 0 and flag.get('seconds') == 0
                            for flag in result['review_flags']))
        self.assertEqual(result['corrections'][0]['end_seconds'], 0.2)
        cues = t.read_json(cache / 'cues.json')
        self.assertTrue(all(cue['end'] > cue['start'] for cue in cues))
        self.assertTrue(t.valid_srt(Path(result['output'])))

    def test_timing_overlay_rejects_checksum_identity_indices_and_extra_changes(self):
        path = self.directory / 'chunk-000.json'
        valid = self.timing_overlay(path)
        changes = [('checksum', lambda value: value.update(source_sha256='0' * 64)),
                   ('word', lambda value: value['corrections'][0].update(word='different')),
                   ('old start', lambda value: value['corrections'][0].update(old_start_offset='0.4s')),
                   ('old type', lambda value: value['corrections'][0].update(old_end_offset=0.1)),
                   ('bool index', lambda value: value['corrections'][0].update(part_index=True)),
                   ('negative index', lambda value: value['corrections'][0].update(word_index=-1)),
                   ('range index', lambda value: value['corrections'][0].update(word_index=999)),
                   ('duplicate', lambda value: value['corrections'].append(copy.deepcopy(value['corrections'][0]))),
                   ('speaker edit', lambda value: value['corrections'][0].update(speaker='replacement')),
                   ('version bool', lambda value: value.update(version=True)),
                   ('empty corrections', lambda value: value.update(corrections=[]))]
        for name, change in changes:
            with self.subTest(case=name):
                value = copy.deepcopy(valid)
                change(value)
                t.atomic_json(path.with_suffix('.timing-overrides.json'), value)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)

    def test_timing_overlay_requires_verified_bounded_raw_recheck_evidence(self):
        path = self.directory / 'chunk-000.json'
        valid = self.timing_overlay(path)
        changes = [('reviewer', lambda value: value.update(reviewer=' ')),
                   ('reason', lambda value: value.update(reason='')),
                   ('evidence', lambda value: value.update(evidence={})),
                   ('evidence checksum', lambda value: value['evidence'].update(recheck_sha256='0' * 64)),
                   ('evidence path', lambda value: value['evidence'].update(recheck_path='relative.json')),
                   ('evidence index', lambda value: value['evidence'].update(recheck_word_index=True)),
                   ('clip range', lambda value: value['evidence'].update(clip_end_seconds=11)),
                   ('clip reversed', lambda value: value['evidence'].update(clip_start_seconds=4)),
                   ('unsupported time', lambda value: value.update(start_seconds=0.25)),
                   ('unsupported zero', lambda value: value.update(end_seconds=0.2)),
                   ('nonfinite', lambda value: value.update(start_seconds=float('nan'))),
                   ('boolean time', lambda value: value.update(end_seconds=True)),
                   ('reversed', lambda value: value.update(start_seconds=0.7)),
                   ('outside', lambda value: value.update(end_seconds=11))]
        for name, change in changes:
            with self.subTest(case=name):
                value = copy.deepcopy(valid)
                change(value['corrections'][0])
                path.with_suffix('.timing-overrides.json').write_text(json.dumps(value))
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)
        value = copy.deepcopy(valid)
        value['corrections'][0]['evidence']['clip_end_seconds'] = 70
        t.atomic_json(path.with_suffix('.timing-overrides.json'), value)
        with self.assertRaises(t.SubtitleError):
            t.read_checkpoint(path, 100)

    def test_timing_overlay_rejects_unfinished_malformed_or_out_of_clip_recheck(self):
        path = self.directory / 'chunk-000.json'
        valid = self.timing_overlay(path)
        evidence_path = Path(valid['corrections'][0]['evidence']['recheck_path'])
        original = t.read_json(evidence_path)
        changes = [('unfinished', lambda value: value['candidates'][0].update(finishReason='MAX_TOKENS')),
                   ('word mismatch', lambda value: value['candidates'][0]['content']['parts'][0]
                    ['audioTranscription']['words'][0].update(word='Different')),
                   ('bad timing', lambda value: value['candidates'][0]['content']['parts'][0]
                    ['audioTranscription']['words'][0].update(endOffset='0.1s')),
                   ('exact clip', lambda value: value['candidates'][0]['content']['parts'][0]
                    ['audioTranscription']['words'][-1].update(endOffset='4.05s'))]
        for name, change in changes:
            with self.subTest(case=name):
                recheck = copy.deepcopy(original)
                change(recheck)
                t.atomic_json(evidence_path, recheck)
                overlay = copy.deepcopy(valid)
                overlay['corrections'][0]['evidence']['recheck_sha256'] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
                t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(path, 10)

    def test_orphan_overlay_cannot_trigger_paid_request(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        self.timing_overlay(path)
        path.unlink()
        pool = self.pool(lambda *_: self.fail('orphan overlay must not trigger paid work'))
        with patch.object(pool, 'token', side_effect=AssertionError('authentication called')):
            with self.assertRaises(t.SubtitleError):
                pool.transcribe(b'offline', path, 10, t.settings(args))
            result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix('.requests.json').exists())

    def test_overlay_is_validated_even_when_raw_timing_is_already_valid(self):
        path = self.directory / 'chunk-000.json'
        overlay = self.timing_overlay(path)
        t.atomic_json(path, response())
        overlay['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        overlay['corrections'][0]['old_end_offset'] = '0.7s'
        overlay['corrections'][0]['reviewer'] = ''
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        with self.assertRaisesRegex(t.SubtitleError, 'reviewer'):
            t.read_checkpoint(path, 10)

    def test_overlay_pool_cache_and_format_only_resume_keep_provenance_without_api(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        overlay = self.timing_overlay(path)
        before = path.read_bytes()
        pool = self.pool(lambda *_: self.fail('API called for cached overlay'))
        with patch.object(pool, 'token', side_effect=AssertionError('authentication called')):
            data = pool.transcribe(b'offline', path, 10, t.settings(args))
            self.assertEqual(t.timed_words(data, 10)[0]['start'], 0.2)
            args.format_only = True
            result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result['corrections'][0]['kind'], 'reviewed_timing_override')
        self.assertEqual(result['corrections'][0]['source_sha256'], overlay['source_sha256'])
        self.assertEqual(result['corrections'][0]['word_id'], 0)
        self.assertEqual(result['review_flags'][0]['evidence'], overlay['corrections'][0]['evidence'])
        self.assertEqual(t.FORMAT_VERSION, 1)

    def test_verified_overlay_onset_is_not_repaired_again_from_silence(self):
        video, args, cache = fixture(self.directory)
        path = cache / 'chunk-000.json'
        raw = response()
        raw['candidates'][0]['content']['parts'][0]['audioTranscription']['words'] = [
            {'word': 'Example.', 'startOffset': '0.3s', 'endOffset': '0.1s'}]
        overlay = self.timing_overlay(path, raw=raw)
        correction = overlay['corrections'][0]
        correction['end_seconds'] = 3.0
        evidence_path = Path(correction['evidence']['recheck_path'])
        recheck = response()
        recheck['candidates'][0]['content']['parts'][0]['audioTranscription']['words'] = [
            {'word': 'Example.', 'startOffset': '0.2s', 'endOffset': '3.0s'}]
        t.atomic_json(evidence_path, recheck)
        correction['evidence']['recheck_sha256'] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        t.atomic_json(path.with_suffix('.timing-overrides.json'), overlay)
        prep = t.read_json(cache / 'preparation.json')
        prep['silences'] = [{'start': 1.0, 'end': 2.6}]
        t.atomic_json(cache / 'preparation.json', prep)
        args.format_only = True
        result = t.process_episode(video, args, self.pool(lambda *_: self.fail('paid call')))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(t.read_json(cache / 'words.json')[0]['start'], 0.2)
        self.assertEqual(t.read_json(cache / 'words.json')[0]['end'], 3.0)
        self.assertEqual(len(result['corrections']), 1)
        self.assertEqual(result['corrections'][0]['kind'], 'reviewed_timing_override')

    def test_invalid_success_stops_queued_sections_without_uncertain_marker_or_replay(self):
        video, args, cache = fixture(self.directory)
        args.request_workers = 1
        prep = t.read_json(cache / 'preparation.json')
        prep['chunks'] = [{'index': 0, 'start': 0, 'end': 5}, {'index': 1, 'start': 5, 'end': 10}]
        t.atomic_json(cache / 'preparation.json', prep)
        calls = []
        bad = response()
        bad['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['endOffset'] = '0.1s'
        pool = self.pool(lambda *_: calls.append(1) or bad)
        result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(calls, [1])
        self.assertEqual(t.read_json(cache / 'chunk-000.json'), bad)
        self.assertEqual(result['requests']['attempts'], 1)
        self.assertEqual(result['requests']['uncertain_outcomes'], 0)
        self.assertFalse((cache / 'chunk-000.inflight.json').exists())
        self.assertFalse((cache / 'chunk-001.requests.json').exists())
        self.assertFalse((cache / 'chunk-001.inflight.json').exists())
        resumed = t.process_episode(video, args, self.pool(lambda *_: self.fail('paid replay')))
        self.assertEqual(resumed['status'], 'failed')
        self.assertEqual(calls, [1])

    def test_unknown_outcome_not_replayed_even_on_restart(self):
        calls = []
        def timeout(*_):
            calls.append(1)
            raise TimeoutError('sensitive server information')
        pool = self.pool(timeout)
        path = self.directory / 'chunk-000.json'
        for _ in range(2):
            with self.assertRaises(t.SubtitleError) as caught:
                pool.transcribe(b'offline', path, 10, t.settings(t.parser().parse_args(['episode.wav'])))
            self.assertNotIn('sensitive', str(caught.exception))
        self.assertEqual(calls, [1])
        self.assertTrue(path.with_suffix('.inflight.json').exists())

    def test_retry_only_known_429_and_server_error_max_three(self):
        for status, expected in [(429, 3), (503, 3), (400, 1)]:
            calls = []
            def fail(*_):
                calls.append(1)
                raise urllib.error.HTTPError('https://invalid.test', status, 'redacted', {}, None)
            with self.assertRaises(t.SubtitleError):
                self.pool(fail).transcribe(b'offline', self.directory / f'chunk-{status}.json',
                                           10, t.settings(t.parser().parse_args(['episode.wav'])))
            self.assertEqual(len(calls), expected)

    def test_global_request_semaphore(self):
        active = maximum = 0
        lock = threading.Lock()
        def fake(*_):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(active, maximum)
            time.sleep(0.03)
            with lock:
                active -= 1
            return response()
        pool = self.pool(fake, 2)
        config = t.settings(t.parser().parse_args(['episode.wav']))
        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(lambda i: pool.transcribe(b'offline', self.directory / f'chunk-{i}.json', 10, config), range(8)))
        self.assertEqual(maximum, 2)

    def test_concurrent_processes_one_source_one_request(self):
        video, args, cache = fixture(self.directory)
        counter = self.directory / 'requests.txt'
        context = multiprocessing.get_context('fork')
        processes = [context.Process(target=counted_process, args=(str(video), str(self.directory),
                       str(counter), str(self.directory / f'result-{index}.json'))) for index in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(counter.read_text().splitlines(), ['request'])
        statuses = {json.loads((self.directory / f'result-{index}.json').read_text())['status'] for index in range(2)}
        self.assertEqual(statuses, {'completed', 'skipped'})

    def test_completed_skip_no_auth_or_extraction(self):
        video = self.directory / 'does-not-need-to-exist.wav'
        output = video.with_suffix('.en.srt')
        output.write_text('1\n00:00:00,100 --> 00:00:01,000\nReviewed subtitle.\n')
        pool = self.pool()
        with patch.object(pool, 'token', side_effect=AssertionError('auth called')), \
             patch.object(t, 'prepare_audio', side_effect=AssertionError('extraction called')):
            result = t.process_episode(video, args_for(video, self.directory), pool)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(output.read_text(), '1\n00:00:00,100 --> 00:00:01,000\nReviewed subtitle.\n')

    def test_invalid_existing_output_preserved_without_auth(self):
        video = self.directory / 'episode.wav'
        output = video.with_suffix('.en.srt')
        output.write_text('review draft')
        pool = self.pool()
        with patch.object(pool, 'token', side_effect=AssertionError('auth called')):
            result = t.process_episode(video, args_for(video, self.directory), pool)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(output.read_text(), 'review draft')

    def test_cache_configuration_invalidation(self):
        video, args, cache = fixture(self.directory)
        root = t.source_root(video, args.cache_dir)
        config = t.settings(args)
        for name, value in [('model', 'other-model'), ('language', 'fr-FR'), ('diarization', False)]:
            changed = {**config, name: value}
            _, new_cache = t.identity_for(video, root, changed)
            self.assertNotEqual(cache, new_cache)

    def test_format_only_and_auto_pin_do_not_authenticate(self):
        video, args, cache = fixture(self.directory)
        t.atomic_json(cache / 'chunk-000.json', response())
        root = t.source_root(video, args.cache_dir)
        args.model, args.format_only = 'auto', True
        stat = video.stat()
        t.atomic_json(root / 'model-selection.json', {'signature': {'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns},
            'requested_settings': t.settings(args), 'resolved_model': 'test-transcribe'})
        pool = self.pool()
        with patch.object(pool, 'token', side_effect=AssertionError('auth called')):
            result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['model'], 'test-transcribe')

    def test_out_of_order_cross_speaker_phrases_and_word_preservation(self):
        words = [{'id': 0, 'word': 'Hello', 'speaker': 'a', 'start': 0.1, 'end': 0.7},
                 {'id': 1, 'word': 'there.', 'speaker': 'a', 'start': 0.75, 'end': 1.4},
                 {'id': 2, 'word': 'Yes.', 'speaker': 'b', 'start': 0.65, 'end': 1.0}]
        words.sort(key=lambda word: (word['start'], word['id']))
        cues = t.make_cues(words, 2)
        t.validate_cues(cues, words, 2)
        self.assertEqual(cues[0]['text'], '- Hello there.\n- Yes.')
        self.assertEqual(cues[0]['word_ids'], [0, 2, 1])

    def test_bad_timestamps_fail(self):
        for value in ('NaNs', 'infs', '-1s', '20s', None):
            data = response()
            data['candidates'][0]['content']['parts'][0]['audioTranscription']['words'][0]['startOffset'] = value
            with self.assertRaises(t.SubtitleError):
                t.timed_words(data, 10)

    def test_silence_expansion_and_no_speech_cut(self):
        self.assertEqual(t.choose_boundaries(1000, [400, 800]), [0, 400, 800, 1000])
        self.assertEqual(t.choose_boundaries(800, []), [0, 800])
        with self.assertRaises(t.SubtitleError):
            t.choose_boundaries(1000, [])

    def test_measured_pause_repair_and_review_flags(self):
        words = [{'id': 0, 'word': 'Hello', 'speaker': 'a', 'start': 0, 'end': 5}]
        corrections, flags = t.repair_long_words(words, [{'start': 3, 'end': 4.4}])
        self.assertEqual(words[0]['start'], 4.4)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(flags[0]['kind'], 'implausible_word_interval')
        original = [{'id': 0, 'word': 'Hello', 'speaker': 'a', 'start': 0, 'end': 5}]
        t.repair_long_words(original, [])
        self.assertEqual(original[0]['start'], 0)

    def onset_words(self):
        return [{'id': i, 'word': text, 'speaker': 'voice', 'section': 4, 'start': start, 'end': end}
                for i, (text, start, end) in enumerate([
                    ('Alpha', 2.0, 2.4), ('beta.', 2.4, 2.8),
                    ('Gamma', 1.0, 4.8), ('delta.', 4.8, 5.2)])]

    def test_regressed_onset_uses_measured_pause_and_raw_order_evidence(self):
        words = self.onset_words()
        before = [(w['id'], w['word'], w['speaker'], w['section'], w['end']) for w in words]
        with contextlib.redirect_stdout(io.StringIO()):
            corrections, flags = t.repair_long_words(words, [{'start': 3.8, 'end': 4.5}])
        self.assertEqual(words[2]['start'], 4.5)
        self.assertEqual(words[2]['original_start'], 1.0)
        self.assertEqual(before, [(w['id'], w['word'], w['speaker'], w['section'], w['end']) for w in words])
        evidence = corrections[0]['evidence']
        self.assertEqual(evidence['kind'], 'same_speaker_raw_order_regression')
        self.assertEqual(evidence['pause'], {'start': 3.8, 'end': 4.5})
        self.assertEqual((evidence['previous_word_id'], evidence['following_word_id']), (1, 3))
        self.assertEqual(evidence['intervening_word_ids'], [0, 1])
        self.assertTrue(any(flag['kind'] == 'inferred_onset' and flag['evidence'] == evidence for flag in flags))
        words.sort(key=lambda word: (word['start'], word['id']))
        cues = t.make_cues(words, 6)
        t.validate_cues(cues, words, 6)
        self.assertEqual([i for cue in cues for i in cue['word_ids']], list(range(4)))

    def test_two_regressed_onsets_repair_later_first_without_fixed_offset(self):
        words = [{'id': i, 'word': text, 'speaker': 'voice', 'section': 1, 'start': start, 'end': end}
                 for i, (text, start, end) in enumerate([
                     ('One', 3.0, 3.3), ('two.', 3.3, 3.8), ('Three', 1.7, 6.1),
                     ('four', 6.1, 6.6), ('five.', 6.6, 7.1),
                     ('Six', 2.8, 9.4), ('seven.', 9.4, 10.0)])]
        words.sort(key=lambda word: (word['start'], word['id']))
        before = {word['id']: (word['word'], word['end']) for word in words}
        with contextlib.redirect_stdout(io.StringIO()):
            corrections, flags = t.repair_long_words(words, [{'start': 5.4, 'end': 5.85},
                                                           {'start': 8.7, 'end': 9.05}])
        self.assertEqual([value['word_id'] for value in corrections], [2, 5])
        self.assertEqual([value['start'] for value in corrections], [5.85, 9.05])
        self.assertNotAlmostEqual(corrections[0]['start'] - corrections[0]['original_start'],
                                  corrections[1]['start'] - corrections[1]['original_start'])
        self.assertEqual(before, {word['id']: (word['word'], word['end']) for word in words})
        self.assertEqual(sum(flag['kind'] == 'inferred_onset' for flag in flags), 2)
        words.sort(key=lambda word: (word['start'], word['id']))
        t.validate_cues(t.make_cues(words, 11), words, 11)
        self.assertEqual([word['id'] for word in words], list(range(7)))

    def test_regression_repair_rejects_other_speaker_or_section(self):
        for changed in ({'speaker': 'different'}, {'section': 9}):
            with self.subTest(changed=changed):
                words = self.onset_words()
                # The immediate previous/following neighbors still share the
                # track. An earlier intervening word is the conflicting voice.
                words[0].update(changed)
                corrections, flags = t.repair_long_words(words, [{'start': 3.8, 'end': 4.5}])
                self.assertEqual(corrections, [])
                self.assertEqual(words[2]['start'], 1.0)
                self.assertFalse(any(flag['kind'] == 'inferred_onset' for flag in flags))

    def test_regression_repair_rejects_credible_following_overlap_or_unfinished_word(self):
        for change in ('following', 'unfinished'):
            with self.subTest(change=change):
                words = self.onset_words()
                if change == 'following':
                    words[3].update(start=3.2, end=3.5)
                else:
                    words[0]['end'] = 4.7
                corrections, _ = t.repair_long_words(words, [{'start': 3.8, 'end': 4.5}])
                self.assertEqual(corrections, [])
                self.assertEqual(words[2]['start'], 1.0)

    def test_repair_does_not_ignore_overlap_starting_before_anomalous_start(self):
        words = self.onset_words()
        words[0].update(start=0.5, end=2.4, speaker='different')
        corrections, _ = t.repair_long_words(words, [{'start': 3.8, 'end': 4.5}])
        self.assertEqual(corrections, [])
        self.assertEqual(words[2]['start'], 1.0)

    def test_repair_does_not_ignore_following_raw_word_before_anomalous_start(self):
        words = self.onset_words()
        words[3].update(start=0.4, end=0.7)
        corrections, _ = t.repair_long_words(words, [{'start': 3.8, 'end': 4.5}])
        self.assertEqual(corrections, [])
        self.assertEqual(words[2]['start'], 1.0)

    def test_regression_repair_requires_pause_section_unique_ids_and_backwards_sequence(self):
        for missing in ('pause', 'section', 'unique_ids', 'regression'):
            with self.subTest(missing=missing):
                words = self.onset_words()
                pauses = [{'start': 3.8, 'end': 4.5}]
                if missing == 'pause':
                    pauses = []
                elif missing == 'section':
                    for word in words:
                        word.pop('section')
                elif missing == 'unique_ids':
                    words[0]['id'] = words[1]['id']
                else:
                    words[2].update(start=3.0, end=6.0)
                    words[3].update(start=3.5, end=3.8)
                    pauses = [{'start': 4.6, 'end': 5.4}]
                original = words[2]['start']
                corrections, _ = t.repair_long_words(words, pauses)
                self.assertEqual(corrections, [])
                self.assertEqual(words[2]['start'], original)

    def test_usage_actual_modality(self):
        usage = t.usage_summary([response()])
        self.assertEqual((usage['input_audio_tokens'], usage['input_text_tokens'], usage['output_text_tokens']), (321, 1, 40))
        self.assertEqual(t.usage_summary([{}])['responses_missing_usage'], 1)

    def test_serialized_validation_and_word_drop_detected(self):
        words = [{'id': 0, 'word': 'Hello.', 'speaker': 'a', 'start': 0.1, 'end': 0.7}]
        cues = t.make_cues(words, 2)
        path = self.directory / 'test.srt'
        t.atomic_bytes(path, t.render_srt(cues).encode())
        self.assertTrue(t.valid_srt(path))
        cues[0]['word_ids'] = []
        with self.assertRaises(t.SubtitleError):
            t.validate_cues(cues, words, 2)

    def test_result_usage_and_final_publication(self):
        video, args, cache = fixture(self.directory)
        result = t.process_episode(video, args, self.pool())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['word_count'], 4)
        self.assertTrue(t.valid_srt(video.with_suffix('.en.srt')))
        self.assertEqual(result['usage']['input_audio_tokens'], 321)
        self.assertNotIn('test-project', (cache / 'result.json').read_text())
        self.assertNotIn('offline-token', (cache / 'chunk-000.json').read_text())

    def test_confirmed_language_renaming_only_reuses_cache(self):
        video, args, cache = fixture(self.directory)
        args.output_language = None
        original = t.process_episode(video, args, self.pool())
        self.assertEqual(original['status'], 'needs_language')
        self.assertIsNone(original['output'])
        self.assertEqual(list(self.directory.rglob('*.srt')), [])
        args.format_only, args.output_language = True, 'fr'
        pool = self.pool()
        with patch.object(pool, 'token', side_effect=AssertionError('auth called')):
            tagged = t.process_episode(video, args, pool)
        self.assertEqual(tagged['status'], 'completed')
        self.assertEqual(t.render_srt(t.read_json(cache / 'cues.json')), video.with_suffix('.fr.srt').read_text())
        self.assertEqual(original['cache'], tagged['cache'])
        self.assertFalse((cache / 'validated.srt').exists())
        self.assertFalse(video.with_suffix('.source.srt').exists())
        self.assertEqual(t.read_metadata(video), {'original_language': 'fr', 'original_subtitle': 'episode.fr.srt'})

    def test_batch_independent_success_and_failure_summary(self):
        video, args, cache = fixture(self.directory)
        missing = self.directory / 'missing.wav'
        pool = self.pool()
        with patch.object(t, 'RequestPool', return_value=pool):
            code = t.main([str(video), str(missing), '--project', 'test-project',
                           '--model', 'test-transcribe', '--output-language', 'en', '--cache-dir', str(args.cache_dir)])
        self.assertEqual(code, 1)
        summary = t.read_json(args.cache_dir / 'summary.json')
        self.assertEqual(summary['counts'], {'completed': 1, 'skipped': 0, 'needs_language': 0, 'failed': 1})
        self.assertTrue(video.with_suffix('.en.srt').exists())
        self.assertNotIn('test-project', (args.cache_dir / 'summary.json').read_text())

    def test_main_completed_skip_without_project_or_auth(self):
        video = self.directory / 'reviewed.wav'
        video.with_suffix('.en.srt').write_text('1\n00:00:00,100 --> 00:00:01,000\nReviewed.\n')
        t.atomic_json(t.metadata_path(video), {'original_language': 'en', 'original_subtitle': 'reviewed.en.srt'})
        with patch.object(t, 'select_project', side_effect=AssertionError('project resolution called')), \
             patch.object(t.RequestPool, 'token', side_effect=AssertionError('auth called')):
            code = t.main([str(video), '--cache-dir', str(self.directory / 'cache')])
        self.assertEqual(code, 0)

    def test_dry_run_no_auth_or_extraction_or_cache(self):
        video = self.directory / 'dry.wav'
        args = args_for(video, self.directory)
        with patch.object(t, 'probe', return_value=120), \
             patch.object(t, 'RequestPool', side_effect=AssertionError('pool initialized')), \
             patch.object(t, 'prepare_audio', side_effect=AssertionError('extraction called')):
            code = t.main([str(video), '--project', 'test-project', '--dry-run',
                           '--cache-dir', str(args.cache_dir)])
        self.assertEqual(code, 0)
        self.assertFalse(args.cache_dir.exists())

    def test_no_audio_fails_gracefully(self):
        class Result:
            stdout = '{"streams":[{"codec_type":"video"}],"format":{"duration":"1"}}'
        with patch.object(t, 'command', return_value=Result()):
            with self.assertRaisesRegex(t.SubtitleError, 'no audio'):
                t.probe(self.directory / 'silent-video.mp4')

    def test_real_audio_preparation(self):
        video, args, existing = fixture(self.directory)
        cache = self.directory / 'new-preparation'
        cache.mkdir()
        preparation = t.prepare_audio(video, cache)
        self.assertEqual(preparation['duration'], 10)
        self.assertEqual(preparation['chunks'], [{'index': 0, 'start': 0.0, 'end': 10.0}])
        with wave.open(str(cache / 'audio.wav'), 'rb') as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate()), (1, 16000))

    def test_retry_backoff_60_then_120_with_positive_jitter(self):
        times = []
        def fail(*_):
            times.append(pool.test_clock.now())
            raise rejection()
        pool = self.pool(fail)
        pool.random = lambda: 0.5
        path = self.directory / 'chunk-000.json'
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(t.SubtitleError):
            pool.transcribe(b'offline', path, 10, t.settings(t.parser().parse_args(['episode.wav'])))
        self.assertEqual(times, [0, 66, 198])
        audit = t.read_json(path.with_suffix('.requests.json'))
        self.assertEqual((audit['attempts'], audit['rejections'], audit['retries']), (3, 3, 2))
        self.assertFalse(path.with_suffix('.inflight.json').exists())
        self.assertEqual(audit['diagnostics'][0]['category'], 'request_quota')

    def test_retry_after_and_retry_info_larger_delay_wins(self):
        times = []
        def fake(*_):
            times.append(pool.test_clock.now())
            if len(times) == 1:
                raise rejection(retry_after='150', retry_info='180.5s')
            return response()
        pool = self.pool(fake)
        path = self.directory / 'chunk-000.json'
        with contextlib.redirect_stdout(io.StringIO()):
            pool.transcribe(b'offline', path, 10, t.settings(t.parser().parse_args(['episode.wav'])))
        self.assertEqual(times, [0, 180.5])
        self.assertEqual(t.read_json(path.with_suffix('.requests.json'))['last_outcome'], 'succeeded')

    def test_retry_after_http_date_cap_and_malformed_values(self):
        for header, expected in [(formatdate(180, usegmt=True), 180), ('99999', 300), ('NaN', None),
                                  ('120; Bearer private-sentinel', None)]:
            error = rejection(retry_after=header)
            try:
                diagnostic = t.http_diagnostic(error, wall_clock=lambda: 0)
            finally:
                error.close()
            self.assertEqual(diagnostic.get('retry_after_seconds'), expected)
            self.assertNotIn('private-sentinel', json.dumps(diagnostic))

    def test_shared_cooldown_gates_queued_first_requests(self):
        calls = []
        first_started, let_first_reject = threading.Event(), threading.Event()
        lock = threading.Lock()
        def fake(*_):
            with lock:
                calls.append(pool.test_clock.now())
                first = len(calls) == 1
            if first:
                first_started.set()
                self.assertTrue(let_first_reject.wait(2))
                raise rejection()
            return response()
        pool = self.pool(fake, 1)
        config = t.settings(t.parser().parse_args(['episode.wav']))
        with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=2) as workers:
            a = workers.submit(pool.transcribe, b'offline', self.directory / 'chunk-000.json', 10, config)
            self.assertTrue(first_started.wait(2))
            b = workers.submit(pool.transcribe, b'offline', self.directory / 'chunk-001.json', 10, config)
            let_first_reject.set()
            a.result(timeout=5)
            b.result(timeout=5)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], 0)
        self.assertTrue(all(time >= 60 for time in calls[1:]))

    def test_shared_cooldown_extension_rechecked(self):
        pool = self.pool()
        first = True
        def sleep(seconds):
            nonlocal first
            pool.test_clock.sleep(seconds)
            if first:
                first = False
                pool.cooldown(90)
        pool.sleep = sleep
        pool.cooldown(60)
        pool.wait_for_cooldown()
        self.assertEqual(pool.test_clock.now(), 150)
        self.assertEqual(pool.test_clock.sleeps, [60, 60, 30])

    def test_diagnostics_do_not_leak_identifiers_even_in_allowed_keys(self):
        error = rejection(sensitive='private-sentinel')
        diagnostic = t.http_diagnostic(error)
        error.close()
        self.assertEqual(diagnostic['reason'], 'RATE_LIMIT_EXCEEDED')
        self.assertEqual(diagnostic['quota_limit'], 'GenerateContentRequestsPerMinutePerProjectPerBaseModel')
        self.assertNotIn('private-sentinel', json.dumps(diagnostic))
        malicious = {'error': {'status': 'private-sentinel', 'message': 'private-sentinel',
            'details': [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'reason': 'private-sentinel',
                'metadata': {'quota_metric': 'aiplatform.googleapis.com/private-sentinel',
                             'quota_limit': 'GenerateContentRequestsPerMinutePerProjectprivate-sentinel'}}]}}
        error = urllib.error.HTTPError('https://invalid.test', 429, 'private-sentinel', {},
                                       io.BytesIO(json.dumps(malicious).encode()))
        diagnostic = t.http_diagnostic(error)
        error.close()
        self.assertEqual(diagnostic, {'http_status': 429, 'category': 'resource_exhausted_unspecified'})

    def test_section_boundary_does_not_invent_dialogue(self):
        words = [
            {'id': 0, 'word': 'the', 'start': 0.0, 'end': 0.3, 'speaker': '0:1', 'section': 0},
            {'id': 1, 'word': 'environment', 'start': 0.5, 'end': 1.2, 'speaker': '1:1', 'section': 1},
            {'id': 2, 'word': 'matters.', 'start': 1.2, 'end': 1.6, 'speaker': '1:1', 'section': 1}]
        cues = t.make_cues(words, 2.0)
        t.validate_cues(cues, words, 2.0)
        self.assertEqual([c['text'] for c in cues], ['the', 'environment matters.'])
        self.assertFalse(any(c['text'].startswith('- ') for c in cues))
        self.assertEqual([i for c in cues for i in c['word_ids']], [0, 1, 2])

    def test_vertex_long_metric_identifies_request_quota(self):
        body = {'error': {'details': [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo',
            'metadata': {'quota_metric': 'aiplatform.googleapis.com/generate_content_requests_per_minute_per_project_per_base_model',
                         'consumer': 'projects/private-sentinel'}}]}}
        error = urllib.error.HTTPError('https://invalid.test', 429, 'rejected', {},
            io.BytesIO(json.dumps(body).encode()))
        diagnostic = t.http_diagnostic(error)
        error.close()
        self.assertEqual(diagnostic['category'], 'request_quota')
        self.assertNotIn('private-sentinel', json.dumps(diagnostic))

    def test_known_message_category_and_quota_name_are_sanitized(self):
        body = {'error': {'message': "Quota exceeded for quota metric 'aiplatform.googleapis.com/generate_content_input_tokens' "
                         "and limit 'GenerateContentInputTokensPerDayPerProject' for consumer 'projects/private-sentinel'"}}
        error = urllib.error.HTTPError('https://invalid.test', 429, 'private-sentinel', {},
                                       io.BytesIO(json.dumps(body).encode()))
        diagnostic = t.http_diagnostic(error)
        error.close()
        self.assertEqual(diagnostic['category'], 'daily_quota')
        self.assertEqual(diagnostic['quota_metric'], 'aiplatform.googleapis.com/generate_content_input_tokens')
        self.assertNotIn('private-sentinel', json.dumps(diagnostic))

    def test_episode_result_aggregates_rejections_safely(self):
        video, args, cache = fixture(self.directory)
        def fail(*_):
            raise rejection(sensitive='private-sentinel', retry_info='120s')
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            result = t.process_episode(video, args, self.pool(fail))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['requests']['rejection_counts'], {'429': 3})
        self.assertEqual(result['requests']['retries'], 2)
        self.assertIn('request_quota', result['error'])
        self.assertNotIn('private-sentinel', printed.getvalue())
        for path in cache.glob('*.json'):
            self.assertNotIn('private-sentinel', path.read_text())
        self.assertFalse(video.with_suffix('.en.srt').exists())

    def test_rejection_counts_persist_across_resume(self):
        def fail(*_):
            raise rejection()
        path = self.directory / 'chunk-000.json'
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(t.SubtitleError):
                self.pool(fail).transcribe(b'offline', path, 10, t.settings(t.parser().parse_args(['episode.wav'])))
        audit = t.read_json(path.with_suffix('.requests.json'))
        self.assertEqual((audit['attempts'], audit['rejections'], audit['retries']), (6, 6, 4))

    def test_long_run_token_refresh_before_new_request(self):
        pool = self.pool()
        pool._token_acquired_at = pool.test_clock.now()
        pool.test_clock.sleep(t.TOKEN_REFRESH_SECONDS)
        class Result:
            stdout = 'renewed-offline-token\n'
        with patch.object(t.subprocess, 'run', return_value=Result()) as auth:
            self.assertEqual(pool.token(), 'renewed-offline-token')
            self.assertEqual(pool.token(), 'renewed-offline-token')
        self.assertEqual(auth.call_count, 1)

    def test_auth_acquisition_forces_fresh_token_without_disclosing_output(self):
        pool = self.pool()
        pool._token = None
        result = type('Result', (), {'stdout': 'fresh-synthetic-token\n'})()
        with patch.object(t.subprocess, 'run', return_value=result) as auth:
            self.assertEqual(pool.token(), 'fresh-synthetic-token')
        self.assertEqual(auth.call_args.args[0], ['gcloud', 'config', 'config-helper',
                         '--force-auth-refresh', '--format=value(credential.access_token)'])
        self.assertTrue(auth.call_args.kwargs['capture_output'])

    def test_auth_failure_withholds_secret_stderr(self):
        pool = self.pool()
        pool._token = None
        error = t.subprocess.CalledProcessError(1, ['gcloud'], output='secret-sentinel', stderr='secret-sentinel')
        with patch.object(t.subprocess, 'run', side_effect=error), self.assertRaises(t.SubtitleError) as caught:
            pool.token()
        self.assertEqual(str(caught.exception), 'gcloud authentication failed')
        self.assertIsNone(pool._token)

    def test_terminal_rejection_stops_queued_sections_before_submission(self):
        video, args, cache = fixture(self.directory)
        preparation = t.read_json(cache / 'preparation.json')
        preparation['chunks'] = [{'index': 0, 'start': 0, 'end': 5},
                                 {'index': 1, 'start': 5, 'end': 10}]
        t.atomic_json(cache / 'preparation.json', preparation)
        calls = []
        def reject(*_):
            calls.append(1)
            raise urllib.error.HTTPError('https://invalid.test', 401, 'rejected', {},
                io.BytesIO(b'{"error":{"status":"UNAUTHENTICATED"}}'))
        with contextlib.redirect_stdout(io.StringIO()):
            result = t.process_episode(video, args, self.pool(reject))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(calls, [1])
        self.assertEqual(result['requests']['attempts'], 1)
        self.assertFalse((cache / 'chunk-001.requests.json').exists())
        self.assertFalse((cache / 'chunk-001.inflight.json').exists())
        self.assertFalse((cache / 'chunk-001.json').exists())

    def test_uncertain_request_stops_later_sections_and_preserves_marker(self):
        calls = []
        def unknown(*_):
            calls.append(1)
            raise OSError('secret-sentinel')
        pool = self.pool(unknown)
        configuration = t.settings(t.parser().parse_args(['episode.wav']))
        first, second = self.directory / 'chunk-000.json', self.directory / 'chunk-001.json'
        with self.assertRaises(t.SubtitleError):
            pool.transcribe(b'offline', first, 10, configuration)
        with self.assertRaises(t.SubtitleError) as caught:
            pool.transcribe(b'offline', second, 10, configuration)
        self.assertEqual(calls, [1])
        self.assertTrue(first.with_suffix('.inflight.json').exists())
        self.assertFalse(second.with_suffix('.inflight.json').exists())
        self.assertFalse(second.with_suffix('.requests.json').exists())
        self.assertNotIn('secret-sentinel', str(caught.exception))

    def test_401_never_blindly_replays_audio(self):
        calls = []
        def reject(*_):
            calls.append(1)
            raise urllib.error.HTTPError('https://invalid.test', 401, 'private-sentinel', {},
                io.BytesIO(b'{"error":{"status":"UNAUTHENTICATED","message":"private-sentinel"}}'))
        pool = self.pool(reject)
        path = self.directory / 'chunk-000.json'
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(t.SubtitleError) as caught:
            pool.transcribe(b'offline', path, 10, t.settings(t.parser().parse_args(['episode.wav'])))
        self.assertEqual(calls, [1])
        self.assertIsNone(pool._token)
        self.assertNotIn('private-sentinel', str(caught.exception))
        self.assertEqual(t.read_json(path.with_suffix('.requests.json'))['retries'], 0)

    def test_unknown_language_main_status_exit_and_no_srt(self):
        video, args, cache = fixture(self.directory)
        pool = self.pool()
        with patch.object(t, 'RequestPool', return_value=pool), contextlib.redirect_stdout(io.StringIO()) as printed:
            code = t.main([str(video), '--project', 'test-project', '--model', 'test-transcribe',
                           '--cache-dir', str(args.cache_dir)])
        self.assertEqual(code, 2)
        summary = t.read_json(args.cache_dir / 'summary.json')
        result = summary['results'][0]
        self.assertEqual(summary['counts']['needs_language'], 1)
        self.assertIsNone(result['output'])
        self.assertIsNone(result['original_language'])
        self.assertEqual(result['status'], 'needs_language')
        self.assertIn('--format-only --output-language TAG', printed.getvalue())
        self.assertTrue((cache / 'words.json').exists())
        self.assertTrue((cache / 'cues.json').exists())
        self.assertEqual(list(self.directory.rglob('*.srt')), [])
        self.assertFalse(t.metadata_path(video).exists())

    def test_tagged_publication_metadata_and_no_validation_duplicates(self):
        video, args, cache = fixture(self.directory)
        result = t.process_episode(video, args, self.pool())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['original_language'], 'en')
        self.assertEqual(t.read_metadata(video), {'original_language': 'en', 'original_subtitle': 'episode.en.srt'})
        text = t.metadata_path(video).read_text()
        self.assertIn('\n  "original_language": "en",\n', text)
        self.assertNotIn('test-project', text)
        self.assertNotIn('offline-token', text)
        self.assertEqual(list(cache.rglob('*.srt')), [])
        self.assertEqual([path.name for path in self.directory.glob('*.srt')], ['episode.en.srt'])

    def test_metadata_default_rerun_preserves_reviewed_text_without_auth(self):
        video, args, cache = fixture(self.directory)
        self.assertEqual(t.process_episode(video, args, self.pool())['status'], 'completed')
        subtitle = video.with_suffix('.en.srt')
        reviewed = '1\n00:00:00,100 --> 00:00:01,000\nManually corrected subtitle.\n'
        subtitle.write_text(reviewed)
        args.output_language, args.model = None, 'auto'
        pool = self.pool()
        with patch.object(pool, 'token', side_effect=AssertionError('auth called')), \
             patch.object(t, 'prepare_audio', side_effect=AssertionError('extraction called')):
            result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['original_language'], 'en')
        self.assertEqual(result['output'], str(subtitle.resolve()))
        self.assertEqual(subtitle.read_text(), reviewed)

    def test_known_existing_skip_creates_metadata_and_preserves_extra_fields(self):
        video = self.directory / 'already.wav'
        output = video.with_suffix('.en.srt')
        output.write_text('1\n00:00:00,100 --> 00:00:01,000\nReviewed.\n')
        t.atomic_json(t.metadata_path(video), {'notes': ['retain this'], 'translations': {'fr': 'already.fr.srt'}})
        with patch.object(t.RequestPool, 'token', side_effect=AssertionError('auth called')):
            result = t.process_episode(video, args_for(video, self.directory), self.pool())
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(t.read_metadata(video), {'notes': ['retain this'], 'translations': {'fr': 'already.fr.srt'},
            'original_language': 'en', 'original_subtitle': 'already.en.srt'})

    def test_malformed_metadata_fails_before_auth_and_paid_requests(self):
        video = self.directory / 'malformed.wav'
        args = args_for(video, self.directory)
        for text in ('{not json}', '// JSON5 comment\n{}', '[]', '{"original_language":"bad/tag"}',
                     '{"original_language":"en","original_subtitle":"../outside.en.srt"}',
                     '{"nested":1e999}', '{"original_language":"und"}'):
            t.metadata_path(video).write_text(text)
            pool = self.pool()
            with patch.object(pool, 'token', side_effect=AssertionError('auth called')):
                result = t.process_episode(video, args, pool)
            self.assertEqual(result['status'], 'failed')
            self.assertIn('Malformed subtitle metadata', result['error'])
            self.assertEqual(t.metadata_path(video).read_text(), text)
        with patch.object(t, 'select_project', side_effect=AssertionError('project resolution called')), \
             patch.object(t, 'RequestPool', side_effect=AssertionError('pool initialized')):
            with self.assertRaises(t.SubtitleError):
                t.main([str(video), '--output-language', 'en'])

    def test_language_hint_fallback_and_output_tag_precedence(self):
        video = self.directory / 'language.wav'
        args = args_for(video, self.directory, '--language', 'fr-FR')
        args.output_language = None
        output, language = t.publication(video, args)
        self.assertEqual((output.name, language), ('language.fr-FR.srt', 'fr-FR'))
        args.output_language = 'fr'
        output, language = t.publication(video, args)
        self.assertEqual((output.name, language), ('language.fr.srt', 'fr'))

    def test_unknown_custom_output_does_not_claim_filename_language(self):
        video, args, cache = fixture(self.directory)
        args.output_language = None
        args.output = self.directory / 'chosen.ja.srt'
        result = t.process_episode(video, args, self.pool())
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(args.output.exists())
        self.assertIsNone(result['original_language'])
        self.assertEqual(t.read_metadata(video), {'original_language': None, 'original_subtitle': 'chosen.ja.srt'})

    def test_metadata_does_not_follow_custom_external_path(self):
        video, args, cache = fixture(self.directory)
        external = self.directory / 'external' / 'chosen.en.srt'
        args.output = external
        result = t.process_episode(video, args, self.pool())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(t.read_metadata(video)['original_subtitle'], 'chosen.en.srt')
        metadata = t.read_metadata(video)
        metadata['original_subtitle_path'] = '../external/chosen.en.srt'
        t.atomic_json(t.metadata_path(video), metadata)
        args.output, args.output_language = None, None
        self.assertEqual(t.destination(video, args), video.with_suffix('.en.srt'))
        self.assertFalse(video.with_suffix('.en.srt').exists())

    def test_unlabelled_source_output_forbidden_before_auth(self):
        video = self.directory / 'episode.wav'
        args = args_for(video, self.directory, '--output', str(video.with_suffix('.source.srt')))
        with patch.object(t.RequestPool, 'token', side_effect=AssertionError('auth called')):
            result = t.process_episode(video, args, self.pool())
        self.assertEqual(result['status'], 'failed')
        self.assertIn('drafts are not supported', result['error'])
        self.assertFalse(video.with_suffix('.source.srt').exists())

    def test_metadata_basename_cannot_follow_external_symlink(self):
        video = self.directory / 'episode.wav'
        external = self.directory / 'external' / 'reviewed.en.srt'
        external.parent.mkdir()
        external.write_text('1\n00:00:00,100 --> 00:00:01,000\nReviewed.\n')
        target = video.with_suffix('.en.srt')
        target.symlink_to(external)
        t.atomic_json(t.metadata_path(video), {'original_language': 'en', 'original_subtitle': target.name})
        args = args_for(video, self.directory)
        args.output_language = None
        with self.assertRaisesRegex(t.SubtitleError, 'inside the media folder'):
            t.destination(video, args)


if __name__ == '__main__':
    unittest.main()
