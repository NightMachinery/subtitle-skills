import importlib.util
import io
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('subtitle_models',
    str(Path(__file__).resolve().parents[1] / 'skills' / 'subtitle-creation' / 'scripts' / 'models.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Models(unittest.TestCase):
    def test_version_not_lexical(self):
        items = [{'name': 'publishers/google/models/gemini-' + version + '-transcribe'}
                 for version in ['3.9', '3.10']]
        self.assertEqual(m.choose_model(items), 'gemini-3.10-transcribe')

    def test_preview_and_live_and_retired(self):
        items = [{'name': 'gemini-4.1-transcribe-preview'},
                 {'name': 'gemini-4.1-transcribe'},
                 {'name': 'gemini-9-transcribe-live'},
                 {'name': 'gemini-5-transcribe', 'versionState': 'DEPRECATED'}]
        self.assertEqual(m.choose_model(items), 'gemini-4.1-transcribe')

    def test_latest_preview_over_old_stable(self):
        self.assertEqual(m.choose_model([{'name': 'gemini-4-transcribe-preview'},
                                        {'name': 'gemini-3.9-transcribe'}]),
                         'gemini-4-transcribe-preview')

    def test_empty_no_invented_fallback(self):
        with self.assertRaises(RuntimeError): m.choose_model([{'name': 'gemini-4-flash'}])

    def test_pagination_and_header(self):
        responses = [dict(publisherModels=[{'name': 'gemini-3-transcribe'}],nextPageToken='two'),
                     dict(publisherModels=[{'name': 'gemini-4-transcribe'}])]
        seen = []
        def opener(req, timeout):
            seen.append(req)
            return io.BytesIO(json.dumps(responses.pop(0)).encode())
        models = m.catalog('inert-test-token', 'example-project', 'global', opener)
        self.assertEqual(m.choose_model(models), 'gemini-4-transcribe')
        self.assertIn('pageToken=two', seen[1].full_url)


if __name__ == '__main__': unittest.main()
