"""Reviewed empty-section recovery remains offline, explicit and fail-closed."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from test_transcribe import t, response, fixture


def empty_response():
    return {'candidates': [{'finishReason': 'STOP'}],
            'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 0}}


class NonSpeechTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='subtitle-empty-offline-')
        self.directory = Path(self.temporary.name)
        self.path = self.directory / 'chunk-000.json'

    def tearDown(self):
        self.temporary.cleanup()

    def overlay(self, duration=125, path=None, raw=None):
        path = path or self.path
        t.atomic_json(path, empty_response() if raw is None else raw)
        evidence = []
        start = 0
        while start < duration:
            end = min(start + 60, duration)
            evidence_path = path.parent / f'opinion-{len(evidence)}.json'
            t.atomic_json(evidence_path, {'modelVersion': 'gemini-3-flash-preview',
                'responseId': f'synthetic-{len(evidence)}',
                'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                    {'text': '[NO_SPEECH]'}]}}]})
            evidence.append({'recheck_path': str(evidence_path.resolve()),
                'recheck_sha256': hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                'clip_start_seconds': start, 'clip_end_seconds': end,
                'reviewed_no_speech': True})
            start = end
        overlay = {'version': 1, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'reviewer': 'synthetic reviewer', 'reason': 'Reviewed every bounded clip',
                   'evidence': evidence}
        self.save(overlay, path)
        return overlay

    def save(self, overlay, path=None):
        (path or self.path).with_suffix('.non-speech.json').write_text(json.dumps(overlay))

    def test_default_strict_and_review_preserve_raw_usage_and_evidence(self):
        raw = empty_response()
        t.atomic_json(self.path, raw)
        with self.assertRaisesRegex(t.SubtitleError, 'no timed words'):
            t.read_checkpoint(self.path, 125)
        overlay = self.overlay()
        before = self.path.read_bytes()
        data, corrections = t.read_checkpoint(self.path, 125, include_corrections=True)
        self.assertEqual(data, raw)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(t.SubtitleError, 'no timed words'):
            t.timed_words(data, 125)
        self.assertEqual(t.timed_words(data, 125, allow_empty=True), [])
        self.assertEqual(corrections[0]['kind'], 'reviewed_non_speech')
        self.assertEqual(corrections[0]['evidence'], overlay['evidence'])
        self.assertEqual(t.usage_summary([data])['prompt_tokens_reported'], 12)

    def test_rejects_schema_checksum_review_and_nonfinite_bounds(self):
        cases = [lambda o: o.update(version=True), lambda o: o.update(extra=1),
                 lambda o: o.update(reviewer=' '), lambda o: o.update(reason=''),
                 lambda o: o.update(source_sha256='0'*64), lambda o: o.update(evidence=[]),
                 lambda o: o['evidence'][0].update(extra=1),
                 lambda o: o['evidence'][0].update(reviewed_no_speech=1),
                 lambda o: o['evidence'][0].update(recheck_sha256='0'*64),
                 lambda o: o['evidence'][0].update(clip_start_seconds=False),
                 lambda o: o['evidence'][0].update(clip_end_seconds=float('nan')),
                 lambda o: o['evidence'][0].update(clip_end_seconds=float('inf')),
                 lambda o: o['evidence'][0].update(recheck_path='relative.json')]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                overlay = self.overlay()
                mutate(overlay)
                self.save(overlay)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(self.path, 125)

    def test_duplicate_json_fields_are_rejected_in_overlay_source_and_evidence(self):
        overlay = self.overlay()
        self.path.with_suffix('.non-speech.json').write_text(
            json.dumps(overlay).replace('"version": 1', '"version": 1, "version": 1'))
        with self.assertRaisesRegex(t.SubtitleError, 'Duplicate JSON'):
            t.read_checkpoint(self.path, 125)
        overlay = self.overlay()
        self.path.write_text('{"candidates": [{"finishReason": "STOP", "finishReason": "STOP"}]}')
        overlay['source_sha256'] = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.save(overlay)
        with self.assertRaisesRegex(t.SubtitleError, 'Duplicate JSON'):
            t.read_checkpoint(self.path, 125)
        overlay = self.overlay()
        evidence = overlay['evidence'][0]
        path = Path(evidence['recheck_path'])
        path.write_text(path.read_text().replace('"finishReason": "STOP"',
            '"finishReason": "STOP", "finishReason": "STOP"'))
        evidence['recheck_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.save(overlay)
        with self.assertRaisesRegex(t.SubtitleError, 'Duplicate JSON'):
            t.read_checkpoint(self.path, 125)

    def test_requires_unique_contiguous_complete_bounded_coverage(self):
        cases = [lambda o: o['evidence'].pop(),
                 lambda o: o['evidence'].append(copy.deepcopy(o['evidence'][0])),
                 lambda o: o['evidence'][1].update(clip_start_seconds=60.1),
                 lambda o: o['evidence'][1].update(clip_start_seconds=59.9),
                 lambda o: o['evidence'][0].update(clip_end_seconds=61),
                 lambda o: o['evidence'][0].update(clip_start_seconds=-1),
                 lambda o: o['evidence'][0].update(clip_end_seconds=0),
                 lambda o: o['evidence'][-1].update(clip_end_seconds=125.1),
                 lambda o: o['evidence'][1].update(recheck_path=o['evidence'][0]['recheck_path'],
                     recheck_sha256=o['evidence'][0]['recheck_sha256'])]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                overlay = self.overlay()
                mutate(overlay)
                self.save(overlay)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(self.path, 125)
        overlay = self.overlay()
        overlay['evidence'][1]['clip_start_seconds'] += 0.0000005
        overlay['evidence'].reverse()
        self.save(overlay)
        self.assertEqual(t.read_checkpoint(self.path, 125), empty_response())

    def test_rejects_speech_text_words_and_unsuccessful_or_malformed_source(self):
        sources = [response(), {'candidates': []}, {'candidates': [{'finishReason': 'MAX_TOKENS'}]},
                   {'candidates': [{'finishReason': 'STOP', 'content': None}]},
                   {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'spoken'}]}}]},
                   {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                       {'audioTranscription': {'text': 'spoken', 'words': []}}]}}]},
                   {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                       {'audioTranscription': {'text': 42}}]}}]},
                   {**empty_response(), 'error': {'code': 401}},
                   {**empty_response(), 'promptFeedback': {'blockReason': 'SAFETY'}}]
        for raw in sources:
            with self.subTest(raw=raw):
                self.overlay(raw=raw)
                before = self.path.read_bytes()
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(self.path, 125)
                self.assertEqual(self.path.read_bytes(), before)
        raw = empty_response()
        raw['candidates'][0]['content'] = {'parts': [{'audioTranscription': {'words': [], 'text': ''}}, {'text': '  '}]}
        self.overlay(raw=raw)
        self.assertEqual(t.read_checkpoint(self.path, 125), raw)

    def test_evidence_must_be_successful_flash_nonempty_text_not_recursive(self):
        cases = [lambda d: d.update(modelVersion='gemini-3-flash-lite-preview'),
                 lambda d: d.update(modelVersion='gemini-3-pro-preview'),
                 lambda d: d.pop('modelVersion'),
                 lambda d: d['candidates'][0].update(finishReason='MAX_TOKENS'),
                 lambda d: d.update(error={'code': 503}),
                 lambda d: d['candidates'].append(copy.deepcopy(d['candidates'][0])),
                 lambda d: d['candidates'][0]['content'].update(parts=[]),
                 lambda d: d['candidates'][0]['content']['parts'][0].update(text=''),
                 lambda d: d['candidates'][0]['content']['parts'][0].update(thought=True),
                 lambda d: d['candidates'][0]['content']['parts'][0].update(text=42)]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                overlay = self.overlay()
                evidence = overlay['evidence'][0]
                path = Path(evidence['recheck_path'])
                data = t.read_json(path)
                mutate(data)
                t.atomic_json(path, data)
                evidence['recheck_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
                self.save(overlay)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(self.path, 125)
        overlay = self.overlay()
        overlay['evidence'][0].update(recheck_path=str(self.path), recheck_sha256=overlay['source_sha256'])
        self.save(overlay)
        with self.assertRaises(t.SubtitleError):
            t.read_checkpoint(self.path, 125)
        # No checkpoint recursion: even an overlay beside evidence cannot repair it.
        overlay = self.overlay()
        evidence_path = Path(overlay['evidence'][0]['recheck_path'])
        evidence_path.with_suffix('.non-speech.json').write_text('{}')
        with patch.object(t, 'read_checkpoint', wraps=t.read_checkpoint) as reader:
            reader(self.path, 125)
            self.assertEqual(reader.call_count, 1)

    def test_coexisting_and_orphan_overlays_stop_before_requests(self):
        for suffix in ('.timing-overrides.json', '.word-exclusions.json', '.word-joins.json', '.filler-omissions.json'):
            with self.subTest(suffix=suffix):
                self.overlay()
                other = self.path.with_suffix(suffix)
                other.write_text('{}')
                with self.assertRaisesRegex(t.SubtitleError, 'coexist'):
                    t.read_checkpoint(self.path, 125)
                other.unlink()
        self.overlay()
        self.path.unlink()
        pool = t.RequestPool(1, 'synthetic', request=lambda *_: self.fail('unexpected request'))
        with patch.object(pool, 'token', side_effect=AssertionError('authentication')):
            with self.assertRaises(t.SubtitleError):
                pool.transcribe(b'audio', self.path, 125, {'location': 'global', 'model': 'test'})
        self.assertFalse(self.path.with_suffix('.requests.json').exists())

    def test_fresh_empty_response_is_retained_and_stops_next_request(self):
        calls = []
        def request(*_):
            calls.append(1)
            return empty_response()
        pool = t.RequestPool(1, 'synthetic', request=request)
        pool._token = 'synthetic-token'
        configuration = {'location': 'global', 'model': 'test', 'language': 'auto'}
        with self.assertRaisesRegex(t.SubtitleError, 'no timed words'):
            pool.transcribe(b'audio', self.path, 125, configuration)
        self.assertEqual(t.read_json(self.path), empty_response())
        self.assertEqual(t.read_json(self.path.with_suffix('.requests.json'))['last_outcome'], 'succeeded')
        self.assertFalse(self.path.with_suffix('.inflight.json').exists())
        next_path = self.directory / 'chunk-001.json'
        with self.assertRaisesRegex(t.SubtitleError, 'stopped'):
            pool.transcribe(b'audio', next_path, 125, configuration)
        self.assertEqual(len(calls), 1)
        self.assertFalse(next_path.with_suffix('.requests.json').exists())
        self.assertFalse(next_path.with_suffix('.inflight.json').exists())

    def test_mixed_format_resume_preserves_speech_words_usage_flags_without_network(self):
        video, args, cache = fixture(self.directory)
        args.format_only = True
        preparation = t.read_json(cache / 'preparation.json')
        preparation['chunks'] = [{'index': 0, 'start': 0, 'end': 5}, {'index': 1, 'start': 5, 'end': 10}]
        t.atomic_json(cache / 'preparation.json', preparation)
        path = cache / 'chunk-000.json'
        self.overlay(duration=5, path=path)
        t.atomic_json(cache / 'chunk-001.json', response())
        before = path.read_bytes()
        pool = t.RequestPool(1, 'synthetic', request=lambda *_: self.fail('unexpected request'))
        with patch.object(pool, 'token', side_effect=AssertionError('authentication')):
            self.assertEqual(pool.transcribe(b'', path, 5, t.settings(args)), empty_response())
            result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'completed', result)
        words = t.read_json(cache / 'words.json')
        expected = t.timed_words(response(), 5)
        self.assertEqual([w['word'] for w in words], [w['word'] for w in expected])
        self.assertEqual([w['start'] for w in words], [w['start'] + 5 for w in expected])
        self.assertEqual([w['id'] for w in words], list(range(4)))
        self.assertEqual(result['usage']['prompt_tokens_reported'], 334)
        self.assertEqual(result['corrections'][0]['kind'], 'reviewed_non_speech')
        self.assertEqual(result['review_flags'][0]['global_end'], 5)
        self.assertNotIn('word_id', result['corrections'][0])
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(Path(result['output']).exists())

    def digital_overlay(self, width=2, channels=1, frames=16000, nonzero=False):
        audio_path = self.directory / 'silence.wav'
        with wave.open(str(audio_path), 'wb') as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(width)
            audio.setframerate(16000)
            samples = bytearray(frames * channels * width)
            if nonzero:
                samples[len(samples) // 2] = 1
            audio.writeframes(samples)
        overlay = self.overlay(duration=1)
        overlay['evidence'] = {'kind': 'digital_silence',
                               'audio_path': str(audio_path.resolve()),
                               'audio_sha256': hashlib.sha256(audio_path.read_bytes()).hexdigest()}
        self.save(overlay)
        return overlay, audio_path

    def test_exact_zero_pcm_requires_no_flash_and_retains_proof(self):
        for channels in (1, 2):
            overlay, audio_path = self.digital_overlay(channels=channels)
            before = audio_path.read_bytes()
            for opinion_path in self.directory.glob('opinion-*.json'):
                opinion_path.unlink()
            data, corrections = t.read_checkpoint(self.path, 1, include_corrections=True)
            self.assertEqual(data, empty_response())
            self.assertEqual(corrections[0]['evidence'], overlay['evidence'])
            self.assertEqual(corrections[0]['digital_silence_proof'], {
                'frame_count': 16000, 'sample_rate': 16000, 'channels': channels,
                'sample_width_bytes': 2, 'duration_seconds': 1, 'all_sample_bytes_zero': True})
            self.assertEqual(audio_path.read_bytes(), before)
        self.digital_overlay(frames=15999)
        t.read_checkpoint(self.path, 1)  # One frame of representation tolerance.

    def test_digital_silence_rejects_one_nonzero_byte_width_duration_checksum_and_schema(self):
        for options in ({'nonzero': True}, {'width': 1}, {'width': 3}, {'channels': 3},
                        {'frames': 15998}, {'frames': 0}):
            with self.subTest(options=options):
                self.digital_overlay(**options)
                with self.assertRaises(t.SubtitleError):
                    t.read_checkpoint(self.path, 1)
        for mutate in (lambda e: e.update(audio_sha256='0' * 64),
                       lambda e: e.update(kind='low_energy'),
                       lambda e: e.update(extra=True),
                       lambda e: e.update(audio_path='relative.wav')):
            overlay, _ = self.digital_overlay()
            mutate(overlay['evidence'])
            self.save(overlay)
            with self.assertRaises(t.SubtitleError):
                t.read_checkpoint(self.path, 1)
        overlay, audio_path = self.digital_overlay()
        audio_path.write_bytes(audio_path.read_bytes()[:-2])
        overlay['evidence']['audio_sha256'] = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        self.save(overlay)
        with self.assertRaises(t.SubtitleError):
            t.read_checkpoint(self.path, 1)
        self.digital_overlay()
        with self.assertRaises(t.SubtitleError):
            t.read_checkpoint(self.path, 901)

    def test_whole_empty_episode_fails_without_publishing_srt(self):
        video, args, cache = fixture(self.directory)
        args.format_only = True
        self.overlay(duration=10, path=cache / 'chunk-000.json')
        pool = t.RequestPool(1, 'synthetic', request=lambda *_: self.fail('unexpected request'))
        with patch.object(pool, 'token', side_effect=AssertionError('authentication')):
            result = t.process_episode(video, args, pool)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('empty subtitles are not published', result['error'])
        self.assertEqual(result['usage']['prompt_tokens_reported'], 12)
        self.assertEqual(result['review_flags'][0]['kind'], 'reviewed_non_speech')
        self.assertFalse(Path(result['output']).exists())


if __name__ == '__main__':
    unittest.main()
