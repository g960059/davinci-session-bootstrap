# davinci-session-bootstrap

DaVinci Resolve session bootstrap for multi-camera piano recordings. This
repository is self-contained: the bundled `piano-guard` CLI in
`src/piano_guard/` is the canonical implementation for this skill.

The standard workflow now stops at a manual Resolve handoff. Color and
editorial work happen in Resolve with multicam timelines, Local Grades, and
Gallery Stills.

For the full operational workflow, see [WORKFLOW.md](WORKFLOW.md).

## Scope

In scope:

- Auto-grouping `incoming/` media into takes.
- Resolve project bootstrap and waveform sync.
- Audio edit proxy generation when needed.
- Resolve cache, gallery stills, and project backup storage preflight.
- Sony α6400 PP10 HLG -> YouTube SDR Rec.709 project color management.
- Project inspection and `reports/operator-handoff.md`.
- Optional still/contact-sheet generation for manual review.

Out of scope:

- Automatic color matching.
- Standard use of `auto-state-99-v1` / Remote Versions.
- AI CDL autopilot as a production path.
- LUT delivery.
- Logic Pro automation.
- Final multicam editing, manual color grading, and export decisions.

## First-Time Setup

Build the local virtualenv once:

```bash
./scripts/install.sh
```

Requirements:

- `uv` (`brew install uv`)
- Python 3.11
- DaVinci Resolve scripting enabled for Resolve-backed commands
- `ffmpeg` / `ffprobe` available on `PATH`

The `.venv/` directory is a disposable machine-local cache. Re-run
`scripts/install.sh` after Python, macOS, Homebrew, or dependency changes.

## Session Layout

Before bootstrap:

```text
<session-root>/
└── incoming/
    ├── <camera files>.mp4
    └── <audio files>.aif
```

After bootstrap:

```text
<session-root>/
├── incoming/
├── session.yaml
├── takes/
│   └── take-XX/
│       ├── angle-X.mp4
│       ├── audio.aif
│       ├── audio-edit.wav
│       └── take.yaml
├── reports/
│   ├── prepare-resolve-session.json
│   ├── inspect-resolve-session.json
│   ├── operator-handoff.md
│   ├── operator-handoff.json
│   ├── render-stills.json
│   ├── color-review-manifest.json
│   ├── color-review-sheet.png
│   └── stills/<take>/<angle>.png
└── resolve/
    └── <project>.drp
```

## Standard Pipeline

Run directly from this checkout:

```bash
./scripts/pg group-session "<session-root>" --json
./scripts/pg prepare-resolve-session "<session-root>" --json
./scripts/pg inspect-resolve-session "<session-root>" --json
./scripts/pg operator-handoff "<session-root>" --json
```

`prepare-resolve-session` writes `operator-handoff.md` automatically when it
completes successfully. Running `operator-handoff` separately is useful after
manual changes to `session.yaml` or take metadata.

Expected Resolve color management:

```text
Color science: DaVinci YRGB Color Managed
Automatic color management: Off
Input color space: Rec.2100 HLG
Timeline color space: Rec.709 Gamma 2.4
Output color space: Rec.709 Gamma 2.4
Input DRT: DaVinci
Output DRT: DaVinci
```

`timelinePlaybackFrameRate=24` on a 29.97 project is reported as a warning
because Resolve scripting cannot reliably change it; fix it manually before
editorial assembly or export.

## Optional Review Aids

These commands create review materials only. They do not write grades into
Resolve.

```bash
./scripts/pg render-stills "<session-root>" --json
./scripts/pg review-manifest "<session-root>" --json
./scripts/pg contact-sheet "<session-root>" --json
```

Use the contact sheet to check angle coverage and broad color/exposure issues
before manual grading.

## Manual Resolve Workflow

After bootstrap:

1. In Resolve, create one multicam clip per take from all angle videos plus the
   final external audio.
2. Use `Sound` sync.
3. Right-click the multicam clip and choose `Open in Timeline`.
4. Confirm sync, then disable or delete camera scratch audio. Keep only the
   external AIF/WAV audio for final use.
5. On the Color page, use Local Grades inside the multicam timeline.
6. Match angles within the same take first.
7. Save angle grades as Gallery Stills and use them as starting points for the
   next take.
8. Assemble the graded multicam clips into the piece timeline.
9. Perform angle switching and final edit.
10. Use final timeline grades only for light take-to-take finishing.

## Experimental CDL Tools

The CLI still contains `preview-cdl` and `manual-cdl` for older experiments and
debugging. They are hidden from normal help and are not part of the standard
workflow.

```bash
./scripts/pg preview-cdl --source-png still.png --slope 1,1,1 --offset 0,0,0 --out preview.png --json
./scripts/pg manual-cdl "<session-root>" --clip-id take-01/angle-c --slope 1,1,1 --offset 0,0,0 --dry-run --json
```

`manual-cdl` writes only CDL values on the named remote version. Identity CDL
does not change the image; the project-wide HLG -> SDR base transform must be
correct before any CDL experiment is meaningful.

## E2E Test Session

The historical E2E fixture was:

```bash
/Volumes/PortableSSD/phase2-e2e-test
```

For current validation, prefer creating a fresh session with `incoming/`, then
run the standard pipeline. Optional still/contact-sheet checks may be run after
Stage B. Ignore legacy auto-color reports and LUT artifacts if they exist in an
old fixture.
