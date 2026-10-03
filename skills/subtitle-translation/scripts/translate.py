#!/usr/bin/env python3
"""Offline subtitle translation preparation and validated assembly for any language."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import textwrap
import unicodedata

TIMING = re.compile(r'^(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})$')
DIRECTIONAL = re.compile(r'[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]')
PROMPT = '''Translate only the supplied source subtitle cue texts into the requested target language. Return a UTF-8 JSON array of objects with exactly the keys "id" and "text", preserving every id exactly once. No timestamps, Markdown, code fences, commentary, or invented content. Use concise, faithful, idiomatic translation; preserve technical distinctions. Keep meaning aligned with its source cue: use neighboring cues for context and grammatical flow, but avoid unnecessary clause shifts. Preserve who does or feels what; do not invent subject relationships. Translate semantic text without added formatting. Preserve separate dialogue lines beginning "- " for two speakers. Preserve legitimate orthography and punctuation; do not add directional controls.'''



def read_source(path):
    raw = path.read_bytes()
    content = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    cues = []
    for block in re.split(r'\n[ \t]*\n', content.strip()):
        lines = block.split('\n')
        if len(lines) < 3 or not lines[0].isdigit() or not TIMING.fullmatch(lines[1]):
            raise ValueError('Invalid SRT cue or timestamp')
        if not '\n'.join(lines[2:]).strip():
            raise ValueError('Empty source cue')
        cues.append({'id': lines[0], 'timing': lines[1], 'text': '\n'.join(lines[2:])})
    if not cues or len({c['id'] for c in cues}) != len(cues):
        raise ValueError('Source cue ids must be nonempty and unique')
    return hashlib.sha256(raw).hexdigest(), cues


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def prepare(args):
    digest, cues = read_source(args.source)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    if any(args.work_dir.iterdir()):
        raise ValueError('Preparation requires an empty work directory')
    tips = ''
    if args.tips:
        tips = args.tips.read_text(encoding='utf-8')
    else:
        tip_dir = Path(__file__).resolve().parent.parent / 'references' / 'languages'
        loaded = set()
        aliases = {'persian': 'fa', 'farsi': 'fa'}
        for language in (args.source_language or '', args.target_language):
            tag = aliases.get(language.lower(), language.lower())
            if not re.fullmatch(r'[a-z]{2,8}(?:-[a-z0-9]{1,8})*', tag):
                continue
            for candidate in dict.fromkeys((tag.split('-')[0], tag)):
                tip = tip_dir / (candidate + '.md')
                if tip.is_file() and tip not in loaded:
                    tips += tip.read_text(encoding='utf-8') + '\n'
                    loaded.add(tip)
    prompt = (PROMPT + f' Keep each cue within two lines of {args.width} characters.'
              ' For two-speaker dialogue, keep each speaker on one line within that limit.'
              ' Prefer short natural wording without dropping meaning.'
              '\nSource language: ' + (args.source_language or 'infer from supplied text')
              + '\nTarget language: ' + args.target_language + '\n' + tips)
    batches = []
    for start in range(0, len(cues), args.batch_size):
        batch = cues[start:start + args.batch_size]
        name = f'batch-{len(batches) + 1:04d}'
        input_path = args.work_dir / f'{name}.input.json'
        output_path = args.work_dir / f'{name}.output.json'
        write_json(input_path, [{'id': c['id'], 'text': c['text']} for c in batch])
        (args.work_dir / f'{name}.prompt.txt').write_text(prompt + '\n\nInput: ' + str(input_path.resolve()) + '\nExclusive response output: ' + str(output_path.resolve()) + '\n', encoding='utf-8')
        batches.append({'input': input_path.name, 'output': output_path.name, 'ids': [c['id'] for c in batch]})
    write_json(args.work_dir / 'manifest.json', {'version': 1, 'source_language': args.source_language, 'target_language': args.target_language, 'line_width': args.width, 'source_sha256': digest, 'cues': cues, 'batches': batches})
    print(f'Prepared {len(cues)} cues in {len(batches)} batches. Assign prepared batches to translation agents.')


def seconds(value):
    h, m, rest = value.split(':')
    s, ms = rest.split(',')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def wrap_text(text, width):
    semantic = [line.strip() for line in text.strip().splitlines() if line.strip()]
    dialogue = len(semantic) > 1 and all(line.startswith('- ') for line in semantic)
    if dialogue:
        lines = semantic
    else:
        lines = textwrap.wrap(' '.join(semantic), width=width, break_long_words=False, break_on_hyphens=False)
    # Overflow is a review condition, never a reason to remove content.
    return '\n'.join(lines), len(lines) > 2 or any(len(line) > width for line in lines)


def assemble(args):
    digest, cues = read_source(args.source)
    manifest = json.loads((args.work_dir / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or manifest.get('source_sha256') != digest or manifest.get('cues') != cues:
        raise ValueError('Source changed since preparation; prepare translations again')
    if args.output.resolve() == args.source.resolve() or (args.output.exists() and os.path.samefile(args.output, args.source)):
        raise ValueError('Cannot overwrite source subtitles')
    if args.output.exists() and not args.overwrite:
        raise ValueError('Output exists; use --overwrite explicitly')
    translations = {}
    source_by_id = {cue['id']: cue for cue in cues}
    expected_all = [c['id'] for c in cues]
    assigned = []
    for batch in manifest['batches']:
        assigned.extend(batch['ids'])
        filename = batch['output']
        if Path(filename).name != filename:
            raise ValueError('Batch output must be a filename in the work directory')
        result = json.loads((args.work_dir / filename).read_text(encoding='utf-8'))
        if not isinstance(result, list):
            raise ValueError('Translation response must be a JSON array')
        local = []
        for item in result:
            if not isinstance(item, dict) or set(item) != {'id', 'text'} or not isinstance(item['id'], str) or not isinstance(item['text'], str):
                raise ValueError('Each response item must contain string id and text only')
            ident, text = item['id'], item['text']
            if ident in translations:
                raise ValueError(f'Duplicate translation id: {ident}')
            source_has_content = any(unicodedata.category(char)[0] in {'L', 'N'}
                                     for char in source_by_id.get(ident, {}).get('text', ''))
            target_has_content = any(unicodedata.category(char)[0] in {'L', 'N'} for char in text)
            if not text.strip() or (source_has_content and not target_has_content) or '```' in text or DIRECTIONAL.search(text):
                raise ValueError(f'Invalid translation text for cue {ident}')
            if any(TIMING.fullmatch(line.strip()) for line in text.splitlines()) or any(ord(char) < 32 and char not in '\n\t' for char in text):
                raise ValueError(f'Unexpected timestamp or control character in cue {ident}')
            if ident in source_by_id:
                source_lines = source_by_id[ident]['text'].splitlines()
                if len(source_lines) > 1 and all(line.strip().startswith('- ') for line in source_lines):
                    translated_lines = [line.strip() for line in text.splitlines() if line.strip()]
                    if len(translated_lines) != len(source_lines) or not all(line.startswith('- ') for line in translated_lines):
                        raise ValueError(f'Preserve separate dialogue lines for cue {ident}')
            translations[ident] = text
            local.append(ident)
        if len(local) != len(batch['ids']) or set(local) != set(batch['ids']):
            raise ValueError(f'Missing or extra ids in {filename}')
    if assigned != expected_all or set(translations) != set(expected_all):
        raise ValueError('Manifest or translations have missing, duplicate, or extra ids')
    rendered, flags = [], []
    for cue in cues:
        text, overflow = wrap_text(translations[cue['id']], args.width)
        if overflow:
            flags.append({'id': cue['id'], 'reason': 'length exceeds two lines or configured line width; revise translation'})
        start, end = cue['timing'].split(' --> ')
        duration = seconds(end) - seconds(start)
        if duration <= 0:
            raise ValueError(f'Nonpositive duration for cue {cue["id"]}')
        cps = len(text.replace('\n', '')) / duration
        if cps > args.max_cps:
            flags.append({'id': cue['id'], 'reason': 'reading speed', 'characters_per_second': round(cps, 2)})
        rendered.append(f'{cue["id"]}\n{cue["timing"]}\n{text}\n')
    write_json(args.work_dir / 'review.json', flags)
    if any('length' in flag['reason'] for flag in flags):
        raise ValueError('Length overflow: review.json identifies cues to shorten; no subtitle published')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.translation-', suffix='.srt', dir=args.output.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write('\n'.join(rendered))
            stream.flush()
            os.fsync(stream.fileno())
        if args.overwrite:
            os.replace(temporary, args.output)
        else:
            # Hard-link publication provides atomic no-clobber behavior even if
            # another process creates the destination after the initial check.
            os.link(temporary, args.output)
        print(f'Published {args.output}; {len(flags)} review flags in review.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('source', type=Path)
    prep.add_argument('--work-dir', type=Path, required=True)
    prep.add_argument('--target-language', required=True)
    prep.add_argument('--source-language')
    prep.add_argument('--tips', type=Path)
    prep.add_argument('--batch-size', type=int, default=80)
    prep.add_argument('--width', type=int, default=42)
    final = sub.add_parser('assemble')
    final.add_argument('source', type=Path)
    final.add_argument('--work-dir', type=Path, required=True)
    final.add_argument('--output', type=Path, required=True)
    final.add_argument('--width', type=int, default=42)
    final.add_argument('--max-cps', type=float, default=20)
    final.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare' and not args.target_language.strip():
        parser.error('Target language must not be empty')
    if getattr(args, 'batch_size', 1) < 1 or getattr(args, 'width', 1) < 1 or getattr(args, 'max_cps', 1) <= 0:
        parser.error('Batch size, width, and maximum reading speed must be positive')
    try:
        (prepare if args.action == 'prepare' else assemble)(args)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f'Error: {error}\n')


if __name__ == '__main__':
    main()
