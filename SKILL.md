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
   waveform sync, `00_color_prep_all_takes`, storage paths, and HLG -> SDR
   Rec.709 color management.
   Waveform sync must retain each video clip's embedded scratch audio; do not
   replace video audio with `audio-master`.
3. **inspect-resolve-session**: verify the Resolve project, take bins, clips,
   color settings, and project snapshot.
4. **operator-handoff**: write `reports/operator-handoff.md` for the human
   Resolve workflow.

Do not run AI CDL / `manual-cdl` / `auto-state-99-v1` as part of the normal
workflow. Color is handled manually in Resolve on `00_color_prep_all_takes`
with Local Grades and Gallery Stills before manual multicam conversion.

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
- Read `<session-root>/reports/auto-group-plan.md` and verify angle labeling:
  expected angle count, per-take labels, source filenames, lane IDs, and scores.
  Labeling is part of E2E acceptance, not just a grouping implementation detail.
- Read `<session-root>/reports/take-order.md` when present. It is a
  non-destructive shooting-order estimate based on same-angle file times; use it
  for human acceptance, not as proof that take folders were renamed.
- Use only `<session-root>/incoming/` by default. Do not look for
  `<session-root>/incomings/` unless the operator explicitly passes
  `--incoming-dir`.

## Stage B — Resolve Bootstrap

```bash
${CLAUDE_SKILL_DIR}/scripts/pg prepare-resolve-session "<session-root>" --json
${CLAUDE_SKILL_DIR}/scripts/pg inspect-resolve-session "<session-root>" --json
${CLAUDE_SKILL_DIR}/scripts/pg operator-handoff "<session-root>" --json
```

- Read `<session-root>/reports/prepare-resolve-session.md` first.
- Read `<session-root>/reports/inspect-resolve-session.md` second.
- Read `<session-root>/reports/operator-handoff.md`.
- Continue on PASS or WARN. Halt on FAIL.
- `prepare-resolve-session` also writes `operator-handoff.md` on success.
- `prepare-resolve-session` must include a `post_reload_verification` stage:
  it saves, closes, reloads, and re-inspects the Resolve project before the
  run is considered complete.
- JSON reports are still written for automation, but Markdown reports are the
  human-facing source of truth.
- Existing `00_color_prep_all_takes` timelines are preserved by default to
  protect manual grades. Use `--rebuild-color-prep` only when the operator
  explicitly wants to delete and recreate that timeline.

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

1. Open `00_color_prep_all_takes`.
2. Confirm the expected project color management is still active.
3. Treat `compact-v1`, `compact-v2`, ... as packed rows; grade by the clip item
   label (`angle-a`, `angle-b`, ...) rather than by track name.
4. On the Color page, use **Local Grades** on the source angle timeline items.
5. Match angles within the same take first: white keys, black piano finish,
   gold plate, skin when visible, and window highlights.
6. Grab Gallery Stills for each angle and apply them as starting points for
   the next take, then adjust exposure/WB for that take.
7. Do not use Remote Grades, Shared Nodes, or CDL commands for the standard
   workflow.
8. Duplicate the color-prep timeline before manual multicam conversion or
   downstream editing.
9. Use final timeline grades only for light take-to-take finishing.

## Hand-Off Message

```text
Session bootstrap complete (Stages A/B).
  Takes: <N>
  Resolve project: <project_name>
  Handoff: <session-root>/reports/operator-handoff.md

Next step: open Resolve, open `00_color_prep_all_takes`, and color-match source
angle clips with Local Grades before manual multicam conversion.
```

## Experimental CDL Tools

`preview-cdl` and `manual-cdl` remain available for debugging older sessions,
but they are not part of this skill's standard workflow. Do not run them unless
the operator explicitly asks for CDL experimentation.
