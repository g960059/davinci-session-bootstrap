---
name: davinci-session-bootstrap
description: |
  Bootstrap a new piano session end-to-end: auto-group incoming MP4s + audio bounce into
  takes, then create a DaVinci Resolve project with waveform-synced clips. Stops at the
  bootstrap boundary; hand-off to /color-review or `pg auto-color-normalize` for color
  stages. Wraps the bundled piano-guard CLI.
disable-model-invocation: true
---

# davinci-session-bootstrap

Drives the piano-guard CLI through the bootstrap stages — `group-session` and
`prepare-resolve-session` — for a new piano session. Operator drops camera
MP4s and a Logic Pro audio bounce into `<session-root>/incoming/`, invokes
`/davinci-session-bootstrap <session-root>`, and ends with a DaVinci Resolve project
that has takes assembled, audio-edit proxies generated, and clips
waveform-synced and ready for color review.

The skill stops at bootstrap. Color stages (`auto-color-normalize`, AI-driven
`/color-review`) are explicit follow-ups, not part of this skill.

## When to use

- New session: `<session-root>/incoming/` contains raw camera files and a
  Logic Pro audio bounce, but `takes/` is empty or missing.
- Re-run on an already-bootstrapped session: stage A is a no-op; stage B is
  idempotent and safe to re-execute.

## Prerequisites — verify before running stages

Halt and surface to operator on any failure. Do not auto-fix.

1. `<session-root>/incoming/` exists and contains:
   - At least one `*.mp4` camera file.
   - Exactly one audio file: `audio.wav`, `audio.aif`, `audio.aiff`, or
     `audio.flac` (Logic Pro bounce or sync-recorder). Required —
     `discover_take` fails without it.
2. `${CLAUDE_SKILL_DIR}/.venv/bin/piano-guard` exists. If missing:
   ```
   The skill .venv is not built. Run:
     ~/.claude/skills/davinci-session-bootstrap/scripts/install.sh
   ```
   Halt — do not auto-run install.sh inside a Claude turn.
3. DaVinci Resolve is running.
4. `~/Library/Preferences/Blackmagic Design/DaVinci Resolve/dblist.conf`
   already lists the project library that `<session-root>` will use. If
   piano-guard reports "Resolve project library is not registered," the
   operator must restart Resolve once.

## Stages

Always invoke the CLI via `${CLAUDE_SKILL_DIR}/scripts/pg` so it uses this
skill's vendored .venv regardless of the operator's working directory.

### Stage A — Auto-group

```
${CLAUDE_SKILL_DIR}/scripts/pg group-session "<session-root>" --json [--reference-angle <angle>]
```

- The `--json` payload is on stdout. **No JSON report file is written by
  this stage** — parse stdout directly.
- Verify `status == "PASS"` and `takes_created` count matches operator's
  expectation (ask if uncertain).
- **Reference angle policy**: read `<session-root>/session.yaml` first
  (key `reference_angle`). If unset, ask the operator which angle is the
  WB anchor *before* running stage A, then pass it as `--reference-angle`.
  This persists the choice via `_prepare_session_config`. On subsequent
  runs the value is read from yaml and the operator is not asked again.
- **Idempotence**: if `incoming/` is empty, this stage exits 0 with a
  "session already grouped" payload — that is normal on re-runs.

### Stage B — Resolve bootstrap

```
${CLAUDE_SKILL_DIR}/scripts/pg prepare-resolve-session "<session-root>" --json --allow-uncalibrated
```

- Always pass `--allow-uncalibrated`. Calibration is being phased out for
  the upcoming grey-card workflow; the calibration gate should not block
  bootstrap.
- Read the report at `<session-root>/reports/prepare-resolve-session.json`.
- Verify top-level `status` is PASS or WARN. FAIL halts.
- Stage B internally:
  - Generates `<session-root>/takes/take-XX/audio-edit.wav` (48 kHz / 24-bit
    PCM proxy via ffmpeg + libsoxr) unless source audio is already in that
    format.
  - Calls `MediaPool.AutoSyncAudio` waveform-based — no timecode required.
  - Applies the project color management to YRGB Auto SDR Rec.709.

## What Claude resolves automatically

- **Project library / project name**: defaults derived from
  `<session-root>` parent directory name (logic in piano-guard's
  `_default_project_library`). The operator does not need to specify
  `--project-library-name` etc. unless they want non-default names.
- **`--allow-uncalibrated`**: always set for stage B.

## Hand-off

On bootstrap success, summarize and direct the operator:

```
Session bootstrap complete.
  Takes: <N>
  Reference angle: <angle>
  Resolve project: <project_name>

Next steps (operator chooses):
  - For automatic CDL across angles:  pg auto-color-normalize "<session-root>"
  - For AI-driven per-clip CDL:       /color-review on <session-root>
  - To skip color and go to editing:  open Resolve and proceed.
```

On any FAIL, surface the failing report path and the relevant
`code` / `message` fields from the JSON. Do not retry without operator
input.

## Pitfalls

- **Resolve must be restarted after dblist.conf updates**. piano-guard
  writes a new entry on first invocation if the library is missing, but
  the running Resolve process only re-reads dblist.conf at startup.
- **Project color management must be YRGB Auto SDR Rec.709** before any
  color stage runs. Stage B applies this; verify by reading the report's
  `project_settings` block. If the operator changed it manually after
  bootstrap, color stages will produce wrong CDLs.
- **`incoming/` must contain only the source files** — camera-side
  metadata (`*.thm`, `*.LRV`) belongs in `<session-root>/excluded/`.
  group-session will refuse a plan that mixes them.
- **Audio file naming**: prefer `audio.wav`. Other names work but are
  matched by a fallback heuristic; a clear name avoids ambiguity.

## Escape

| Failure | Action |
|---|---|
| `.venv/bin/piano-guard` missing | Operator runs `scripts/install.sh` once. |
| Resolve not running | Operator launches Resolve; re-run the failed stage. |
| `library_not_registered` | Operator restarts Resolve; re-run stage B. |
| Stage A `status: FAIL` with `code: missing_audio` | Operator places `audio.wav` in `incoming/`; re-run stage A. |
| Stage B `status: FAIL` with `code: project_settings_mismatch` | Surface mismatch list; ask operator to confirm before retrying with `--fresh`. |
| Stage B WARN with `setcdl_unverified` | Acceptable — identity color path. Continue. |

## Updating the bundled CLI

When the upstream piano-guard is updated:

```bash
~/.claude/skills/davinci-session-bootstrap/scripts/sync-from-source.sh
# Re-run install.sh only if pyproject.toml or uv.lock changed:
~/.claude/skills/davinci-session-bootstrap/scripts/install.sh
```

The editable install picks up Python source edits without re-running
install.sh.
