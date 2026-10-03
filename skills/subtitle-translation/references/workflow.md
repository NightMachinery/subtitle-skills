# Subtitle translation workflow

Work from an existing UTF-8 SRT. Preserve it and produce a separate file tagged with the target language, for example `episode.es.srt`. Target language is mandatory. Source language is optional when it can be inferred from the text.

From this skill directory:

```sh
python3 scripts/translate.py prepare /path/episode.en.srt --source-language en --target-language es --work-dir /path/translation-work --batch-size 80
```

Preparation requires an empty work directory. It emits a manifest containing the exact source hash, cue ids, timing, language labels, numbered input JSON files, prompts, and expected response filenames. Language-specific advice loads automatically when relevant; add a language-tagged file such as `references/languages/es.md` or `pt-br.md` to extend the automatic tips. Base-language and matching regional tips load once each for the source and target. Custom advice can be supplied with `--tips /path/tips.md`.

Inspect the runtime model selector or available-model inventory and choose the latest available Sol family model with low reasoning effort. Respect an explicit user model selection. Do not assume a future model identifier or pin this workflow to one version. Assign each batch to a fresh-context native translator. Default concurrency is two translators, each potentially handling several batch files. The coordinator can explicitly authorize up to four. Each worker owns only its assigned response files. No recursive delegation unless the coordinator explicitly authorizes it.

Use `collaboration.spawn_agent` with `fork_turns: "none"`, `reasoning_effort: "low"`, and the selected model identifier in `model`. Include this full brief, substituting actual paths and languages:

> Translate the supplied source subtitle cue texts from SOURCE_LANGUAGE into TARGET_LANGUAGE. Read the assigned batch prompt and input JSON. Write only the assigned output JSON path, your exclusive response file. Return a UTF-8 JSON array with exactly string `id` and `text` keys: `[{"id":"1","text":"Translated text"}]`. Include every supplied id exactly once, no extra ids, timestamps, code fences, commentary, or invented content. Be concise, faithful, and idiomatic; preserve technical distinctions and relevant supplied terminology. Translate plain semantic text without added formatting. Preserve separate dialogue lines beginning `- ` for two speakers. Preserve legitimate orthography and punctuation without adding directional controls. Do not retranscribe, modify source subtitles, call APIs, or delegate. Report the output path when finished.

Wait for each worker to finish, then assemble:

```sh
python3 scripts/translate.py assemble /path/episode.en.srt --work-dir /path/translation-work --output /path/episode.es.srt
```

Assembly verifies the exact source bytes and cue timing, every id with no duplicates or omissions, nonempty text, and absence of code fences or directional controls. Number-only cues are valid. It preserves source cue ids and timestamp lines. Natural whitespace wrapping defaults to 42 characters per line and at most two lines. Preparation tells the translator this budget; use the same `--width` for preparation and assembly if overriding it. Overflow blocks publication and identifies cues in `review.json` for translator revision. No content is silently truncated. Reading speeds above 20 characters per second create review flags. `--width` and `--max-cps` adjust these heuristics for the target language; they are not verified standards. Unspaced scripts may require revised text or an appropriate width.

Review flagged cues and technical terminology before delivery. Flag suspicious source wording for audio verification by the creation workflow; do not silently invent a correction in translation. If verified source wording changes, prepare against the corrected source and reuse only unchanged text-and-timestamp matches. Failed assembly leaves the source intact. Final publication is atomic, refuses the source path, and refuses an existing target file unless `--overwrite` is explicitly supplied. The helper is offline and needs no API credentials or account configuration.
