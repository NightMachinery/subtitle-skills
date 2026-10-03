"""Offline priority queue invariants. No authentication or paid calls."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
import wave
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills' / 'subtitle-creation' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('subtitle_batch', SCRIPTS / 'batch.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

SRT = '1\n00:00:00,100 --> 00:00:01,000\nAn offline example.\n\n'


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='subtitle-batch-offline-')
        self.root = Path(self.tmp.name).resolve()
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stderr = contextlib.redirect_stderr(io.StringIO())
        self.stdout.__enter__()
        self.stderr.__enter__()
        self.addCleanup(lambda: self.stdout.__exit__(None, None, None))
        self.addCleanup(lambda: self.stderr.__exit__(None, None, None))
        self.state = self.root / 'state'
        self.state.mkdir()
        self.manifest = self.root / 'manifest.json'
        self.calls = []
        self.patches = [patch.object(b.t, 'probe', return_value=2),
                        patch.object(b.t, 'command', return_value=types.SimpleNamespace(
                            stdout=json.dumps({'streams': [{'codec_type': 'subtitle'}]}))),
                        patch.object(b.t, 'select_project', return_value='offline-project'),
                        patch.object(b.t.RequestPool, 'transcribe', side_effect=AssertionError('paid call forbidden'))]
        for value in self.patches:
            value.start()
        self.addCleanup(lambda: [value.stop() for value in reversed(self.patches)])
        self.addCleanup(self.tmp.cleanup)

    def jobs(self, definitions):
        jobs = []
        for index, (wave, group) in enumerate(definitions):
            media = self.root / f'media{index}.wav'
            media.write_bytes(f'Offline media {index}'.encode())
            jobs.append({'id': f'job{index}', 'wave': wave, 'group': group, 'source': str(media)})
        self.write_manifest(jobs)
        return jobs

    def write_manifest(self, jobs):
        self.manifest.write_text(json.dumps({'version': 1, 'jobs': jobs}))

    def args(self, *extra):
        return b.parser().parse_args(['--manifest', str(self.manifest), '--state-dir', str(self.state),
                                     '--model', 'offline-model', '--once', *extra])

    def publish(self, job, language='en', english=False):
        media = Path(job['source'])
        native = media.with_suffix('.' + language + '.srt')
        native.write_text(SRT)
        b.t.atomic_json(b.t.metadata_path(media), {'original_language': language, 'original_subtitle': native.name})
        if english:
            media.with_suffix('.en.srt').write_text(SRT.replace('An offline example.', 'A translated example.'))
        return native

    def processor(self, jobs, needs_language=False, failed=False):
        by_source = {job['source']: job for job in jobs}
        def process(media, args, pool):
            self.assertEqual(args.language, 'auto')
            self.assertIsNone(args.output_language)
            self.assertFalse(args.overwrite)
            self.calls.append((by_source[str(media)]['id'], pool))
            if failed:
                return {'status': 'failed', 'error': 'offline rejection'}
            if needs_language:
                return {'status': 'needs_language', 'cache': str(self.root / 'cache'), 'review_flags': []}
            self.publish(by_source[str(media)])
            return {'status': 'completed', 'review_flags': []}
        return process

    def review(self, queue, wave, group, *, failed=False, edit=None):
        values = []
        for job in queue.jobs:
            if job['wave'] != wave or job['group'] != group or queue.entry(job)['status'] == 'accepted_existing':
                continue
            native, language = queue.native(job)
            item = {'id': job['id'], 'source_sha256': queue.entry(job)['source_identity']['sha256'],
                    'outcome': 'failed' if failed else 'accepted', 'flags': [],
                    'provenance': {'reviewer': 'offline-reviewer', 'method': 'representative text',
                                   'samples': ['opening', 'middle', 'ending'], 'reviewed_at': 'offline'}}
            if failed:
                item['reason'] = 'offline review failure'
            else:
                item.update(original_language=language, native=b.checked_subtitle(native),
                            english=b.checked_subtitle(Path(job['source']).with_suffix('.en.srt'))
                            if queue.english_required(language) else None)
            if edit:
                edit(item)
            values.append(item)
        path = self.state / 'review-decisions' / f'{wave}-{group}.json'
        b.t.atomic_json(path, {'version': 1, 'manifest_sha256': queue.identity,
                             'wave': wave, 'group': group, 'jobs': values})
        return path

    def test_wave_order_barrier_and_shared_pool(self):
        jobs = self.jobs([(2, 'a'), (1, 'b'), (1, 'a'), (1, 'a')])
        queue = b.Queue(self.args())
        with patch.object(b.t, 'process_episode', side_effect=self.processor(jobs)):
            self.assertEqual(queue.step(), 2)
            self.assertEqual([value[0] for value in self.calls], ['job2', 'job3', 'job1'])
            self.review(queue, 1, 'a')
            self.assertEqual(queue.step(), 2)
            self.assertEqual(len(self.calls), 3)
            self.review(queue, 1, 'b')
            self.assertEqual(queue.step(), 2)
            self.assertEqual([value[0] for value in self.calls], ['job2', 'job3', 'job1', 'job0'])
            self.review(queue, 2, 'a')
            self.assertEqual(queue.step(), 0)
        self.assertTrue(all(pool is queue.pool for _, pool in self.calls))
        self.assertTrue((self.state / 'completed.json').exists())

    def test_group_request_ready_before_next_group(self):
        jobs = self.jobs([(1, 'a'), (1, 'b')])
        process = self.processor(jobs)
        def checked(media, args, pool):
            if str(media) == jobs[1]['source']:
                self.assertTrue((self.state / 'review-requests' / '1-a.json').exists())
            return process(media, args, pool)
        with patch.object(b.t, 'process_episode', side_effect=checked):
            self.assertEqual(b.Queue(self.args()).step(), 2)

    def test_needs_language_continues_wave_but_resume_does_not_reprocess(self):
        jobs = self.jobs([(1, 'a'), (1, 'a'), (2, 'a')])
        with patch.object(b.t, 'process_episode', side_effect=self.processor(jobs, needs_language=True)):
            queue = b.Queue(self.args())
            self.assertEqual(queue.step(), 2)
            self.assertEqual(len(self.calls), 2)
            resumed = b.Queue(self.args())
            self.assertEqual(resumed.step(), 2)
            self.assertEqual(len(self.calls), 2)
            for job in jobs[:2]:
                self.publish(job)
            self.review(resumed, 1, 'a')
            self.assertEqual(resumed.step(), 2)
            self.assertEqual(len(self.calls), 3)

    def test_resume_existing_and_exact_review_hashes(self):
        jobs = self.jobs([(1, 'a')])
        with patch.object(b.t, 'process_episode', side_effect=self.processor(jobs)):
            queue = b.Queue(self.args())
            queue.step()
            self.review(queue, 1, 'a')
            self.assertEqual(b.Queue(self.args()).step(), 0)
            self.assertEqual(b.Queue(self.args()).step(), 0)
        self.assertEqual(len(self.calls), 1)
        Path(jobs[0]['source']).with_suffix('.en.srt').write_text(SRT.replace('example', 'changed'))
        with self.assertRaisesRegex(b.t.SubtitleError, 'Accepted native subtitle changed'):
            b.Queue(self.args())

    def test_prior_accepted_existing_is_distinct_and_untouched(self):
        jobs = self.jobs([(1, 'a')])
        native = self.publish(jobs[0])
        metadata = b.t.metadata_path(Path(jobs[0]['source']))
        before = (native.read_bytes(), metadata.read_bytes())
        jobs[0]['accepted_existing'] = True
        self.write_manifest(jobs)
        with patch.object(b.t, 'process_episode', side_effect=AssertionError('existing subtitle should skip')):
            queue = b.Queue(self.args())
            self.assertEqual(queue.step(), 0)
        self.assertEqual(queue.entry(jobs[0])['status'], 'accepted_existing')
        self.assertTrue(queue.entry(jobs[0])['prior_accepted_existing'])
        self.assertEqual(before, (native.read_bytes(), metadata.read_bytes()))

    def test_existing_unreviewed_requires_decision_without_transcription(self):
        jobs = self.jobs([(1, 'a')])
        self.publish(jobs[0])
        with patch.object(b.t, 'process_episode', side_effect=AssertionError('existing subtitle should skip')):
            queue = b.Queue(self.args())
            self.assertEqual(queue.step(), 2)
            self.review(queue, 1, 'a')
            self.assertEqual(queue.step(), 0)

    def test_foreign_native_requires_english_and_exact_timeline(self):
        jobs = self.jobs([(1, 'a')])
        self.publish(jobs[0], 'es')
        queue = b.Queue(self.args())
        self.assertEqual(queue.step(), 2)
        self.assertFalse(Path(jobs[0]['source']).with_suffix('.en.srt').exists())
        self.publish(jobs[0], 'es', english=True)
        path = self.review(queue, 1, 'a')
        english = Path(jobs[0]['source']).with_suffix('.en.srt')
        english.write_text(english.read_text().replace('00:00:01,000', '00:00:01,100'))
        decision = b.load(path)
        decision['jobs'][0]['english'] = b.checked_subtitle(english)
        b.t.atomic_json(path, decision)
        with self.assertRaisesRegex(b.t.SubtitleError, 'timeline'):
            queue.step()

    def test_foreign_accepted_existing_still_requires_translation_review(self):
        jobs = self.jobs([(1, 'a')])
        self.publish(jobs[0], 'es', english=True)
        jobs[0]['accepted_existing'] = True
        self.write_manifest(jobs)
        queue = b.Queue(self.args())
        self.assertEqual(queue.step(), 2)
        self.review(queue, 1, 'a')
        self.assertEqual(queue.step(), 0)
        self.assertTrue(queue.entry(jobs[0])['prior_accepted_existing'])

    def test_explicit_english_optout(self):
        jobs = self.jobs([(1, 'a')])
        self.publish(jobs[0], 'ja')
        queue = b.Queue(self.args('--no-english'))
        self.assertEqual(queue.step(), 2)
        self.review(queue, 1, 'a')
        self.assertEqual(queue.step(), 0)

    def test_failure_halts_and_persists_without_paid_retry(self):
        jobs = self.jobs([(1, 'a'), (1, 'b'), (2, 'a')])
        with patch.object(b.t, 'process_episode', side_effect=self.processor(jobs, failed=True)):
            self.assertEqual(b.main(['--manifest', str(self.manifest), '--state-dir', str(self.state), '--once']), 1)
            self.assertEqual(b.main(['--manifest', str(self.manifest), '--state-dir', str(self.state), '--once']), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.state / 'failed.json').exists())

    def test_failed_review_stops_pending_group_before_paid_work(self):
        jobs = self.jobs([(1, 'a'), (1, 'b')])
        queue = b.Queue(self.args())
        self.review(queue, 1, 'b', failed=True)
        with patch.object(b.t, 'process_episode', side_effect=AssertionError('paid work after review failure')):
            with self.assertRaisesRegex(b.t.SubtitleError, 'Review failed'):
                queue.step()

    def test_bad_native_hash_or_provenance_rejected(self):
        jobs = self.jobs([(1, 'a')])
        self.publish(jobs[0])
        queue = b.Queue(self.args())
        queue.step()
        self.review(queue, 1, 'a', edit=lambda item: item['native'].update(sha256='0' * 64))
        with self.assertRaisesRegex(b.t.SubtitleError, 'hash'):
            queue.step()
        self.review(queue, 1, 'a', edit=lambda item: item['provenance'].update(samples=['opening']))
        with self.assertRaisesRegex(b.t.SubtitleError, 'opening, middle and ending'):
            queue.step()

    def test_duplicate_ids_sources_and_destination_collisions(self):
        jobs = self.jobs([(1, 'a'), (1, 'a')])
        for variant in (dict(jobs[1], id=jobs[0]['id']), dict(jobs[1], source=jobs[0]['source'])):
            self.write_manifest([jobs[0], variant])
            with self.assertRaisesRegex(b.t.SubtitleError, 'Duplicate'):
                b.manifest_for(self.manifest)
        collision = Path(jobs[0]['source']).with_suffix('.mp4')
        collision.write_bytes(b'offline')
        self.write_manifest([jobs[0], dict(jobs[1], source=str(collision))])
        with self.assertRaisesRegex(b.t.SubtitleError, 'overlapping'):
            b.manifest_for(self.manifest)

    def test_manifest_source_and_settings_changes_rejected(self):
        jobs = self.jobs([(1, 'a')])
        b.Queue(self.args())
        self.write_manifest([dict(jobs[0], wave=2)])
        with self.assertRaisesRegex(b.t.SubtitleError, 'manifest identity changed'):
            b.Queue(self.args())
        self.write_manifest(jobs)
        with self.assertRaisesRegex(b.t.SubtitleError, 'model, location'):
            b.Queue(self.args('--model', 'changed-model'))
        Path(jobs[0]['source']).write_bytes(b'changed offline media')
        with self.assertRaisesRegex(b.t.SubtitleError, 'Source changed'):
            b.Queue(self.args())

    def test_inflight_or_prior_failure_prevents_any_paid_call(self):
        jobs = self.jobs([(1, 'a'), (2, 'a')])
        root = b.t.source_root(Path(jobs[1]['source']), None)
        marker = root / 'offline-cache' / 'chunk-000.inflight.json'
        b.t.atomic_json(marker, {'state': 'request-started'})
        with patch.object(b.t, 'process_episode', side_effect=AssertionError('paid work after uncertain outcome')):
            with self.assertRaisesRegex(b.t.SubtitleError, 'uncertain'):
                b.Queue(self.args())
        marker.unlink()
        b.t.atomic_json(root / 'result.json', {'status': 'failed'})
        with self.assertRaisesRegex(b.t.SubtitleError, 'previous episode failed'):
            b.Queue(self.args())

    def test_real_cached_transcription_language_publication_and_resume_are_offline(self):
        jobs = self.jobs([(1, 'a')])
        media = Path(jobs[0]['source'])
        with wave.open(str(media), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\0\0' * 16000 * 5)
        args = b.t.parser().parse_args([str(media), '--model', 'offline-model', '--request-workers', '1'])
        root = b.t.source_root(media, None)
        with b.t.source_lock(root):
            identity, cache = b.t.identity_for(media, root, b.t.settings(args))
            cache.mkdir(parents=True)
            b.t.atomic_json(cache / 'identity.json', identity)
            (cache / 'audio.wav').write_bytes(media.read_bytes())
            b.t.atomic_json(cache / 'preparation.json', {'duration': 5, 'silences': [],
                'chunks': [{'index': 0, 'start': 0, 'end': 5}]})
            b.t.atomic_json(cache / 'chunk-000.json', {'candidates': [{'finishReason': 'STOP',
                'content': {'parts': [{'audioTranscription': {'speakerLabel': '1', 'words': [
                    {'word': 'An', 'startOffset': '0.1s', 'endOffset': '0.5s'},
                    {'word': 'offline', 'startOffset': '0.6s', 'endOffset': '1.1s'},
                    {'word': 'example.', 'startOffset': '1.2s', 'endOffset': '1.8s'}]}}]}}]})
        queue = b.Queue(self.args())
        self.assertEqual(queue.step(), 2)
        self.assertEqual(queue.entry(jobs[0])['result']['status'], 'needs_language')
        self.assertFalse(media.with_suffix('.en.srt').exists())
        publish_args = b.t.parser().parse_args([str(media), '--model', 'offline-model',
                                              '--format-only', '--output-language', 'en'])
        result = b.t.process_episode(media, publish_args, queue.pool)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(b.load(root / 'result.json')['status'], 'completed')
        with patch.object(b.t, 'process_episode', side_effect=AssertionError('resume should only inspect publication')):
            resumed = b.Queue(self.args())
            self.review(resumed, 1, 'a')
            self.assertEqual(resumed.step(), 0)

    def test_interrupted_processing_resumes_once_using_same_pool(self):
        jobs = self.jobs([(1, 'a')])
        queue = b.Queue(self.args())
        queue.entry(jobs[0])['status'] = 'processing'
        queue.save()
        resumed = b.Queue(self.args())
        with patch.object(b.t, 'process_episode', side_effect=self.processor(jobs)):
            self.assertEqual(resumed.step(), 2)
            self.assertEqual(resumed.step(), 2)
        self.assertEqual(len(self.calls), 1)

    def test_notification_ordering_no_shell_and_no_resume_replay(self):
        jobs = self.jobs([(1, 'a')])
        self.publish(jobs[0])
        command_file = self.root / 'notify.json'
        command_file.write_text(json.dumps(['offline-notifier', 'literal;argument']))
        events = []
        def callback(argv, **kwargs):
            event = json.loads(kwargs['input'])
            self.assertEqual(argv, ['offline-notifier', 'literal;argument'])
            self.assertIs(kwargs['shell'], False)
            self.assertEqual(kwargs['timeout'], 30)
            self.assertIs(kwargs['stdout'], b.subprocess.DEVNULL)
            self.assertIs(kwargs['stderr'], b.subprocess.DEVNULL)
            if event['event'] == 'review_requested':
                self.assertTrue(Path(event['request']).is_file())
            if event['event'] == 'queue_completed':
                self.assertTrue((self.state / 'completed.json').is_file())
            if event['event'] == 'queue_failed':
                self.assertTrue((self.state / 'failed.json').is_file())
            events.append(event['event'])
        args = self.args('--notify-command-file', str(command_file))
        with patch.object(b.subprocess, 'run', side_effect=callback):
            queue = b.Queue(args)
            queue.step()
            b.Queue(args).step()
            self.assertEqual(events, ['review_requested'])
            self.review(queue, 1, 'a')
            queue.step()
            b.Queue(args).step()
            self.assertEqual(events, ['review_requested', 'queue_completed'])
            queue.fail(b.t.SubtitleError('offline failure'))
            self.assertEqual(events[-1], 'queue_failed')

    def test_notification_failure_is_sanitized_and_does_not_fail_queue(self):
        jobs = self.jobs([(1, 'a')])
        self.publish(jobs[0])
        command_file = self.root / 'notify.json'
        command_file.write_text(json.dumps(['offline-notifier']))
        with patch.object(b.subprocess, 'run', side_effect=b.subprocess.TimeoutExpired('secret-sentinel', 30)):
            queue = b.Queue(self.args('--notify-command-file', str(command_file)))
            self.assertEqual(queue.step(), 2)
        self.assertEqual(queue.state['status'], 'waiting_review')
        self.assertNotIn('secret-sentinel', (self.state / 'events.jsonl').read_text())
        self.assertIn('notify_failed', (self.state / 'events.jsonl').read_text())
        self.assertFalse((self.state / 'failed.json').exists())

    def test_notification_bad_argv_fails_before_work(self):
        self.jobs([(1, 'a')])
        command_file = self.root / 'notify.json'
        command_file.write_text(json.dumps({'shell': 'forbidden'}))
        with self.assertRaisesRegex(b.t.SubtitleError, 'argv'):
            b.Queue(self.args('--notify-command-file', str(command_file)))

    def test_literal_bracket_media_names_protect_only_matching_subtitles(self):
        jobs = self.jobs([(1, 'a')])
        source = self.root / 'episode[1]?.wav'
        source.write_bytes(b'offline bracket filename')
        jobs[0]['source'] = str(source)
        self.write_manifest(jobs)
        native = self.publish(jobs[0])
        unrelated = self.root / 'episode1x.en.srt'
        unrelated.write_text(SRT)
        queue = b.Queue(self.args())
        protected = queue.entry(jobs[0])['protected_outputs']
        self.assertEqual(set(protected), {str(native)})
        unrelated.write_text('unrelated change')
        queue.assert_safe()
        native.write_text(SRT.replace('example', 'changed'))
        with self.assertRaisesRegex(b.t.SubtitleError, 'existing subtitle was changed'):
            queue.assert_safe()

    def test_late_other_group_failed_review_stops_next_episode(self):
        jobs = self.jobs([(1, 'a'), (1, 'a'), (1, 'b')])
        queue = b.Queue(self.args())
        process = self.processor(jobs)
        def late_review(media, args, pool):
            result = process(media, args, pool)
            self.review(queue, 1, 'b', failed=True)
            return result
        with patch.object(b.t, 'process_episode', side_effect=late_review):
            with self.assertRaisesRegex(b.t.SubtitleError, 'Review failed'):
                queue.step()
        self.assertEqual([item[0] for item in self.calls], ['job0'])

    def test_unchanged_subtitles_reuse_ffprobe_validation_cache(self):
        jobs = self.jobs([(1, 'a')])
        self.publish(jobs[0])
        jobs[0]['accepted_existing'] = True
        self.write_manifest(jobs)
        with patch.object(b.t, 'command', return_value=types.SimpleNamespace(
                stdout=json.dumps({'streams': [{'codec_type': 'subtitle'}]}))) as probe:
            queue = b.Queue(self.args())
            self.assertEqual(queue.step(), 0)
            for _ in range(3):
                queue.assert_safe()
                self.assertEqual(queue.step(), 0)
            self.assertEqual(probe.call_count, 1)

    def test_corrupted_completion_or_missing_saved_review_cannot_resume(self):
        jobs = self.jobs([(1, 'a')])
        self.publish(jobs[0])
        queue = b.Queue(self.args())
        queue.step()
        self.review(queue, 1, 'a')
        queue.step()
        queue.entry(jobs[0])['decision'] = {}
        queue.save()
        with self.assertRaisesRegex(b.t.SubtitleError, 'valid saved review'):
            b.Queue(self.args())
        queue.entry(jobs[0])['status'] = 'pending'
        queue.save()
        with self.assertRaisesRegex(b.t.SubtitleError, 'unaccepted episode'):
            b.Queue(self.args())

    def test_failed_review_after_first_chunk_stops_next_request_without_marker(self):
        # Exercise the real request pool; replace its network callable only.
        self.patches[3].stop()
        jobs = self.jobs([(1, 'a'), (1, 'b')])
        media = Path(jobs[0]['source'])
        with wave.open(str(media), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\0\0' * 16000 * 10)
        args = b.t.parser().parse_args([str(media), '--model', 'offline-model', '--request-workers', '1'])
        root = b.t.source_root(media, None)
        with b.t.source_lock(root):
            identity, cache = b.t.identity_for(media, root, b.t.settings(args))
            cache.mkdir(parents=True)
            b.t.atomic_json(cache / 'identity.json', identity)
            (cache / 'audio.wav').write_bytes(media.read_bytes())
            b.t.atomic_json(cache / 'preparation.json', {'duration': 10, 'silences': [],
                'chunks': [{'index': 0, 'start': 0, 'end': 5}, {'index': 1, 'start': 5, 'end': 10}]})
        queue = b.Queue(self.args())
        queue.pool._token = 'offline-token'
        calls = []
        def request(*_):
            calls.append(1)
            self.review(queue, 1, 'b', failed=True)
            return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                {'audioTranscription': {'speakerLabel': '1', 'words': [
                    {'word': 'Offline.', 'startOffset': '0.1s', 'endOffset': '0.6s'}]}}]}}]}
        queue.pool.request = request
        with self.assertRaisesRegex(b.t.SubtitleError, 'Episode failed') as failure:
            queue.step()
        queue.fail(failure.exception)
        self.assertEqual(calls, [1])
        self.assertTrue((cache / 'chunk-000.json').exists())
        self.assertFalse((cache / 'chunk-001.json').exists())
        self.assertFalse(list(cache.glob('*.inflight.json')))
        self.assertFalse((cache / 'chunk-001.requests.json').exists())
        self.assertTrue((self.state / 'failed.json').exists())
        self.assertEqual(queue.entry(jobs[1])['status'], 'failed')
        self.assertIn('Review failed', queue.entry(jobs[0])['result']['error'])

    def test_queue_lock_and_private_mode(self):
        with b.queue_lock(self.state):
            with self.assertRaisesRegex(b.t.SubtitleError, 'Another coordinator'):
                with b.queue_lock(self.state):
                    pass
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_invalid_existing_or_preaccepted_missing_native_fails_before_processing(self):
        jobs = self.jobs([(1, 'a')])
        jobs[0]['accepted_existing'] = True
        self.write_manifest(jobs)
        with self.assertRaisesRegex(b.t.SubtitleError, 'requires a valid confirmed'):
            b.Queue(self.args())
        native = self.publish(jobs[0])
        native.write_text('invalid offline subtitle')
        with self.assertRaisesRegex(b.t.SubtitleError, 'invalid'):
            b.Queue(self.args())


if __name__ == '__main__':
    unittest.main()
