# Video Rescue guide

Video Rescue is a local, opt-in CPU workflow. It reads a source video, records
observable damage or uncertainty, prepares a deterministic plan and same-range
previews, waits for explicit confirmation, then writes new verified artifacts. It
does not overwrite the source, upload media, use GPU, or download models.

## Install and check FFmpeg

Install the base distribution in Python 3.11 or 3.12:

```text
python -m pip install genvideoscope==0.5.0.dev0
videoscope doctor
```

Install `ffmpeg` and `ffprobe` separately and place both executables on `PATH`.
VideoScope does not bundle FFmpeg. Codec/filter availability differs between
FFmpeg builds; `doctor` and the Rescue plan expose unavailable capabilities.

## Choose symptoms and a strategy

Symptom hints are optional. Supported values are `unplayable`,
`timeline_discontinuity`, `missing_audio`, `audio_video_offset`, `dark`,
`video_noise`, `soft_detail`, `flicker`, `shake`, `low_loudness`, `audio_noise`,
and `audio_clipping`. A hint narrows assessment; it is not proof of a cause and
does not authorize a media change.

- `conservative` prioritizes faithful remuxing, timestamp/stream normalization,
  decodable-range salvage, bounded edge trimming, and reliably measured fixed
  A/V offset. It does not apply subjective viewing enhancement.
- `balanced` includes Conservative behavior and may offer bounded luma, denoise,
  sharpen, deflicker, stabilization, loudness, audio-denoise, or fixed-offset
  actions when measured evidence and side-effect gates allow them.

Aggressive AI restoration is not included. The base install has no model, model
download, AI interpolation, super-resolution, generative fill, or GPU dependency.

## CLI workflow

Run in an interactive terminal to inspect the plan and synchronized preview before
confirming:

```text
videoscope rescue input.mp4 --output rescue-job --strategy conservative
videoscope rescue input.mp4 --output rescue-job --strategy balanced --symptom dark --symptom video_noise
```

Use `--locked-range START:END` to preserve a source-time range, and
`--preview-seconds` (greater than zero, at most 10) to bound total preview time.
For a reviewed non-interactive plan, pass the exact displayed digest with
`--confirm-plan`. Any plan/config/source change invalidates the old digest.
`examples/rescue-config.example.json` is a settings reference; the CLI currently
accepts the corresponding flags rather than a `--config` file.

## Preview and confirmation

Private preview files stay in `rescue-review-private/previews/`. Source, faithful,
and improved previews cover the same source interval. Review playback, timing,
cropping, audio, and whether every content-changing action is acceptable. A preview
is not a final artifact and does not establish that full-file verification passed.

Confirmation selects exact action IDs, any edge-trim damage IDs, and whether an
improved output is requested. It is bound to the complete plan digest. Stale,
altered, duplicated, or missing confirmation fails closed.

## Results and JSON

A publishable result uses `rescue-output/` with only fixed, standalone files:

```text
rescue-plan.json
faithful-rescue.mp4
improved-viewing.mp4        # Balanced only when evidence supports it
damaged-segments.json
changes.json
verification-report.json
technical-report.json
report.html
```

The faithful and improved files are separately encoded and independently verified;
an improved failure does not invalidate a faithful file that passed. JSON is UTF-8,
schema-validated, deterministic apart from documented run-envelope fields, and uses
only output-root-relative forward-slash paths. `docs/rescue-schema.md` is the
versioned contract.

`partial` means a playable faithful result exists but source intervals were not
retained; inspect `damaged-segments.json` and source mappings. `needs_review` means
one or more required checks or side-effect gates were inconclusive. Neither status
should be presented as complete recovery.

## Local Web workflow

Install the explicit Web extra, then serve only on loopback unless you intentionally
accept a broader trust boundary:

```text
python -m pip install "genvideoscope[web]==0.5.0.dev0"
python -m videoscope serve
```

The dashboard uses the same Rescue pipeline as the CLI. Upload a local file, select
symptoms/strategy, compare previews, confirm the exact plan, monitor progress, and
download only terminal artifacts. Refresh recovery, cancellation, English/Simplified
Chinese, keyboard access, mobile layout, and explicit deletion are release-review
gates; they are not substitutes for playback review of your own output.

## Privacy, retention, and limits

All work is local by default, but the input, private previews, hashes, damage map,
reports, and output videos may be sensitive. Do not share
`rescue-review-private/`. After review, use the Web delete action or remove the whole
CLI job directory. Backups, snapshots, recycle bins, and synchronization software may
retain copies outside VideoScope's control.

Video filtering cannot recreate information lost before or during recording. It may
reduce observable noise or rebalance measurable signals, but missing frames, clipped
audio, crushed shadows, blown highlights, and destroyed detail remain unavailable.
Always retain the original and compare both outputs in the actual target player.
