"""Offline invariants. Run: python3 -B tests/test_transcribe.py [path/to/transcribe.py]"""
import importlib.util
import contextlib
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
                                  '--model', 'test-transcribe', *extra])


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
        output = video.with_suffix('.source.srt')
        output.write_text('1\n00:00:00,100 --> 00:00:01,000\nReviewed subtitle.\n')
        pool = self.pool()
        with patch.object(pool, 'token', side_effect=AssertionError('auth called')), \
             patch.object(t, 'prepare_audio', side_effect=AssertionError('extraction called')):
            result = t.process_episode(video, args_for(video, self.directory), pool)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(output.read_text(), '1\n00:00:00,100 --> 00:00:01,000\nReviewed subtitle.\n')

    def test_invalid_existing_output_preserved_without_auth(self):
        video = self.directory / 'episode.wav'
        output = video.with_suffix('.source.srt')
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
        self.assertTrue(t.valid_srt(video.with_suffix('.source.srt')))
        self.assertEqual(result['usage']['input_audio_tokens'], 321)
        self.assertNotIn('test-project', (cache / 'result.json').read_text())
        self.assertNotIn('offline-token', (cache / 'chunk-000.json').read_text())

    def test_confirmed_language_renaming_only_reuses_cache(self):
        video, args, cache = fixture(self.directory)
        original = t.process_episode(video, args, self.pool())
        self.assertEqual(original['status'], 'completed')
        args.format_only, args.output_language = True, 'fr'
        pool = self.pool()
        with patch.object(pool, 'token', side_effect=AssertionError('auth called')):
            tagged = t.process_episode(video, args, pool)
        self.assertEqual(tagged['status'], 'completed')
        self.assertEqual(video.with_suffix('.source.srt').read_bytes(), video.with_suffix('.fr.srt').read_bytes())
        self.assertEqual(original['cache'], tagged['cache'])

    def test_batch_independent_success_and_failure_summary(self):
        video, args, cache = fixture(self.directory)
        missing = self.directory / 'missing.wav'
        pool = self.pool()
        with patch.object(t, 'RequestPool', return_value=pool):
            code = t.main([str(video), str(missing), '--project', 'test-project',
                           '--model', 'test-transcribe', '--cache-dir', str(args.cache_dir)])
        self.assertEqual(code, 1)
        summary = t.read_json(args.cache_dir / 'summary.json')
        self.assertEqual(summary['counts'], {'completed': 1, 'skipped': 0, 'failed': 1})
        self.assertTrue(video.with_suffix('.source.srt').exists())
        self.assertNotIn('test-project', (args.cache_dir / 'summary.json').read_text())

    def test_main_completed_skip_without_project_or_auth(self):
        video = self.directory / 'reviewed.wav'
        video.with_suffix('.source.srt').write_text('1\n00:00:00,100 --> 00:00:01,000\nReviewed.\n')
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
        self.assertFalse(video.with_suffix('.source.srt').exists())

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


if __name__ == '__main__':
    unittest.main()
