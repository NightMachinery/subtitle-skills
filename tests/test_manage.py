"""Synthetic offline tests for inventory and explicit reviewer decisions."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/subtitle-creation/scripts'
sys.path.insert(0, str(SCRIPTS))
import manage as m

SRT = '1\n00:00:00,100 --> 00:00:01,000\nA synthetic example.\n\n'


class ManageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='subtitle-manage-offline-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.media = self.root / 'media'
        self.media.mkdir()
        self.state = self.root / 'state'
        self.patches = [patch.object(m.t, 'probe', return_value=3),
                        patch.object(m.t, 'command', return_value=types.SimpleNamespace(
                            stdout=json.dumps({'streams': [{'codec_type': 'subtitle'}]}))),
                        patch.object(m.t, 'select_project', side_effect=AssertionError('auth forbidden')),
                        patch.object(m.t.RequestPool, 'transcribe', side_effect=AssertionError('paid request forbidden'))]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(lambda: self.output.__exit__(None, None, None))

    def file(self, name):
        path = self.media / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'synthetic media ' + name.encode())
        return path

    def plan(self, *extra):
        return m.main(['plan', '--root', str(self.media), '--state-dir', str(self.state), *extra])

    def published(self, source, language='en'):
        path = source.with_suffix('.' + language + '.srt')
        path.write_text(SRT)
        m.t.atomic_json(m.t.metadata_path(source), {'original_language': language, 'original_subtitle': path.name,
                                                  'user_field': 'preserved synthetic value'})
        return path

    def queue(self, language='en'):
        source = self.file('series/01-example.wav')
        self.plan()
        native = self.published(source, language) if language else None
        args = m.b.parser().parse_args(['--manifest', str(self.state / 'manifest.json'), '--state-dir', str(self.state), '--once'])
        queue = m.b.Queue(args)
        if language:
            queue.step()
        else:
            entry = queue.entry(queue.jobs[0])
            entry.update(status='awaiting_review', result={'status': 'needs_language'})
            queue.save()
            queue.request(1, queue.jobs[0]['group'], queue.jobs)
        request = self.state / 'review-requests' / ('1-' + queue.jobs[0]['group'] + '.json')
        return queue, request, source, native

    def notes(self, queue, **changes):
        note = {'id': queue.jobs[0]['id'], 'outcome': 'accepted', 'flags': [],
                'provenance': {'reviewer': 'synthetic-reviewer', 'method': 'representative text and flags',
                               'reviewed_at': 'synthetic-review-time', 'samples': ['opening', 'middle', 'ending']}}
        note.update(changes)
        path = self.root / 'notes.json'
        path.write_text(json.dumps({'jobs': [note]}))
        return path

    def accept(self, request, notes):
        return m.main(['accept', '--request', str(request), '--notes', str(notes)])

    def test_plan_orders_numbered_waves_then_extras_and_preserves_outputs(self):
        for series in ('a', 'b'):
            for episode in (0, 1, 4, 5, 9):
                self.file(f'{series}/12345-{episode:02d}-example.mp4')
        source = self.media / 'a/12345-01-example.mp4'
        native = self.published(source)
        before = (native.read_bytes(), m.t.metadata_path(source).read_bytes())
        self.assertEqual(self.plan(), 0)
        manifest, _ = m.b.manifest_for(self.state / 'manifest.json')
        self.assertEqual([job['wave'] for job in manifest['jobs']], [1] * 4 + [2] * 2 + [3] * 2 + [4] * 2)
        self.assertTrue(all(not job['accepted_existing'] for job in manifest['jobs']))
        self.assertTrue(all('-00-' in job['source'] for job in manifest['jobs'][-2:]))
        self.assertEqual(before, (native.read_bytes(), m.t.metadata_path(source).read_bytes()))
        self.assertEqual(self.plan(), 0)

    def test_no_audio_is_explicitly_skipped_before_filename_parsing(self):
        self.file('01.wav')
        silent = self.file('unparsed-silent.mp4')
        with patch.object(m.t, 'probe', side_effect=lambda source: (_ for _ in ()).throw(
                m.t.SubtitleError('Input media has no audio stream')) if source == silent else 3):
            self.plan()
        items = m.b.load(self.state / 'inventory.json')['media']
        self.assertEqual([item['reason'] for item in items if item['status'] == 'skipped'], ['no_audio'])

    def test_unparsed_and_duplicate_episodes_stop_with_private_evidence(self):
        for name in ('01.wav', '01-duplicate.mp4', 'unparsed.wav'):
            self.file(name)
        with self.assertRaisesRegex(m.t.SubtitleError, 'Inventory needs resolution'):
            self.plan()
        reasons = {item.get('reason') for item in m.b.load(self.state / 'inventory.json')['media']}
        self.assertIn('unparsed_or_ambiguous_episode', reasons)
        self.assertIn('duplicate_episode_or_output_ownership', reasons)
        self.assertFalse((self.state / 'manifest.json').exists())

    def test_custom_regex_ambiguous_and_bad_capture_stop(self):
        self.file('episode1-episode2.wav')
        with self.assertRaisesRegex(m.t.SubtitleError, 'Inventory needs resolution'):
            self.plan('--episode-pattern', r'episode(?P<episode>\d+)')
        with self.assertRaisesRegex(m.t.SubtitleError, 'named'):
            self.plan('--episode-pattern', r'(\d+)')

    def test_hidden_cache_and_external_symlink_are_not_inventory_scope(self):
        self.file('01.wav')
        self.file('.hidden/unparsed.wav')
        self.file('cache/unparsed.wav')
        outside = self.root / 'outside.wav'
        outside.write_bytes(b'synthetic outside scope')
        (self.media / '02.wav').symlink_to(outside)
        (self.media / 'linked-directory').symlink_to(self.root, target_is_directory=True)
        self.plan()
        items = m.b.load(self.state / 'inventory.json')['media']
        self.assertEqual(len(items), 2)
        self.assertEqual([item.get('reason') for item in items if item['status'] == 'skipped'], ['symlink_outside_root'])

    def test_plan_rejects_media_inside_public_repository_before_probe(self):
        source = self.file('series/01.wav')
        with patch.object(m, 'PUBLIC_ROOT', self.media), \
                patch.object(m.t, 'probe', side_effect=AssertionError('Public media must not be probed')):
            with self.assertRaisesRegex(m.t.SubtitleError, 'Inventory needs resolution'):
                self.plan()
        inventory = m.b.load(self.state / 'inventory.json')
        self.assertEqual(inventory['media'][0]['source'], str(source))
        self.assertEqual(inventory['media'][0]['status'], 'error')
        self.assertEqual(inventory['media'][0]['reason'], 'media_inside_public_skill_repository')
        self.assertFalse((self.state / 'manifest.json').exists())
        self.assertEqual(list(source.parent.iterdir()), [source])

    def test_plan_rejects_public_media_symlink_even_with_other_eligible_media(self):
        valid = self.file('01.wav')
        public = self.root / 'synthetic-public-repository'
        public.mkdir()
        source = public / '02.wav'
        source.write_bytes(b'synthetic public media')
        (self.media / '02.wav').symlink_to(source)
        def probe(path):
            self.assertEqual(path, valid)
            return 3
        with patch.object(m, 'PUBLIC_ROOT', public), patch.object(m.t, 'probe', side_effect=probe):
            with self.assertRaisesRegex(m.t.SubtitleError, 'Inventory needs resolution'):
                self.plan()
        inventory = m.b.load(self.state / 'inventory.json')['media']
        self.assertEqual([item['reason'] for item in inventory if item['status'] == 'error'],
                         ['media_inside_public_skill_repository'])
        self.assertFalse((self.state / 'manifest.json').exists())
        self.assertEqual(list(public.iterdir()), [source])

    def test_private_boundary_resolves_symlinks_and_nested_artifacts(self):
        alias = self.root / 'public-alias'
        alias.symlink_to(m.PUBLIC_ROOT, target_is_directory=True)
        with self.assertRaisesRegex(m.t.SubtitleError, 'outside this public'):
            m.private(alias / 'never-written')
        private_dir = self.root / 'safe'
        private_dir.mkdir()
        (private_dir / 'packet.json').symlink_to(m.PUBLIC_ROOT / 'never-written.json')
        with self.assertRaisesRegex(m.t.SubtitleError, 'outside this public'):
            m.publish(private_dir / 'packet.json', {'synthetic': True})
        self.assertFalse((m.PUBLIC_ROOT / 'never-written.json').exists())

    def test_replan_never_modifies_active_manifest_or_state(self):
        queue, request, source, native = self.queue()
        paths = [self.state / 'queue-state.json', self.state / 'manifest.json', self.state / 'inventory.json']
        before = [path.read_bytes() for path in paths]
        self.file('series/02.wav')
        with self.assertRaisesRegex(m.t.SubtitleError, 'Existing queue identity'):
            self.plan()
        self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_packet_recovers_cache_flags_and_bounds_samples(self):
        queue, request, source, native = self.queue()
        cache = m.t.source_root(source, None) / 'synthetic-cache'
        m.t.atomic_json(cache / 'identity.json', {'source_sha256': m.b.digest(source)})
        words = [{'word': 'synthetic', 'start': i, 'end': i + .5, 'speaker': str(i % 2)} for i in range(100)]
        m.t.atomic_json(cache / 'words.json', words)
        flags, corrections = [{'kind': 'synthetic_flag'}], [{'kind': 'synthetic_correction'}]
        m.t.atomic_json(cache / 'result.json', {'status': 'completed', 'review_flags': flags, 'corrections': corrections})
        m.t.atomic_json(cache.parent / 'result.json', {'status': 'skipped'})
        work = self.root / 'work'
        m.main(['packet', '--request', str(request), '--work-dir', str(work)])
        data = m.b.load(work / 'packet.json')['jobs'][0]
        self.assertEqual(data['review_flags'], flags)
        self.assertEqual(data['corrections'], corrections)
        self.assertLessEqual(sum(len(sample['items']) for sample in data['samples']['words']), 18)
        self.assertIn('speaker_change_candidate', [sample['region'] for sample in data['samples']['words']])
        with self.assertRaisesRegex(m.t.SubtitleError, 'empty private'):
            m.main(['packet', '--request', str(request), '--work-dir', str(work)])

    def test_packet_rejects_result_cache_from_another_source(self):
        queue, request, source, native = self.queue()
        wrong = self.root / 'other-cache'
        m.t.atomic_json(wrong / 'identity.json', {'source_sha256': '0' * 64})
        m.t.atomic_json(m.t.source_root(source, None) / 'result.json', {'status': 'skipped', 'cache': str(wrong)})
        with self.assertRaisesRegex(m.t.SubtitleError, 'assigned source'):
            m.main(['packet', '--request', str(request), '--work-dir', str(self.root / 'work')])

    def test_changed_source_and_request_identity_reject_packet(self):
        queue, request, source, native = self.queue()
        source.write_bytes(b'changed synthetic source')
        with self.assertRaisesRegex(m.t.SubtitleError, 'Source changed'):
            m.context(request)
        source.write_bytes(b'synthetic media series/01-example.wav')
        data = m.b.load(request)
        data['manifest_sha256'] = '0' * 64
        m.t.atomic_json(request, data)
        with self.assertRaisesRegex(m.t.SubtitleError, 'current queue'):
            m.context(request)

    def test_label_rejects_different_native_and_preserves_same_label(self):
        queue, request, source, native = self.queue()
        metadata = m.t.metadata_path(source)
        before = (native.read_bytes(), metadata.read_bytes())
        with self.assertRaisesRegex(m.t.SubtitleError, 'native language differs'):
            m.main(['label', '--request', str(request), '--job', queue.jobs[0]['id'], '--language', 'es'])
        m.main(['label', '--request', str(request), '--job', queue.jobs[0]['id'], '--language', 'en'])
        self.assertEqual(before, (native.read_bytes(), metadata.read_bytes()))

    def test_label_uses_only_cached_formatter_with_no_auth(self):
        queue, request, source, native = self.queue(language=None)
        cache = m.t.source_root(source, None) / 'synthetic-cache'
        settings = m.t.settings(m.t.parser().parse_args([str(source), '--model', 'synthetic-model']))
        m.t.atomic_json(cache / 'identity.json', {'source_sha256': m.b.digest(source)})
        m.t.atomic_json(cache / 'result.json', {'status': 'needs_language', 'settings': settings, 'cache': str(cache)})
        def formatter(media, args, pool):
            self.assertTrue(args.format_only)
            self.assertFalse(args.overwrite)
            self.assertEqual(args.output_language, 'es')
            self.assertEqual(args.model, 'synthetic-model')
            with self.assertRaisesRegex(m.t.SubtitleError, 'offline'):
                pool.token()
            self.published(media, 'es')
            return {'status': 'completed'}
        with patch.object(m.t, 'process_episode', side_effect=formatter):
            m.main(['label', '--request', str(request), '--job', queue.jobs[0]['id'], '--language', 'es'])
        self.assertTrue(source.with_suffix('.es.srt').exists())
        self.assertFalse(source.with_suffix('.en.srt').exists())

    def test_uncached_label_fails_without_formatter_call(self):
        queue, request, source, native = self.queue(language=None)
        with patch.object(m.t, 'process_episode', side_effect=AssertionError('uncached formatter forbidden')):
            with self.assertRaisesRegex(m.t.SubtitleError, 'No usable cached'):
                m.main(['label', '--request', str(request), '--job', queue.jobs[0]['id'], '--language', 'en'])

    def test_accept_hashes_outputs_and_leaves_active_queue_unchanged(self):
        queue, request, source, native = self.queue()
        before = (self.state / 'queue-state.json').read_bytes()
        notes = self.notes(queue)
        self.accept(request, notes)
        path = self.state / 'review-decisions' / request.name
        decision = m.b.load(path)
        self.assertEqual(decision['manifest_sha256'], queue.identity)
        self.assertEqual(decision['jobs'][0]['source_sha256'], m.b.digest(source))
        self.assertEqual(decision['jobs'][0]['native']['sha256'], m.b.digest(native))
        self.assertEqual(before, (self.state / 'queue-state.json').read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.accept(request, notes)
        notes = self.notes(queue, flags=['different synthetic review'])
        with self.assertRaisesRegex(m.t.SubtitleError, 'artifact differs'):
            self.accept(request, notes)

    def test_accept_requires_actual_notes_provenance_samples_and_exact_ids(self):
        queue, request, source, native = self.queue()
        for changes in ({'provenance': {}}, {'provenance': {'reviewer': 'synthetic', 'method': 'text',
                          'reviewed_at': 'synthetic', 'samples': ['opening']}}, {'id': 'wrong'}, {'outcome': 'failed'}):
            with self.assertRaises(m.t.SubtitleError):
                self.accept(request, self.notes(queue, **changes))
        path = self.root / 'notes.json'
        path.write_text('{}')
        with self.assertRaisesRegex(m.t.SubtitleError, 'explicitly cover'):
            self.accept(request, path)
        self.assertFalse((self.state / 'review-decisions' / request.name).exists())

    def test_foreign_english_timeline_and_sha_guard(self):
        queue, request, source, native = self.queue('es')
        english = source.with_suffix('.en.srt')
        english.write_text(SRT.replace('00:00:01,000', '00:00:01,100'))
        notes = self.notes(queue)
        with self.assertRaisesRegex(m.t.SubtitleError, 'timeline'):
            self.accept(request, notes)
        english.write_text(SRT)
        self.accept(request, notes)
        value = m.b.load(self.state / 'review-decisions' / request.name)['jobs'][0]
        self.assertEqual(value['english']['sha256'], m.b.digest(english))

    def test_accept_hashes_reviewed_corrections_to_newly_generated_output(self):
        source = self.file('series/01.wav')
        self.plan()
        queue = m.b.Queue(m.b.parser().parse_args(['--manifest', str(self.state / 'manifest.json'),
                                                 '--state-dir', str(self.state), '--once']))
        def process(media, args, pool):
            native = self.published(media)
            return {'status': 'completed', 'output_sha256': m.b.digest(native)}
        with patch.object(m.t, 'select_project', return_value='synthetic-project'), \
                patch.object(m.t, 'process_episode', side_effect=process):
            queue.step()
        native = source.with_suffix('.en.srt')
        native.write_text(SRT.replace('A synthetic example.', 'An audio-supported synthetic correction.'))
        request = self.state / 'review-requests' / ('1-' + queue.jobs[0]['group'] + '.json')
        self.accept(request, self.notes(queue))
        decision = m.b.load(self.state / 'review-decisions' / request.name)
        self.assertEqual(decision['jobs'][0]['native']['sha256'], m.b.digest(native))

    def test_accept_ready_group_while_unrelated_episode_is_inflight(self):
        source = self.file('a/01.wav')
        other = self.file('b/01.wav')
        self.plan()
        self.published(source)
        queue = m.b.Queue(m.b.parser().parse_args(['--manifest', str(self.state / 'manifest.json'),
                                                  '--state-dir', str(self.state), '--once']))
        assigned = next(job for job in queue.jobs if job['source'] == str(source))
        unrelated = next(job for job in queue.jobs if job['source'] == str(other))
        queue.entry(assigned).update(status='awaiting_review', result={'status': 'skipped'})
        queue.entry(unrelated).update(status='processing')
        queue.save()
        queue.request(1, assigned['group'], [assigned])
        marker = m.t.source_root(other, None) / 'synthetic-cache/chunk-000.inflight.json'
        m.t.atomic_json(marker, {'status': 'synthetic-inflight'})
        request = self.state / 'review-requests' / ('1-' + assigned['group'] + '.json')
        queue.jobs = [assigned]
        notes = self.notes(queue)
        before = (self.state / 'queue-state.json').read_bytes()
        self.accept(request, notes)
        self.assertEqual(before, (self.state / 'queue-state.json').read_bytes())
        self.assertTrue(marker.exists())

    def test_external_explicit_manifest_is_supported(self):
        queue, request, source, native = self.queue()
        external = self.root / 'explicit-manifest.json'
        (self.state / 'manifest.json').rename(external)
        m.main(['accept', '--request', str(request), '--manifest', str(external), '--notes', str(self.notes(queue))])
        self.assertTrue((self.state / 'review-decisions' / request.name).exists())

    def test_real_cached_label_publication_is_offline(self):
        queue, request, source, native = self.queue(language=None)
        args = m.t.parser().parse_args([str(source), '--model', 'synthetic-model'])
        settings = m.t.settings(args)
        root = m.t.source_root(source, None)
        with m.t.source_lock(root):
            identity, cache = m.t.identity_for(source, root, settings)
            m.t.atomic_json(cache / 'identity.json', identity)
            m.t.atomic_json(cache / 'preparation.json', {'duration': 3, 'silences': [],
                'chunks': [{'index': 0, 'start': 0, 'end': 3}]})
            m.t.atomic_json(cache / 'chunk-000.json', {'candidates': [{'finishReason': 'STOP',
                'content': {'parts': [{'audioTranscription': {'speakerLabel': '1', 'words': [
                    {'word': 'Synthetic', 'startOffset': '0.1s', 'endOffset': '0.5s'},
                    {'word': 'example.', 'startOffset': '0.6s', 'endOffset': '1.1s'}]}}]}}]})
            m.t.atomic_json(cache / 'result.json', {'status': 'needs_language', 'settings': settings, 'cache': str(cache)})
        m.main(['label', '--request', str(request), '--job', queue.jobs[0]['id'], '--language', 'en'])
        self.assertTrue(m.t.valid_srt(source.with_suffix('.en.srt')))
        self.assertEqual(m.t.read_metadata(source)['original_language'], 'en')

    def test_failed_review_publishes_failure_without_mutating_active_state(self):
        queue, request, source, native = self.queue()
        before = (self.state / 'queue-state.json').read_bytes()
        self.accept(request, self.notes(queue, outcome='failed', reason='synthetic unresolved wording'))
        value = m.b.load(self.state / 'review-decisions' / request.name)['jobs'][0]
        self.assertEqual(value['outcome'], 'failed')
        self.assertEqual(before, (self.state / 'queue-state.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
