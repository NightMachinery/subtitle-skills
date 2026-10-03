import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(sys.argv.pop(1)) if len(sys.argv) > 1 and sys.argv[1].endswith('translate.py') else Path(__file__).resolve().parents[1] / 'skills' / 'subtitle-translation' / 'scripts' / 'translate.py'
SOURCE = '7\n00:00:01,000 --> 00:00:04,000\nRecord your thoughts.\n\n12\n00:00:05,000 --> 00:00:08,000\n- Are you ready?\n- Yes.\n\n15\n00:00:09,000 --> 00:00:12,000\nTry behavioral activation.\n'
TEXTS = ['افکارتان را ثبت کنید.', '- آماده‌اید؟\n- بله.', 'فعال‌سازی رفتاری را امتحان کنید.']

class TranslationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'en.srt'
        self.source.write_text(SOURCE, encoding='utf-8')
        self.work = self.root / 'work'
        self.out = self.root / 'fa.srt'
        self.run_cli('prepare', self.source, '--work-dir', self.work, '--batch-size', 2, '--source-language', 'en', '--target-language', 'fa')
        self.populate()

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def populate(self):
        manifest = json.loads((self.work / 'manifest.json').read_text())
        mapping = dict(zip(['7', '12', '15'], TEXTS))
        for batch in manifest['batches']:
            (self.work / batch['output']).write_text(json.dumps([{'id': ident, 'text': mapping[ident]} for ident in batch['ids']], ensure_ascii=False), encoding='utf-8')

    def assemble(self, success=True, extra=()):
        return self.run_cli('assemble', self.source, '--work-dir', self.work, '--output', self.out, *extra, success=success)

    def test_preserves_timeline_ids_english_and_zwnj(self):
        self.assemble()
        result = self.out.read_text()
        self.assertEqual(self.source.read_text(), SOURCE)
        self.assertEqual([line for line in result.splitlines() if ' --> ' in line], [line for line in SOURCE.splitlines() if ' --> ' in line])
        self.assertEqual([block.splitlines()[0] for block in result.strip().split('\n\n')], ['7', '12', '15'])
        self.assertIn('- آماده‌اید؟\n- بله.', result)
        self.assertIn('فعال‌سازی', result)

    def test_source_changed(self):
        self.source.write_text(SOURCE.replace('Record', 'Write'))
        self.assemble(success=False)
        self.assertFalse(self.out.exists())

    def test_duplicate_and_missing(self):
        batch = self.work / 'batch-0001.output.json'
        for entries in ([{'id': '7', 'text': TEXTS[0]}] * 2, [{'id': '7', 'text': TEXTS[0]}]):
            batch.write_text(json.dumps(entries))
            self.assemble(success=False)
            self.assertEqual(self.source.read_text(), SOURCE)
            self.assertFalse(self.out.exists())

    def test_spanish_target(self):
        spanish_work = self.root / 'spanish-work'
        self.run_cli('prepare', self.source, '--work-dir', spanish_work, '--source-language', 'en', '--target-language', 'es')
        payload = [{'id': ident, 'text': text} for ident, text in zip(['7', '12', '15'], ['Anota tus pensamientos.', '- ¿Estás listo?\n- Sí.', 'Prueba la activación conductual.'])]
        (spanish_work / 'batch-0001.output.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        spanish = self.root / 'episode.es.srt'
        self.run_cli('assemble', self.source, '--work-dir', spanish_work, '--output', spanish)
        self.assertIn('activación conductual', spanish.read_text())
        self.assertEqual(self.source.read_text(), SOURCE)
        manifest = json.loads((spanish_work / 'manifest.json').read_text())
        self.assertEqual(manifest['target_language'], 'es')
        self.assertEqual(manifest['source_language'], 'en')

    def test_no_clobber(self):
        self.assemble()
        before = self.out.read_bytes()
        self.assemble(success=False)
        self.assertEqual(self.out.read_bytes(), before)
        self.assemble(extra=('--overwrite',))
        self.out = self.source
        self.assemble(success=False, extra=('--overwrite',))
        self.assertEqual(self.source.read_text(), SOURCE)

    def test_overflow_and_directional_controls(self):
        batch = self.work / 'batch-0002.output.json'
        for text in ('واژه ' * 100, 'متن\u200f'):
            batch.write_text(json.dumps([{'id': '15', 'text': text}]))
            self.assemble(success=False)
            self.assertFalse(self.out.exists())
            self.assertEqual(self.source.read_text(), SOURCE)

if __name__ == '__main__':
    unittest.main()
