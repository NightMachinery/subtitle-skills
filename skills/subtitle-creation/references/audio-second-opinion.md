# Bounded audio wording opinion

Use `scripts/second_opinion.py` for independent text evidence about a short
unclear passage. Start with the latest available Flash-Lite for an inexpensive
wording check. If an important number or semantic disagreement remains, obtain
one Flash opinion for the same bounded passage. Automatic model selection uses
the read-only catalog and pins the selected model in the private cache.

```sh
python3 scripts/second_opinion.py input.mp4 --start 10 --end 30 --cache /tmp/opinion-cache
```

A clip must be within the media and at most 60 seconds. `--family flash` selects
Flash; `--model` explicitly selects a verified identifier without fallback.
`--prompt-file` accepts a private user prompt verbatim without sourcing shell
configuration. The bundled prompt detects any language and produces clean text.
Do not seed the prompt with the original transcript or candidate corrections.
Project selection follows the creation runner; `--location` defaults to global.

Compare this evidence independently to original wording. Clean transcription
may remove disfluencies and is not proof of full verbatim completeness or timing.
It never supplies timestamp anchors, replaces the original transcript, writes
SRT files, or automatically applies corrections. Timing repair still requires
Gemini Transcribe indexed words. Protect approved subtitles. When the user asks
for best effort, preserve uncertainty flags and make supported improvements
rather than imposing human review as a condition of completion.

The helper rejects cache paths inside its source repository, including symlinked
destinations, before probing media or accessing authentication. A copied skill
without Git metadata protects its skill source directory.

Keep the cache outside the public repository: it contains source hashes, exact
prompt identity, extracted audio, the raw request, response, usage, model version,
and request audit. Reusing a cache requires matching source, offsets, prompt,
model choice, and generation settings. Successful malformed or partial responses
are retained and never automatically repeated. An unknown inflight outcome
requires inspection and is never replayed. Known retryable HTTP rejections use
the existing bounded request-pool cooldown and retry policy.

The CLI is serial with one slot. Separate processes do not share its semaphore
or cooldown. A coordinator must quiesce other paid work safely or pass an existing
shared `RequestPool` to the in-process `opinion` function. This helper does not
stop, kill, or resume unrelated processes. A filesystem lock prevents duplicate
requests for the same cache across concurrent processes.
