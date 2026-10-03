# Reviewed priority-wave queue

`skills/subtitle-creation/scripts/batch.py` processes explicit media serially
with one shared transcription request pool. It sorts jobs by wave, group, then
manifest order within each group. Every job in a wave must have a structurally
valid native subtitle, any required English subtitle, and an accepted review
before the next wave starts. It preserves existing SRT files by storing and
checking their hashes.

Use a private manifest and state directory outside this repository. They contain
media paths, transcript cache locations and reviewer information. The manifest
is a JSON object with `version: 1` and a nonempty `jobs` array. Each job has:

- `id`: a unique safe token.
- `wave`: a positive integer; lower waves run first.
- `group`: a safe token identifying a series or other review group.
- `source`: an explicit absolute path to an existing media file.
- `accepted_existing`: an optional boolean, default false. Set true only when
  the native subtitle was already reviewed before starting the queue.

IDs and groups use letters, digits, underscores, dots and hyphens, beginning
with a letter or digit. Duplicate source paths, IDs and media sharing a subtitle
stem are rejected. No media discovery or directory-wide transcription occurs.

Pass `--manifest`, `--state-dir`, and optionally `--config`, `--project`,
`--model` or `--location`. `--request-workers` defaults to one across the whole
process; it does not multiply by the number of episodes or groups. The queue
keeps the helper's automatic source language and never forces an output label.
Existing confirmed valid native subtitles skip transcription, authentication,
and model discovery. Existing files and metadata are not rewritten by that skip.

The default English requirement applies when the native language's base tag is
not `en`. `--no-english` is an explicit generic opt-out. For a foreign-language
`accepted_existing` original, the native file remains protected but its English
translation still needs review.

## Review handoff

The queue writes `review-requests/<wave>-<group>.json` as soon as that group's
jobs finish transcription, allowing review while a later group in the same
wave transcribes. Requests contain the canonical `manifest_sha256`, wave,
group, job IDs, source hashes, cache/result information, current native output,
English destination, and `decision_required` for each job.

When transcription returns `needs_language`, inspect representative cached
`words.json` or `cues.json` to identify the audio's actual language. Publish the
native SRT with the existing transcription helper's `--format-only` and
`--output-language` options, retaining the original model/language settings.
Then translate into a separate English SRT when required. The queue waits for
these agent actions; it never guesses language, formats unknown language as
English, performs translation, or runs another audio call to obtain a label.

Review representative opening, middle and ending cues, flagged timing/wording,
and required translations. Write an atomic JSON decision to
`review-decisions/<wave>-<group>.json`. Use this contract:

```json
{
  "version": 1,
  "manifest_sha256": "COPY_FROM_REQUEST",
  "wave": 1,
  "group": "series",
  "jobs": [
    {
      "id": "episode",
      "source_sha256": "COPY_FROM_REQUEST",
      "outcome": "accepted",
      "original_language": "en",
      "native": {"path": "ABSOLUTE_FINAL_SRT_PATH", "sha256": "ACTUAL_FINAL_FILE_HASH"},
      "english": null,
      "flags": [],
      "provenance": {
        "reviewer": "REVIEWER_LABEL",
        "method": "REPRESENTATIVE_TEXT_AND_FLAG_REVIEW",
        "samples": ["opening", "middle", "ending"],
        "reviewed_at": "REVIEW_TIME"
      }
    }
  ]
}
```

Include exactly the jobs with `decision_required: true`. For a foreign native
language, `english` is another `{path, sha256}` object when English is required;
otherwise it is null. Hash the final bytes after any supported corrections.
Native paths must match the media's confirmed `.subtitles.json5` metadata and
`<media-stem>.<language-tag>.srt`; English uses `<media-stem>.en.srt`. Both must
pass the transcription helper's strict SRT validation and ffprobe. English cue
IDs and timestamp text must exactly match the native file.

A failed entry uses `outcome: "failed"`, a nonempty `reason`, source hash,
`flags` and reviewer/method/reviewed_at provenance; native/English fields and
sample coverage are optional for failure. A failed review halts the queue
before further paid work. The shared pool checks ready current-wave decisions
after cooldown/authentication and before each new chunk gets an inflight marker
or attempt audit. Requests already sent can finish and retain their responses.
Record unresolved concerns explicitly in `flags`;
structural checks and representative review do not establish perfect accuracy.

## Resume and monitoring

The state directory contains `queue-state.json`, `events.jsonl`, a coordinator
lock, review request/decision directories, and terminal `completed.json` or
`failed.json` markers. State and request writes are atomic; events append as
single writes under the lock and are flushed to disk. The CLI creates the
state directory with mode 0700 and new private artifacts with mode 0600.

Restart the same manifest and state directory. Completed jobs and valid exact
hash decisions are reused. Cached transcription and pinned model selection
remain in the helper's normal per-media `.subtitle-cache`. A language-labeling
or format-only update to `result.json` is expected and does not change source
identity. An interrupted processing job resumes through the helper's cache,
which rejects an uncertain request rather than repeating it.

The queue polls for decisions every five seconds (adjustable up to 60). `--once`
exits with 2 when review is pending instead of waiting; 0 means completion and
1 means failure. It never advances a wave because time elapsed. Concurrent
coordinators are rejected by a nonblocking exclusive lock.

Changed sources, modified existing or accepted subtitles, changed manifest or
model/location/English settings, invalid decisions, prior failed transcription,
and unresolved inflight requests stop the queue. Subtitle validation caches use
actual file hashes, so unchanged accepted files do not repeatedly spawn ffprobe.
Failure markers are sticky;
inspect and resolve the private evidence before manually clearing failed state
or starting a deliberately revised queue. There is no automatic episode retry
loop beyond the transcription helper's bounded request retries.

An optional `--notify-command-file` points to a private JSON array of command
arguments. The helper invokes that argv directly with `shell=False`, sends the
milestone event JSON to stdin, suppresses command output, and times out after
30 seconds. It invokes the command after persisting each new review request,
failure marker or completion marker. Existing requests and completed queues do
not replay notifications on resume. A failed notification records a sanitized
`notify_failed` event without invalidating cached transcription. Use an external
recovery heartbeat when notifications can be missed; the queue itself never
launches agent workers.

## Offline setup and review helper

`skills/subtitle-creation/scripts/manage.py` consolidates inventory and review
bookkeeping. Its commands never authenticate or call transcription APIs:

```sh
python3 skills/subtitle-creation/scripts/manage.py plan \
  --root "$MEDIA_DIR" --state-dir "$PRIVATE_STATE" --wave-size 4
python3 skills/subtitle-creation/scripts/manage.py packet \
  --request "$REVIEW_REQUEST" --work-dir "$EMPTY_PRIVATE_WORK"
python3 skills/subtitle-creation/scripts/manage.py label \
  --request "$REVIEW_REQUEST" --job "$JOB_ID" --language "$ACTUAL_BCP47"
python3 skills/subtitle-creation/scripts/manage.py accept \
  --request "$REVIEW_REQUEST" --notes "$REVIEWER_NOTES"
```

`plan` inventories only the explicitly authorized root. It discovers common
audio/video extensions, skips hidden/cache directories and directory symlinks,
and records outside-root file symlinks and no-audio files as skipped. ffprobe
provides duration/audio checks. Series groups follow source parent folders;
filenames accept bare episode numbers, episode/lecture/lesson prefixes, and a
leading numeric course ID of at least four digits. `--episode-pattern` overrides
this using a named `episode` integer capture. Ambiguous/unparsed names and
duplicate episode/output ownership halt with private inventory evidence.
Numbered wave slots use episode indices (1 through wave-size first); episode
zero extras follow all numbered waves. No paid scope is created by inventory.

The private state contains `inventory.json` and `manifest.json` using the
existing queue schema above. Start batch.py with that manifest and state.
`accepted_existing` defaults false; change it explicitly only for known prior
review before starting the queue. Existing metadata and subtitles are inventoried
and preserved. An existing plan/state is reusable only with matching content
and queue identity. Changed plans require a deliberately new private state.

The review commands expect the planner's `manifest.json` beside queue-state and
review requests, or accept `--manifest` for an existing externally located
explicit manifest. They check canonical queue/request identity, source hashes and
protected outputs. `packet` requires an empty private work directory, recovers
original cache flags behind an existing-native skip summary, and includes bounded
representative samples plus complete flags/corrections and current output hashes.
Multiple unselected transcript caches require resolution rather than guessing.
`label` requires cached transcription and a reviewer-selected language; it
protects existing original/translation files. `accept` requires explicit
reviewer-authored notes, validates current final files/timelines and publishes
only a review decision. Assigned-job checks permit unrelated episodes to remain
in flight during review. Existing different artifacts are never overwritten;
identical decisions may be reused. Private destination checks resolve symlinks
and reject paths within this public skill repository.

Read [assigned review guidance](../skills/subtitle-creation/references/batch-review.md)
for native language evidence, translation, bounded audio rechecks, reviewer note
coverage and output preservation. Packet generation does not perform review.
