import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'skills/subtitle-creation/scripts'))
import second_opinion as so
from transcribe import RequestPool, SubtitleError, atomic_json

GOOD = {'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'Synthetic speech.'}]}}]}

class OpinionTests(unittest.TestCase):
    def test_private_cache_repo_and_symlink_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'synthetic-repo'; source=root/'skills'/'example'/'scripts'/'helper.py'
            source.parent.mkdir(parents=True); source.write_text('synthetic')
            (root/'.git').mkdir()
            outside=Path(d)/'private-cache'
            link=Path(d)/'source-link'; link.symlink_to(root, target_is_directory=True)
            for cache in (root/'evidence', link/'evidence'):
                with self.assertRaises(SubtitleError): so.private_cache(cache,source)
                self.assertFalse(cache.exists())
            self.assertEqual(so.private_cache(outside,source),outside.resolve())
            (root/'.git').rmdir()
            with self.assertRaises(SubtitleError): so.private_cache(source.parent.parent/'evidence',source)

    def test_public_cache_rejected_before_probe_or_write(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'synthetic-repo'; source=root/'skills'/'example'/'scripts'/'helper.py'
            source.parent.mkdir(parents=True); source.write_text('synthetic')
            (root/'.git').mkdir(); cache=root/'evidence'
            with patch.object(so,'__file__',str(source)), patch.object(so,'probe',side_effect=AssertionError('probed')):
                with self.assertRaises(SubtitleError): so.opinion('unused',cache,0,1,'Synthetic prompt')
            self.assertFalse(cache.exists())

    def test_bounds(self):
        so.bounds(0, 60, 90)
        for values in [(0,61,90),(-1,2,90),(2,2,90),(0,3,2),(float('nan'),2,90),(0,float('inf'),90)]:
            with self.assertRaises(SubtitleError): so.bounds(*values)

    def test_family_versions_and_exclusions(self):
        names=['gemini-3.9-flash','gemini-3.10-flash','gemini-4-flash-image','gemini-9-flash-live','gemini-5-flash-tts','gemini-7-transcribe','gemini-3.10-flash-lite','gemini-3.10-flash-preview-09-22']
        models=[{'name':n} for n in names]+[{'name':'gemini-99-flash','versionState':'DEPRECATED'}]
        self.assertEqual(so.choose_model(models,'flash'),'gemini-3.10-flash')
        self.assertEqual(so.choose_model(models,'flash-lite'),'gemini-3.10-flash-lite')

    def test_cached_generic_before_auth_and_invalid_never_replayed(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'raw.json'; atomic_json(out,GOOD)
            pool=RequestPool(1,request=lambda *_: self.fail('replayed'))
            pool.token=lambda: self.fail('authenticated')
            self.assertEqual(pool.cached_request(out,'url',{},so.response_text),'Synthetic speech.')
            atomic_json(out,{'candidates':[{'finishReason':'MAX_TOKENS'}]})
            for _ in range(2):
                with self.assertRaises(SubtitleError): pool.cached_request(out,'url',{},so.response_text)

    def test_successful_malformed_stops_pool_and_retains_raw(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'raw.json'; calls=[]
            def request(*_): calls.append(1); return {'candidates':[]}
            pool=RequestPool(1,request=request); pool.token=lambda:'fake'
            with self.assertRaises(SubtitleError): pool.cached_request(out,'url',{},so.response_text)
            self.assertTrue(out.exists())
            self.assertFalse(out.with_suffix('.inflight.json').exists())
            with self.assertRaises(SubtitleError): pool.assert_running()
            pool.token=lambda: self.fail('authenticated on reuse')
            with self.assertRaises(SubtitleError): pool.cached_request(out,'url',{},so.response_text)
            self.assertEqual(len(calls),1)

    def test_timeout_marker_prevents_replay(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'raw.json'; calls=[]
            def request(*_): calls.append(1); raise TimeoutError()
            pool=RequestPool(1,request=request); pool.token=lambda:'fake'
            with self.assertRaises(SubtitleError): pool.cached_request(out,'url',{},so.response_text)
            self.assertTrue(out.with_suffix('.inflight.json').exists())
            with self.assertRaises(SubtitleError): RequestPool(1,request=request).cached_request(out,'url',{},so.response_text)
            self.assertEqual(len(calls),1)

    def test_known_rejection_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'raw.json'; calls=[]; now=[0]
            def request(*_):
                calls.append(1)
                raise urllib.error.HTTPError('url',429,'busy',{},io.BytesIO(b'{}'))
            pool=RequestPool(1,request=request,clock=lambda:now[0],sleep=lambda v:now.__setitem__(0,now[0]+v))
            pool.token=lambda:'fake'
            with self.assertRaises(SubtitleError): pool.cached_request(out,'url',{},so.response_text)
            self.assertEqual(len(calls),3)
            self.assertFalse(out.with_suffix('.inflight.json').exists())

    def test_prompt_mismatch_and_pinned_cached_before_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            media=Path(d)/'synthetic'; media.write_bytes(b'synthetic source')
            cache=Path(d)/'cache'; pool=RequestPool(1,project='synthetic',request=lambda *_:GOOD)
            pool.token=lambda:'fake'
            def command(args): Path(args[-1]).write_bytes(b'synthetic clip')
            with patch.object(so,'probe',return_value=10), patch.object(so,'command',side_effect=command), patch.object(so,'catalog',return_value=[{'name':'gemini-3.10-flash-lite'}]):
                self.assertEqual(so.opinion(media,cache,0,5,'Synthetic prompt',pool=pool),'Synthetic speech.')
            with patch.object(so,'probe',return_value=10), patch.object(so,'catalog',side_effect=AssertionError('discovery')):
                self.assertEqual(so.opinion(media,cache,0,5,'Synthetic prompt'),'Synthetic speech.')
                with self.assertRaises(SubtitleError): so.opinion(media,cache,0,5,'Different prompt')
                with self.assertRaises(SubtitleError): so.opinion(media,cache,0,5,'Synthetic prompt',model='gemini-4-flash-lite')

    def synthetic_cache(self, folder):
        media=Path(folder)/'synthetic'; media.write_bytes(b'synthetic source')
        cache=Path(folder)/'cache'
        pool=RequestPool(1,project='synthetic',request=lambda *_:GOOD)
        pool.token=lambda:'fake'
        def command(args): Path(args[-1]).write_bytes(b'synthetic clip')
        with patch.object(so,'probe',return_value=10), patch.object(so,'command',side_effect=command), patch.object(so,'catalog',return_value=[{'name':'gemini-3.10-flash-lite'}]):
            so.opinion(media,cache,0,5,'Synthetic prompt',pool=pool)
        pool.token=lambda: self.fail('authentication on corrupt cache')
        pool.request=lambda *_: self.fail('replayed corrupt cache')
        return media, cache, pool

    def test_corrupt_artifact_manifest_prevents_auth_or_replay(self):
        cases=[{}, [], {'clip.wav':'0'*64}, {'clip.wav':'invalid','request.json':'0'*64},
               {'clip.wav':None,'request.json':'0'*64}, {'../unexpected':'0'*64,'request.json':'0'*64}]
        with tempfile.TemporaryDirectory() as d:
            media,cache,pool=self.synthetic_cache(d)
            for manifest in cases:
                with self.subTest(manifest=manifest):
                    atomic_json(cache/'artifacts.json',manifest)
                    with patch.object(so,'probe',return_value=10), patch.object(so,'catalog',side_effect=AssertionError('discovery')):
                        with self.assertRaises(SubtitleError): so.opinion(media,cache,0,5,'Synthetic prompt',pool=pool)

    def test_corrupt_pinned_model_prevents_auth_or_replay(self):
        with tempfile.TemporaryDirectory() as d:
            media,cache,pool=self.synthetic_cache(d)
            identity=json.loads((cache/'identity.json').read_text())
            for model in (None, 42, 'gemini-3.10-flash', 'gemini-3.10-flash-lite-image', '../unexpected'):
                with self.subTest(model=model):
                    atomic_json(cache/'identity.json',dict(identity,model=model))
                    with patch.object(so,'probe',return_value=10), patch.object(so,'catalog',side_effect=AssertionError('discovery')):
                        with self.assertRaises(SubtitleError): so.opinion(media,cache,0,5,'Synthetic prompt',pool=pool)

    def test_explicit_special_variant_rejected_before_probe(self):
        for name in ('gemini-3-flash-image','gemini-3-flash-tts','gemini-3-flash-lite'):
            with self.assertRaises(SubtitleError):
                so.opinion('unused','unused',0,1,'Synthetic prompt',family='flash',model=name)

    def test_non_thought_stop_only(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'raw.json'
            atomic_json(out,{'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'reasoning','thought':True}]}}]})
            with self.assertRaises(SubtitleError): so.response_text(out)

if __name__ == '__main__': unittest.main()
