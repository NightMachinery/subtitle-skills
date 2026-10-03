---
name: subtitle-translation
description: Translate an existing subtitle SRT into any requested language while preserving cue ids, timing, and the original subtitle file. Use for subtitle translation, including additional languages after subtitle creation; do not use to transcribe audio.
---

# Translate existing subtitles

Translate completed SRT cue text, never retranscribe audio for translation. Keep the source subtitle file intact and write a separate language-tagged target file, such as `episode.es.srt`.

Read [the workflow](references/workflow.md), then use the offline helper to prepare batches and validate assembly. `prepare` requires an explicit target language; there is no implicit target. Record the source language when known. Load language-specific advice from `references/languages/` only when relevant to the source or target.

Choose the latest available Sol family model from the runtime model selector, with low reasoning effort, unless the user explicitly selected another model. Translation workers receive fresh context and exclusive response JSON paths. Default to at most two workers; use up to four only when the coordinator explicitly authorizes that concurrency. Do not recursively delegate unless the coordinator explicitly permits it.

The creation skill preserves native-language subtitles and requests English translation for non-English sources unless the user opts out. Additional target languages require a user request. This skill translates whatever source and target the calling task authorizes, with no additional default output.

Assemble only after responses finish. Fix validation or length failures in the assigned response files and rerun; never truncate text or alter source timing to fit a translation. Inspect reading-speed flags and technical terminology before delivery. Do not add API credential setup: the helper is offline and translation uses available native agents.
