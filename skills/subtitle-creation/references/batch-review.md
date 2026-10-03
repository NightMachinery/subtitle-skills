# Assigned batch review

Use `scripts/manage.py packet` to assemble a private review packet from a current
queue request. The packet provides bounded opening, several middle, speaker
change candidates (when words expose them), and ending samples. These are review
inputs, not evidence that code performed semantic review. Read complete flags
and corrections in the packet and inspect the final native and required English
files, not just cached drafts. Expand samples when the material warrants it.

Identify the actual native language from cached words/cues and detected language
metadata. Container tags alone are insufficient. If ambiguous or mixed, retain
the cache and ask for a labeling preference. Use `manage.py label` with the
agent-selected BCP47 tag to publish from the original cached configuration. It
uses format-only processing and forbids authentication/network. Never relabel a
confirmed original or overwrite an existing subtitle to make a check pass.

For non-English originals, produce a separate English translation unless the
queue explicitly opts out. Invoke the bundled translation skill; other targets
require a user request. An available latest Sol at low reasoning effort can
coordinate text review and translation. Honor explicitly assigned models.

Review opening, multiple middle/dialogue and ending text, full flags, inferred
timing corrections, and translation meaning. Structural checks establish
timeline integrity, not transcription accuracy. Do not guess missing words from
plausibility. For a potentially meaning-changing defect, arrange a bounded audio
recheck with surrounding context through the coordinator's shared request slot.
Keep original/new raw responses and record audio-supported corrections privately.
Apply corresponding translation corrections without disturbing valid timings.

Keep an assigned private progress/result file durable. Report unresolved flags
and failed reviews explicitly. Work only on assigned jobs and outputs; do not
edit the skill repository or Git state. Preserve metadata and prior originals
and translations. A raw-cache rerender can undo manually reviewed corrections.

After actual review, author private JSON notes with a `jobs` array containing
exactly the request's `decision_required` IDs. Each note supplies `id`, `outcome`,
`flags`, and `provenance` with nonempty `reviewer`, `method`, `reviewed_at` and
actual `samples`. Accepted samples must include `opening`, `middle`, `ending`;
append cue IDs, time ranges, dialogue coverage and findings describing what was
really inspected. Failure notes require a nonempty `reason`; sample coverage is
optional. No absent notes or structural success implies acceptance.

Run `manage.py accept --request REQUEST --notes NOTES` after any supported
corrections. It adds current source/native/English hashes and identity, validates
with the queue's checker on an isolated state copy, and atomically publishes a
no-clobber decision. It never edits subtitle text or active queue state. The
coordinator consumes the decision. Use the schema in
[queue documentation](../../../docs/batch.md), not a new parallel contract.
