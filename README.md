# Subtitle Skills

Two reusable agent skills for creating and translating synchronized subtitles:

- `subtitle-creation` transcribes the actual audio with the latest available Gemini Transcribe on Vertex AI, preserves its original language, and adds English for non-English audio unless the user opts out.
- `subtitle-translation` translates existing SRT cue text with the latest available Sol at low reasoning effort. It preserves the source file, cue IDs, and timestamp lines. Other target languages are opt-in.

All executable helpers and language tips live in this repository. No API keys,
account credentials, media, transcripts, real project configuration, or billing
records belong here.

## Install

Clone the repository into your skills source directory, then link the two skill
folders into the skill directory used by your agent. For example, from the
repository root:

```sh
command mkdir -p "$HOME/.agents/skills"
command ln -s "$PWD/skills/subtitle-creation" "$HOME/.agents/skills/subtitle-creation"
command ln -s "$PWD/skills/subtitle-translation" "$HOME/.agents/skills/subtitle-translation"
```

The creation helper requires Python 3.11+, ffmpeg, ffprobe, and an authenticated
Google Cloud CLI. Translation preparation and assembly use Python's standard
library. An agent runtime with the requested Sol model performs translation.

Keep project configuration in a local file:

```sh
command mkdir -p "$HOME/.config/subtitle-creation"
command cp skills/subtitle-creation/config.example.toml \
  "$HOME/.config/subtitle-creation/config.toml"
```

Edit `[vertex] project` there. `--project` overrides `SUBTITLE_GCP_PROJECT`, which
overrides that file. `--config` or `SUBTITLE_CONFIG` selects another local TOML.
The helper does not inspect hostnames or read secret shell files.

## Use

Ask your agent to use `$subtitle-creation` for the requested media. Its default
is the original language, plus English when needed. Request other target
languages explicitly. To translate an existing SRT without retranscribing,
invoke `$subtitle-translation` and name the target language.

The transcription runner can also be called directly:

```sh
python3 skills/subtitle-creation/scripts/transcribe.py \
  "episode2.mp4" "episode3.mp4" --episode-workers 4 --request-workers 4
```

This creates native transcript drafts named `.source.srt`. After the agent
identifies the source language from the transcript, it publishes tagged native
subtitles with a local rerender, for example:

```sh
python3 skills/subtitle-creation/scripts/transcribe.py \
  "episode2.mp4" "episode3.mp4" --format-only --output-language en
```

The original media remain unchanged. Saved responses make interruptions
resumable. Existing valid outputs are skipped, while `--overwrite` is explicit.
The default model is discovered from the current Google catalog, then pinned in
the run's checkpoints. `--model` can override it. A model update does not cause
completed sections to be transcribed again.

Use at most four concurrent Vertex requests initially. One batch invocation
shares that request budget across episodes. If using parallel subagents, divide
the requested media among at most four workers and give each
`--request-workers 1`; do not multiply the request limit accidentally.

## Validation

Run the offline tests from the repository root:

```sh
python3 -B tests/test_transcribe.py
python3 -B tests/test_translate.py
python3 -B tests/test_models.py
```

The tests exercise source locks, resumability, bounded retries, model selection,
subtitle integrity, and translation timeline preservation without paid API
calls. Structural checks do not prove perfect transcription or translation;
review representative cue text and flagged timings before delivery.
