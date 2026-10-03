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
responses, and handles concurrent speech using speaker labels. It saves only
native language-tagged SRT files, never `.source.srt` drafts. When the spoken
language is not confirmed, it caches native words and cues as JSON and returns
`needs_language` without publishing an SRT. Exit code 2 means labeling is still
needed; 0 means completed/skipped and 1 means failure. Inspect representative text in
`words.json` or `cues.json` to identify the language, using detected-language
metadata if present. Do not rely on a container language tag alone. If it is
ambiguous or mixed, keep the JSON checkpoints and ask for a labeling preference;
do not silently force English.

Publish the identified source language without retranscription:

```bash
python3 "$SKILL_DIR/scripts/transcribe.py" "episode2.mp4" "episode3.mp4" \
  --format-only --output-language en
```

Replace `en` with the actual language tag. The output tag changes presentation,
not cached transcription settings. If the native language is known, pass
`--output-language` on the initial command. The runner records
`original_language` and `original_subtitle` in a pretty-printed
`<media-stem>.subtitles.json5` sidecar, using strict JSON syntax compatible with JSON5 (the reader does not
accept comments or trailing commas).
Future default runs can reuse that native-language label and skip existing valid
outputs before authentication or spend. Keep original and translated SRTs only;
remove redundant drafts from older runs after verifying the tagged originals.

Known HTTP 429 and server rejections get at most two retries, with roughly
60/120-second exponential cooldowns and jitter. Server retry advice is honored
up to five minutes. Queued requests share the cooldown inside one runner
process; separate worker processes do not share it. On repeated throttling,
resume the cached batch with one episode and request worker. Do not loop batches
indefinitely or change billing, projects, or models to evade a rejection.
The per-chunk `.requests.json` audits and episode `requests` summary retain
sanitized rejection categories and attempt counts. Old responses created before
these audits remain usable, but their historical attempt totals are unknown.
An HTTP 429 alone does not prove a particular numerical quota was exceeded.

The shared pool obtains a fresh gcloud token and renews it proactively during
long runs. Authentication rejection, exhausted retries or an unknown outcome
stop queued audio requests. HTTP401 is never blindly retried. Inspect private
request audits before deliberately resuming only missing sections. Already sent
requests may finish; unknown outcomes retain their inflight markers.

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

## Reviewed priority waves

For ordered multi-series work, use the bundled `scripts/batch.py` with an
explicit private manifest and `--state-dir`. For authorized directory inventory,
use `scripts/manage.py plan`; for packet preparation, cached labeling and explicit
review decision publication, use its `packet`, `label` and `accept` commands.
Read [assigned review guidance](references/batch-review.md) for that workflow.

The queue processes serial episodes with one shared request pool (default one
request worker), emits per-group review
requests, and waits until every job in a wave has valid native/required English
subtitles and accepted review decisions before starting the next wave. Agents
confirm language, publish cached native subtitles with `--format-only`, translate
when needed, and record final-file hashes plus representative review provenance.
Existing subtitles stay protected. Resume the same state rather than launching
independent wave processes. An optional private notification argv file wakes a
coordinator after persisted review/failure/completion milestones. See the exact
manifest, decision and resume contract in [queue documentation](../../docs/batch.md).

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
If review finds garbled wording or a possible meaning-changing omission, recheck
one short audio clip with surrounding context, preserving both the original and
new raw responses. The runner can process an extracted clip as standalone media.
Do not infer missing words from plausibility alone. Apply only audio-supported
wording corrections, record their provenance privately, and update the affected
translations. Invalid recheck timestamps do not justify replacing valid original
cue timing. Keep corrected outputs when resuming; a raw-cache rerender can undo
manual wording corrections.
Report created original/translated paths, skipped/failed episodes, and material
review flags. If cost is requested, use the returned usage counters and current
official Vertex prices, include successful retries/tests, and label it an
estimate unless checked against the billing ledger. Keep account identifiers
and billing figures out of public repositories.
