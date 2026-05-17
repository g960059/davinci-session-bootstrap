---
name: davinci-session-bootstrap
description: |
  Piano multicam Resolve bootstrap: auto-group incoming MP4s and audio into
  takes, prepare a DaVinci Resolve project for Sony α6400 PP10 HLG to YouTube
  SDR Rec.709, validate the project, and write a manual operator handoff.
disable-model-invocation: true
---

# davinci-session-bootstrap

Default behavior stops at a Resolve-ready handoff:

1. **group-session**: `<session-root>/incoming/` media -> `takes/take-XX/`.
2. **prepare-resolve-session**: Resolve project, imports, audio proxies,
   waveform-synced take timelines, storage paths, and HLG -> SDR Rec.709
   color management.
3. **inspect-resolve-session**: verify the Resolve project, take bins, clips,
   color settings, and project snapshot.
4. **operator-handoff**: write `reports/operator-handoff.md` for the human
   Resolve workflow.

Do not run AI CDL / `manual-cdl` / `auto-state-99-v1` as part of the normal
workflow. Color is handled manually in Resolve with multicam `Open in Timeline`,
Local Grades, and Gallery Stills.

## Prerequisites

Halt and surface the issue on any failure.

1. `<session-root>/incoming/` exists and contains camera videos plus one or more
   audio files (`.wav`, `.aif`, `.aiff`, or `.flac`) for grouping.
2. `${CLAUDE_SKILL_DIR}/.venv/bin/piano-guard` exists. If missing, ask the
   operator to run `${CLAUDE_SKILL_DIR}/scripts/install.sh`.
3. DaVinci Resolve is running before Resolve-backed commands.
4. The Resolve project library for the session is registered. If
   `library_not_registered` appears, the operator must restart Resolve once.

Always invoke the CLI via `${CLAUDE_SKILL_DIR}/scripts/pg`.

## Stage A — Auto-Group

```bash
${CLAUDE_SKILL_DIR}/scripts/pg group-session "<session-root>" --json
```

- If `incoming/` is empty, Stage A returns PASS for an already-grouped session.
- Verify `status == "PASS"` before continuing.
- Use only `<session-root>/incoming/` by default. Do not look for
  `<session-root>/incomings/` unless the operator explicitly passes
  `--incoming-dir`.

## Stage B — Resolve Bootstrap

```bash
${CLAUDE_SKILL_DIR}/scripts/pg prepare-resolve-session "<session-root>" --json
${CLAUDE_SKILL_DIR}/scripts/pg inspect-resolve-session "<session-root>" --json
${CLAUDE_SKILL_DIR}/scripts/pg operator-handoff "<session-root>" --json
```

- Read `<session-root>/reports/prepare-resolve-session.json`.
- Read `<session-root>/reports/inspect-resolve-session.json`.
- Read `<session-root>/reports/operator-handoff.md`.
- Continue on PASS or WARN. Halt on FAIL.
- `prepare-resolve-session` also writes `operator-handoff.md` on success.

Expected Resolve color settings:

- Color science: DaVinci YRGB Color Managed.
- Automatic color management: off.
- Input color space: `Rec.2100 HLG`.
- Timeline color space: `Rec.709 Gamma 2.4`.
- Output color space: `Rec.709 Gamma 2.4`.
- Input DRT / Output DRT: `DaVinci`.

`timelinePlaybackFrameRate=24` on a 29.97 session is a WARN, not a blocker for
bootstrap. It must be fixed manually before editorial assembly or export.

If `resolve_storage` reports `restart_required`, rerun
`prepare-resolve-session` after Resolve relaunches; the warning should clear
once `CacheClip`, `.gallery`, and `Resolve Project Backups` live on the
session disk.

## Optional Review Aids

These commands are optional and do not apply color:

```bash
${CLAUDE_SKILL_DIR}/scripts/pg render-stills "<session-root>" --json
${CLAUDE_SKILL_DIR}/scripts/pg review-manifest "<session-root>" --json
${CLAUDE_SKILL_DIR}/scripts/pg contact-sheet "<session-root>" --json
```

Use them to inspect angle coverage and create a contact sheet before or during
manual Resolve color work.

## Manual Resolve Handoff

After Stage B, the operator works in Resolve:

1. For each take, select its angle videos plus the final external audio.
2. Create a multicam clip using `Sound` sync.
3. Right-click the multicam clip -> `Open in Timeline`.
4. Confirm sync, then disable or delete camera scratch audio and keep only the
   external AIF/WAV audio for final use.
5. On the Color page, use **Local Grades** inside the multicam timeline.
6. Match angles within the same take first: white keys, black piano finish,
   gold plate, skin when visible, and window highlights.
7. Grab Gallery Stills for each angle and apply them as starting points for
   the next take, then adjust exposure/WB for that take.
8. Assemble graded multicam clips into the piece timeline and perform angle
   switching.
9. Use final timeline grades only for light take-to-take finishing.

## Hand-Off Message

```text
Session bootstrap complete (Stages A/B).
  Takes: <N>
  Resolve project: <project_name>
  Handoff: <session-root>/reports/operator-handoff.md

Next step: open Resolve, create one multicam clip per take, open each
multicam in timeline, and color-match angles with Local Grades.
```

## Experimental CDL Tools

`preview-cdl` and `manual-cdl` remain available for debugging older sessions,
but they are not part of this skill's standard workflow. Do not run them unless
the operator explicitly asks for CDL experimentation.
