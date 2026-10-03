---
name: subtitle-creation
description: Create synchronized subtitles in the audio's original language using Gemini Transcribe on Vertex AI. Process media in parallel with resumable checkpoints, add English for non-English audio unless opted out, and invoke the bundled translation skill for other requested languages.
---

# Subtitle creation

Use the actual audio with Gemini transcription. Format and validate subtitles
locally using the bundled runner. Do not replace transcription with summaries
or claim that a text-only agent listened to the audio.

Always keep subtitles in the audio's own language. If it is not English, also
create an English translation unless the user explicitly opts out. Produce
other target languages only when requested. Save `<media-stem>.<language-tag>.srt`
beside the media, using a suitable language tag such as `en`, `es`, or `ja`.
Never replace the original-language file with a translation. Do not burn
subtitles into the media or alter it unless asked.

## Project and credentials

Project selection, in priority order:

1. The user's explicit project, passed with `--project`.
2. `SUBTITLE_GCP_PROJECT` from the environment.
3. `[vertex] project` in the local TOML configuration.
4. Without a selection, ask for the project before API calls.

The default local file is `~/.config/subtitle-creation/config.toml`; `--config`
or `SUBTITLE_CONFIG` can select another. The repo contains only a generic sample:

```toml
[vertex]
project = "YOUR_PROJECT_ID"
```

There is no hostname check. Real project configuration and credentials stay
outside the repository.

Use existing `gcloud` authentication for Vertex AI. Credentials remain in
memory. Do not read secret shell files, print access tokens, create API keys,
switch accounts, enable services, or change billing to make a run succeed.
An authenticated Google Cloud CLI, ffmpeg, ffprobe, and Python 3.11+ are required.
Inspect local tool availability before launching expensive work.

Select the latest available **Gemini Transcribe** version with the required
word timestamps and speaker detection, not a general Gemini chat model or the
live streaming variant. The helper's default `--model auto` discovers current
versions from Google's model catalog and records the chosen version in the
checkpoint. Check the selected model's official documentation if its API/schema
has changed. Use verbatim transcription with automatic language recognition,
word timestamps, and first-pass speaker detection. `--language` can provide a
user-specified BCP-47 source-language hint. `--model` pins a selected version;
`--location` overrides
the default `global` endpoint. Report the actual selected version, including
preview status. Do not silently change an explicitly selected model after an
access error. On resume, reuse the checkpoint's model so an update does not
repeat paid transcription.

## Run the requested media

Resolve `SKILL_DIR` to the folder containing this file. Run the helper's `--help`
for supported options. A single batch command is usually fastest and cheapest:

```bash
python3 "$SKILL_DIR/scripts/transcribe.py" "episode2.mp4" "episode3.mp4" \
  --episode-workers 4 --request-workers 4
```

Add `--project PROJECT_ID` when the user specifies a project. Use only the
requested files, not a glob that unexpectedly includes trailers or other
episodes. Transcription uploads their audio to Google and consumes the selected
project's quota. Creating subtitles authorizes those necessary API calls;
avoid a separate test call when the established model already works.

The runner splits audio at pauses into short sections, caches completed API
responses, handles concurrent speech using speaker labels, and publishes only
complete validated SRT files. Its initial `<media-stem>.source.srt` is a native
transcript draft, not an English translation. Inspect representative cue text
to identify the spoken language, using detected-language metadata if present.
Do not rely on a container language tag alone. If it is ambiguous or mixed,
preserve the native draft and ask for a labeling preference; do not silently
force English.

Publish the identified source language without retranscription:

```bash
python3 "$SKILL_DIR/scripts/transcribe.py" "episode2.mp4" "episode3.mp4" \
  --format-only --output-language en
```

Replace `en` with the actual language tag. The output tag changes presentation,
not cached transcription settings. Keep the source draft in the checkpoint or
remove that duplicate draft only after the labeled original-language SRT has
been verified; deliver the labeled files. If the output language is known from
an explicit user instruction, pass `--output-language` on the initial command.
For future English-only reruns, specify `--output-language en` to skip existing
English outputs instead of recreating a native draft.

Resume the same command and cache after an interruption. Existing valid outputs
are skipped before authentication
or spend. Use `--overwrite` only when replacing them was requested. Use
`--format-only` to rebuild from saved responses without network calls.

Start with at most four simultaneous Vertex requests across the whole task.
Raise concurrency only for a demonstrated need or user request. The batch runner
enforces one shared request limit inside its process. When the user asks for
parallel subagents, assign disjoint episodes to at most four workers, with
`--episode-workers 1 --request-workers 1` in each; never give every worker an
independent four-request pool. An agent may run several episodes in one batch
instead of launching agents purely to wait on Python.

For mechanical transcription jobs, use an available cheap worker to execute the
runner; transcription quality comes from Gemini, not the coordinating LLM.
Honor an explicitly requested agent model/effort. Keep prompts fresh and limited
to the skill, assigned media, output ownership, and selected configuration.
Workers must not edit the skill, commit files, or process unassigned episodes.

## Translation

After original-language subtitles succeed, invoke **subtitle-translation**, bundled alongside this
skill in the `subtitle-skills` repo. Read its sibling
[SKILL.md](../subtitle-translation/SKILL.md). It uses the latest available Sol
version at low reasoning effort to translate cue text, not the audio. Add English
when the source is not English unless explicitly opted out, plus any other
requested targets. Skip translation entirely when the source is English and no
other language was requested. Never retranscribe for a translation. Keep the
original-language SRT even if translation fails.

## Accept and report

Inspect the runner's result/summary files and flagged cues. Validate positive,
ordered, nonoverlapping times, cue/word preservation, no more than two lines,
and readable line lengths. Parse final files with ffprobe. For each translation, verify
that every source cue has exactly one translated counterpart and identical
timestamp text. Flag rapid cues and inferred timing corrections for review;
do not fabricate wording or timing to conceal them.

Read representative opening, middle/dialogue, and ending cues for each episode.
Checks establish structural validity, not perfect transcription or translation.
Report created original/translated paths, skipped/failed episodes, and material
review flags. If cost is requested, use the returned usage counters and current
official Vertex prices, include successful retries/tests, and label it an
estimate unless checked against the billing ledger. Keep account identifiers
and billing figures out of public repositories.
