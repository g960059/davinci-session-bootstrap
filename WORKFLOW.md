# Piano Multicam Resolve Workflow

## 0. Purpose

This workflow turns Sony α6400 PP10 HLG multi-camera piano recordings into a
DaVinci Resolve project ready for manual multicam editing, color matching, and
YouTube SDR Rec.709 delivery.

This repository handles the preparation work:

- Group `/incoming/` media into takes and angles.
- Create and validate the Resolve project.
- Configure HLG -> SDR Rec.709 color management.
- Import media and create waveform-synced take timelines.
- Generate an operator handoff for the manual Resolve work.

This repository does not own the final creative work:

- Multicam clip creation.
- Angle switching.
- Manual color grading.
- Final editorial decisions.
- YouTube export choices.
- AI CDL auto-application.

## 1. Create a Session Folder

Create a new session root on the SSD:

```text
<session-root>/
└── incoming/
```

Put all camera videos and external audio files into `incoming/`.

```text
<session-root>/incoming/
├── random-camera-file-001.mp4
├── random-camera-file-002.mp4
├── random-camera-file-003.mp4
├── random-camera-file-004.mp4
├── random-camera-file-005.mp4
├── random-camera-file-006.mp4
├── audio-take1.aif
├── audio-take2.aif
└── ...
```

Random file names are acceptable. The canonical folder name is `incoming/`;
do not use `incomings/`.

## 2. Run the Skill or CLI

When asking Codex or another AI agent:

```text
Follow the davinci-session-bootstrap skill for <session-root>.
Run through Stage A/B and generate the operator handoff.
Do not run CDL, manual-cdl, or auto-state-99-v1.
```

When running the CLI directly:

```bash
./scripts/pg group-session "<session-root>" --json
./scripts/pg prepare-resolve-session "<session-root>" --json
./scripts/pg inspect-resolve-session "<session-root>" --json
./scripts/pg operator-handoff "<session-root>" --json
```

`prepare-resolve-session` also writes the operator handoff automatically after a
successful bootstrap.

## 3. Stage A: Auto Grouping

`group-session` moves and normalizes `incoming/` media into take folders.

Expected output:

```text
<session-root>/
├── incoming/
├── session.yaml
└── takes/
    ├── take-01/
    │   ├── angle-a.mp4
    │   ├── angle-b.mp4
    │   ├── angle-c.mp4
    │   ├── audio.aif
    │   └── take.yaml
    └── take-02/
        ├── angle-a.mp4
        ├── angle-b.mp4
        ├── angle-c.mp4
        ├── audio.aif
        └── take.yaml
```

Check:

- Take count matches the session.
- Each take has the expected angles.
- Each take has external audio.
- `session.yaml` exists.
- No obvious take or angle is missing.

## 4. Stage B: Resolve Bootstrap

`prepare-resolve-session` prepares Resolve for manual work.

It performs:

- Resolve project creation/opening.
- Project library checks.
- Media import.
- Take timeline creation.
- Waveform sync.
- `audio-edit.wav` proxy creation when needed.
- Cache, gallery stills, and backup path setup.
- `.drp` project snapshot export.
- HLG -> SDR Rec.709 color management setup.

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

`inspect-resolve-session` must return `PASS` or `WARN`. Do not proceed to
Resolve editing on `FAIL`.

Notes:

- `timelinePlaybackFrameRate=24` is a warning.
- Fix the timeline playback frame rate manually to 29.97 before editorial
  assembly or export.
- If Resolve cache/stills storage warnings appear, restart Resolve and rerun
  `prepare-resolve-session`.

## 5. Read the Operator Handoff

Generated files:

```text
<session-root>/reports/operator-handoff.md
<session-root>/reports/operator-handoff.json
```

The handoff includes:

- Session information.
- Resolve project name.
- Take list.
- Angle and audio files per take.
- Expected color management.
- Manual Resolve next steps.
- Repo responsibility boundaries.

Use this file as the checklist before entering manual Resolve work.

## 6. Optional Review Aids

These commands create visual review material only. They do not apply grades.

```bash
./scripts/pg render-stills "<session-root>" --json
./scripts/pg review-manifest "<session-root>" --json
./scripts/pg contact-sheet "<session-root>" --json
```

Generated files:

```text
<session-root>/reports/stills/
<session-root>/reports/color-review-manifest.json
<session-root>/reports/color-review-sheet.png
```

Use these to check angle coverage, broad exposure/color issues, and missing
camera coverage before or during manual grading.

## 7. Create Multicam Clips in Resolve

Work one take at a time.

In the Media Pool, select the take's angle videos and external audio:

```text
angle-a.mp4
angle-b.mp4
angle-c.mp4
audio.aif / audio-edit.wav
```

Right-click and choose:

```text
Create New Multicam Clip Using Selected Clips
```

Use:

```text
Sync: Sound
Angle Name: Clip Name or Metadata
```

After creation, right-click the multicam clip:

```text
Open in Timeline
```

Confirm:

- Every angle is synchronized.
- External audio is synchronized.
- Camera scratch audio is muted, disabled, or deleted after sync.
- Final audio comes only from the external audio file.

## 8. Grade Inside Each Multicam Timeline

With the multicam clip open in timeline, go to the Color page.

Standard policy:

```text
Use Local Grades
```

Goal:

- Match angles within the same take.
- Avoid forcing all takes to look identical when sunlight naturally changes.

Check:

- White keys are not overly yellow, blue, or gray.
- The black piano is not crushed.
- The gold plate is believable and not oversaturated.
- The wood floor is not excessively red or yellow.
- Skin looks natural when visible.
- Window highlights do not dominate the image.
- Scopes do not show destructive clipping.

Suggested adjustment order:

1. Exposure.
2. White balance / Temp.
3. Contrast.
4. Saturation.
5. Highlights / shadows.
6. Look refinements if needed.

## 9. Reuse Looks With Gallery Stills

After one take is matched, save stills for each angle.

In the Color page viewer:

```text
Grab Still
```

Name stills clearly:

```text
take01_angle_a_base
take01_angle_b_base
take01_angle_c_base
```

For the next take:

1. Open that take's multicam clip in timeline.
2. Go to the Color page.
3. Select the matching angle.
4. Right-click the Gallery Still.
5. Apply Grade.
6. Adjust exposure and white balance for the new take.

Treat stills as starting points, not finished copies.

## 10. Build the Piece Timeline

After each take's multicam clip is color-matched internally, assemble the piece
timeline.

Example:

```text
Piece Timeline
├── take-01 multicam  # First movement
├── take-02 multicam  # Second movement
└── take-02 multicam  # Third movement
```

Do here:

- Angle switching.
- Cut editing.
- Movement spacing.
- Removing unused material.
- Audio confirmation.

The multicam-internal angle matching should already be done before this stage.

## 11. Final Timeline Finishing

Apply only light Local Grades on the final piece timeline.

Use final timeline grades for:

- Smoothing take-to-take transitions.
- Minor global brightness matching.
- Final tone polish.

Do not fix angle-specific problems here. If one angle is wrong, open that
multicam clip in timeline and fix the source angle there.

## 12. Audio Cleanup

The final timeline should use only the external audio path.

Confirm:

- Camera audio is not mixed into the final output.
- Multiple scratch tracks are not causing phase issues.
- Take boundaries are not audibly abrupt.
- If needed, replace with a Logic/DAW-processed final audio file.

## 13. Pre-Export Checklist

Check before export:

```text
Timeline frame rate: 29.97
Output: Rec.709 / Gamma 2.4
Audio: external audio only
Multicam angle switching: correct
Take boundaries: visually and audibly natural
White keys: natural
Black piano: not crushed
Window highlights: acceptable
Skin tone: natural when visible
```

If the project still warns about `timelinePlaybackFrameRate=24`, fix it
manually before export.

## 14. YouTube SDR Export

In the Deliver page, export as SDR.

Suggested baseline:

```text
Format: MP4
Codec: H.264 or H.265
Resolution: project/source resolution
Frame rate: 29.97
Color space tag: Rec.709
Gamma tag: Rec.709 / Gamma 2.4
Audio: AAC or PCM stereo
```

This workflow is for YouTube SDR. HDR/HLG delivery should be treated as a
separate workflow.

## 15. CLI Summary

Standard path:

```bash
./scripts/pg group-session "<session-root>" --json
./scripts/pg prepare-resolve-session "<session-root>" --json
./scripts/pg inspect-resolve-session "<session-root>" --json
./scripts/pg operator-handoff "<session-root>" --json
```

Optional review:

```bash
./scripts/pg render-stills "<session-root>" --json
./scripts/pg review-manifest "<session-root>" --json
./scripts/pg contact-sheet "<session-root>" --json
```

Experimental only:

```bash
./scripts/pg preview-cdl ...
./scripts/pg manual-cdl ...
```

Do not use CDL commands for the standard workflow.

## 16. Shortest Practical Route

```text
1. Put media into <session-root>/incoming.
2. Run the skill/CLI through Stage A/B.
3. Read reports/operator-handoff.md.
4. In Resolve, create one multicam clip per take.
5. Open each multicam clip in timeline.
6. Match angles with Local Grades.
7. Use Gallery Stills as starting points for the next take.
8. Assemble multicam clips into the piece timeline.
9. Switch angles and cut.
10. Lightly finish on the final timeline.
11. Confirm 29.97, Rec.709, and external-only audio.
12. Export SDR for YouTube.
```
