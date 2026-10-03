# Validation and maintenance

Run `python3 -B -m unittest discover -s tests` from the repository root. These offline tests use synthetic audio and API responses. They do not call paid services.

Run the skill creator validator against each skill folder after changing frontmatter or references. A realistic forward test should use fresh agent context, assigned media only, and disposable checkpoints outside this repository. Review actual outcomes before changing instructions.

The initial forward test showed that brief retries are insufficient when Vertex returns HTTP 429. Keep successful responses cached, reduce concurrency, and record sanitized rejection diagnostics. The runner now uses bounded 60/120-second waits with jitter, honors server hints up to five minutes, and pauses queued requests in the same process. Per-chunk audits record cumulative attempts and rejections; they cannot reconstruct attempts made before the audit existed. An HTTP 429 alone does not identify a numerical project quota; it can also indicate shared model capacity. Never publish live account diagnostics, media, transcripts, or billing figures here.

Language advice belongs in `skills/subtitle-translation/references/languages/<tag>.md`. Both base and regional tags load automatically. Use lowercase language tags and keep advice focused on wording, typography, and terminology.

The forward test also found false dialogue markers where two independently diarized audio sections were combined into one cue. Speaker IDs are local to each section, so the formatter keeps those sections in separate cues. This fix can be applied to cached transcripts with `--format-only`; it needs no new audio requests. If the source cues change after translation preparation, prepare new manifests and reuse translated text only for source cues whose text and exact timestamps still match.

A narrow audio recheck can resolve wording while returning unusable word timestamps. Preserve its raw response and the original timings, record supported lexical corrections outside the repository, and regenerate only affected translations. If a recheck does not confirm a suspected omission, flag the source instead of guessing. Structural validation alone cannot establish semantic accuracy.

Only native language-tagged SRTs should be stored. If the native language is unknown, keep words/cues as JSON and finish labeling before SRT publication. The pretty-printed `.subtitles.json5` sidecar records the original language; it contains no cloud configuration. Test default reruns against that metadata without authentication or duplicate audio calls.

The priority-wave queue tests cover stable wave/group ordering, review barriers,
shared request pools, exact subtitle/source identities, protected existing files,
interrupted and format-only resumes, native/English timeline validation, locks,
sticky failures, literal bracket-filename protection, and notification persistence
ordering without shell execution. A real request-pool test receives a failed
review after one chunk, preserves that response, and prevents a second request
without creating an inflight marker or attempt audit.
A cached-response integration test exercises the real transcription formatter
and language-labeling handoff without authentication or paid calls. See the
[queue contract](batch.md) for private manifests and review decisions.
