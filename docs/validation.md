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

Long-word onset repair uses measured silence endpoints, never a guessed time
shift. A conservative raw-order exception handles starts that regress behind
the same speaker's previous word within one section. It requires supported
previous/following bounds and proves every apparently intervening interval is
an earlier word from that same speaker and section, ending before the proposed
onset. Later anomalies are evaluated first; unrepaired following speech,
conflicting speakers/sections, missing sequence evidence, or absent measured
pauses block correction. Every inferred onset retains its original start,
measured-pause and sequence evidence, and a review flag. Word text, IDs,
speaker labels, endpoints and transcription/cache identity stay unchanged.
Apply this offline through existing cached responses and preserve reviewed SRTs;
representative audio review remains necessary for inferred timing.

Authentication renewal explicitly asks gcloud for a fresh token before caching it for at most 45 minutes. A token returned from the existing gcloud cache may have less remaining lifetime than a newly minted token, so acquisition time alone is insufficient. Credentials and auth command output remain in memory; errors are sanitized. A terminal HTTP rejection or unknown request outcome stops queued sections in that request pool before inflight markers or attempt audits are created. Already submitted requests may finish. HTTP401 never automatically replays audio; a coordinator must inspect known rejection evidence and deliberately resume missing sections. Synthetic tests cover fresh-token acquisition, sanitized auth failure and queued-section cancellation.

## Explicit reviewed timing overlays

Keep raw response JSON byte-for-byte intact when a bounded audio recheck
supports correcting an invalid word interval. The runner never creates an
overlay automatically. A private `chunk-NNN.timing-overrides.json` beside the
raw `chunk-NNN.json` contains exactly `version: 1`, `source_sha256` (SHA256 of
the raw checkpoint bytes), and a nonempty `corrections` array. Each correction
has exactly these fields:

- `part_index`, `word_index`: nonnegative integer indices into the first
  candidate's parts and that part's audio-transcription words. Booleans and
  duplicate targets are rejected.
- `word`, `old_start_offset`, `old_end_offset`: exact original word and offset
  values, including offset value types and original string spelling.
- `start_seconds`, `end_seconds`: finite numeric section-local endpoints,
  ordered without reversal and inside the section. Equal endpoints are allowed
  only when the indexed raw recheck word also has equal endpoints.
- `reviewer`, `reason`: nonempty text identifying the review and its purpose.
- `evidence`: the exact object described below.

Evidence has exactly `recheck_path`, `recheck_sha256`, `clip_start_seconds`,
`clip_end_seconds`, `recheck_part_index`, and `recheck_word_index`. The path is
an absolute existing `.json` raw audio-recheck response; its byte checksum must
match. Clip boundaries use section-local seconds, are positive in duration,
inside the original section, and at most 60 seconds apart. The complete recheck
must finish successfully and all its word timings must fit the exact clip
duration. The indexed anchor must have the same word text, allowing only
different trailing sentence punctuation (`.,;:!?`). Spelling, case, internal
punctuation, numeric signs and percent symbols must still match. Original
word text and both raw responses remain unchanged. Proposed endpoints
must equal clip start plus its actual raw offsets, with only one microsecond of
floating-point tolerance. Manual claims without this retained word anchor are
not accepted.

Evidence may additionally contain `recheck_end_word_index` for an explicitly
reviewed hyphenated ASCII telephone token, for example `1-555-234-ABCD.` or
`555-234-6789`. This selects an inclusive span of two to six consecutive words
in the indexed part, with nonnegative integer indices (never booleans), a single
speaker, positive ordered intervals, and no overlap. Gaps are allowed. The source
must match `(?:\+?[0-9]{1,3}-)?[0-9]{3}-[0-9]{3}-(?:[0-9]{4}|[A-Za-z]{4})`
after stripping trailing sentence punctuation. Span tokens allow only ASCII
alphanumeric groups separated by single hyphens, an optional trailing delimiter
hyphen, and an optional leading `+` on the first token; leading, repeated and
standalone hyphens are rejected;
sentence punctuation is allowed only at the end of the final token. Removing
hyphens must preserve every character in order, including `+`, with ASCII case
insensitivity only for the mnemonic suffix. Proposed endpoints use the first
word's start and final word's end plus the clip offset, with the existing
one-microsecond tolerance. The full indexed anchor span is retained in private
applied provenance. This is an explicit overlay, never an automatic text or
timing correction. Omitting the optional end index keeps the strict single-word
schema and matching behavior, including its case preservation.

A recheck may represent a brief word as a zero-duration point. Preserve that
exact point rather than inventing an endpoint. The original interval warning
remains in review flags, and final subtitle cues must still have positive,
ordered, nonoverlapping durations and preserve every word. A zero-duration
overlay unsupported by the indexed raw anchor is rejected.

The centralized `read_checkpoint` validates the overlay even if the raw words
already pass timing checks. It changes only offsets in an in-memory deep copy;
text, speaker fields, other response fields and raw bytes remain unchanged.
Stale hashes, missing evidence, unknown fields, malformed indices/endpoints,
and an orphan overlay without its raw checkpoint stop processing. Verified
manual intervals are protected from subsequent inferred onset repair. Final
`corrections` and `review_flags` retain the original offsets, reviewer, reason,
raw checkpoint hashes, checkpoint/overlay paths, anchor evidence, section and word ID.
These records are private and must stay outside public repositories.

Pool cache hits, newly returned responses and episode checkpoint reads use the
same validation. A successfully returned response is saved and its inflight
marker removed before timing validation. Invalid timing then stops queued
requests, retains the successful response/audit, and produces no uncertain
marker or automatic replay. Preserve approved SRTs and use explicit cached
formatting for unapproved outputs after a supported overlay is reviewed.
Transcription settings and cache identity are unchanged. Synthetic offline
tests cover anchor verification, exact identity, malformed evidence, byte
preservation, private provenance, format-only reuse and queued cancellation.

## Explicit reviewed terminal artifact exclusion

A private adjacent `chunk-NNN.word-exclusions.json` may exclude exactly one
terminal malformed speaker token matching `uid:[0-9]+`, with a valid start and
missing `endOffset`. Spoken words, complete tokens and nonterminal entries are
rejected. Never generate an overlay automatically or guess from token appearance.
Preserve both raw responses. Stop when bounded successful audio evidence is uncertain.

The closed schema is `version: 1`, `source_sha256`, and `exclusions` containing
exactly one object: `part_index`, `word_index` (nonnegative integer indices),
`raw_word` (the complete exact raw dictionary, including missing fields),
nonempty `reviewer` and `reason`, and `evidence`. Evidence contains exactly
`recheck_path` (absolute raw JSON path), `recheck_sha256`, `clip_start_seconds`
and `clip_end_seconds`. The clip is at most 60 seconds, inside the section,
ends at its end, and covers the token and preceding three word starts. The raw
recheck must finish with STOP, have all timestamps strictly inside the clip,
contain no speaker token, and end with at least three valid words matching those
preceding original words (only trailing sentence punctuation may differ).
Unknown keys, stale checksums and concurrent timing overlays stop processing.

Only a copied word-list entry is removed. Raw bytes, text, remaining words,
speakers, timestamps, usage and cache identity stay intact. Private corrections
and flags retain exact target identity, evidence, paths, checksum, section and
global start; a removed entry has no active word ID. Use `--format-only` for
reviewed cached formatting, preserving approved outputs and avoiding paid replay.

## Explicit reviewed numeric joins

When a decimal and a separate `%` have touching raw boundaries but the percent
interval is reversed, halt paid work and retain a bounded successful audio
recheck. Never automatically join tokens. A private adjacent
`chunk-NNN.word-joins.json` has exactly `version: 1`, `source_sha256` and `joins`
containing exactly one entry with `part_index`, `word_index`, `raw_words`,
`start_seconds`, `end_seconds`, `reviewer`, `reason` and `evidence`.

Indices identify the first of two adjacent entries in one part and must be
nonnegative integers, not booleans. `raw_words` preserves both complete raw
dictionaries and types. The first word matches ASCII `[0-9]+(?:[.,][0-9]+)?`
and has a valid interval. The second is exactly `%`, has a usable reversed
interval, and starts at the first end within one microsecond. Resolved speaker
labels must match. Negative numbers, other units, lexical edits, properly timed
percent words, gaps, cross-part and cross-speaker pairs are rejected.

Evidence has the same exact six fields as a timing overlay: absolute raw
`recheck_path`, `recheck_sha256`, `clip_start_seconds`, `clip_end_seconds`,
`recheck_part_index`, `recheck_word_index`. The successful STOP recheck fits the
strict clip timeline, at most 60 seconds within the section and covering both
original starts. Its indexed word matches the original decimal concatenated
with `%`, allowing only different trailing sentence punctuation. Both proposed
positive ordered endpoints equal clip start plus the actual anchor offsets
within one microsecond. Retained audio evidence and independent review are
required; stop uncertain requests rather than guess a percent-token duration.

The runner only changes the copied first entry's word and two offsets, then
removes the second entry. Other fields and both raw responses stay intact.
Private `reviewed_word_join` corrections and flags retain both identities,
evidence, paths, checksums and merged section/global timing. The merged word's
active ID protects its verified onset from inferred repair. Other overlays on
the same checkpoint and orphan join overlays stop processing before requests.
Format-only reuse, full raw usage/accounting and cache identity remain unchanged.

Audio second-opinion tests use synthetic inputs and mocked responses to check
clip bounds, default Flash routing, explicit Lite selection, model-family filtering
and numeric ordering, cache reuse before authentication, prompt/model identity
mismatch, uncertain-outcome preservation,
bounded known rejection retries, and rejection of partial or thought-only text.
The shared request loop retains the existing transcription overlay validation.

Synthetic repository and symlink tests verify that audio opinion evidence cannot
be cached inside public skill sources, before any media probe or cache write.
