# Validation and maintenance

Run `python3 -B -m unittest discover -s tests` from the repository root. These offline tests use synthetic audio and API responses. They do not call paid services.

Run the skill creator validator against each skill folder after changing frontmatter or references. A realistic forward test should use fresh agent context, assigned media only, and disposable checkpoints outside this repository. Review actual outcomes before changing instructions.

The initial forward test showed that brief retries are insufficient when Vertex returns HTTP 429. Keep successful responses cached, reduce concurrency, and record sanitized rejection diagnostics. The runner now uses bounded 60/120-second waits with jitter, honors server hints up to five minutes, and pauses queued requests in the same process. Per-chunk audits record cumulative attempts and rejections; they cannot reconstruct attempts made before the audit existed. An HTTP 429 alone does not identify a numerical project quota; it can also indicate shared model capacity. Never publish live account diagnostics, media, transcripts, or billing figures here.

Language advice belongs in `skills/subtitle-translation/references/languages/<tag>.md`. Both base and regional tags load automatically. Use lowercase language tags and keep advice focused on wording, typography, and terminology.
