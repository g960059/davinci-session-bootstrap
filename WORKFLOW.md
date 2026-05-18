# Piano Multicam Resolve Workflow

## 0. Purpose

This workflow turns Sony α6400 PP10 HLG multi-camera piano recordings into a
DaVinci Resolve project ready for manual multicam editing, color matching, and
YouTube SDR Rec.709 delivery.

This repository handles the preparation work:

- Group `/incoming/` media into takes and angles.
- Create and validate the Resolve project.
- Configure HLG -> SDR Rec.709 color management.
- Import media, waveform-sync sources, and create `00_color_prep_all_takes`.
- Generate an operator handoff for the manual Resolve work.

This repository does not own the final creative work:

- Multicam clip creation.
- Angle switching.
- Manual color judgment.
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

If `00_color_prep_all_takes` already exists, it is preserved by default so
manual grades are not destroyed. Rebuild it only when you explicitly want a
fresh color-prep timeline:

```bash
./scripts/pg prepare-resolve-session "<session-root>" --rebuild-color-prep --json
```

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
- `reports/auto-group-plan.md` shows the intended angle labeling, lane IDs,
  source filenames, and confidence scores. Treat this as part of E2E
  acceptance.
- `reports/take-order.md` shows the inferred shooting order. Same-angle source
  file times are the strongest evidence; cross-setup merges are marked as
  tentative and do not rename take folders.
- `session.yaml` exists.
- No obvious take or angle is missing.

## 4. Stage B: Resolve Bootstrap

`prepare-resolve-session` prepares Resolve for manual work.

It performs:

- Resolve project creation/opening.
- Project library checks.
- Media import.
- Waveform sync.
- `00_color_prep_all_takes` creation.
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
- Existing `00_color_prep_all_takes` timelines are preserved on rerun unless
  `--rebuild-color-prep` is passed.
- If Resolve cache/stills storage warnings appear, restart Resolve and rerun
  `prepare-resolve-session`.
- `prepare-resolve-session` performs post-reload verification by saving,
  closing, reloading, and inspecting the Resolve project before reporting
  success.

## 5. Read the Markdown Reports

Start with the Markdown files:

```text
<session-root>/reports/prepare-resolve-session.md
<session-root>/reports/inspect-resolve-session.md
<session-root>/reports/auto-group-plan.md
<session-root>/reports/operator-handoff.md
```

The JSON reports remain available for automation, but the Markdown reports are
the human-facing source of truth.  Check `prepare-resolve-session.md` for:

- auto-group angle labeling in `auto-group-plan.md`;
- stage status;
- post-reload verification;
- color prep layout, angles, markers, and track count;
- take-by-take placement;
- low sync-confidence warnings.

## 6. Read the Operator Handoff

Generated files:

```text
<session-root>/reports/operator-handoff.md
<session-root>/reports/operator-handoff.json
```

The handoff includes:

- Session information.
- Resolve project name.
- Color-prep timeline name.
- Take list.
- Angle and audio files per take.
- Expected color management.
- Manual Resolve next steps.
- Repo responsibility boundaries.

Use this file as the checklist before entering manual Resolve work.

## 7. Optional Review Aids

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

## 8. Grade the Color-Prep Timeline

Open:

```text
00_color_prep_all_takes
```

This timeline uses a compact layout.  Semantic angle labels can span the
whole session (`angle-a`, `angle-b`, ..., including different day/night camera
setups), but each take is packed onto the first available video tracks:

```text
V3: take-01 angle-c --- gap --- take-02 angle-f --- gap --- ...
V2: take-01 angle-b --- gap --- take-02 angle-e --- gap --- ...
V1: take-01 angle-a --- gap --- take-02 angle-d --- gap --- ...
A1: master audio take-01 --- gap --- take-02 --- gap --- ...
```

The video tracks are named `compact-v1`, `compact-v2`, and so on.  The clip
items keep the semantic angle labels, so the Color page still shows which
angle you are grading while avoiding sparse empty tracks. The video items are
video-only and the audio track is the external `audio.aif` / `audio-edit.wav`
path. Camera scratch audio is not placed in this timeline.

Go to the Color page and work on the source angle timeline items.

Standard policy:

```text
Use Local Grades
```

Before grading, make the viewer unambiguous:

```text
Image Wipe / Split / Highlight: OFF
Gallery hover preview: avoid while judging the current clip
```

`Image Wipe`, `Split`, and `Highlight` are comparison modes for stills or other
shots. Turn them off while making base corrections or grabbing stills. If a
vertical split, checkerboard, highlight overlay, or A/B comparison is visible,
you are not looking at the current clip by itself.

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

Safe per-angle grading loop:

1. Click the target source angle clip in the Color page filmstrip.
2. Confirm wipe/split/highlight comparison is off.
3. Make the Local Grade.
4. Move to the next angle or take and repeat.

Do not use Remote Grades, Shared Nodes, or CDL commands for the standard
workflow. One source is used once, so Local Grades plus stills are safer and
easier to reason about.

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

Do not rely on Resolve's automatic still names such as `1.2.1` or `3.1.2` for
take/angle meaning. Rename every useful still immediately.

Gallery still behavior:

- A still stores a frame image plus grade metadata.
- Hovering or selecting a still can preview that still or its grade in the
  viewer.
- Hover/preview does not apply the grade to the current clip.
- Only `Apply Grade` changes the selected clip.
- If Image Wipe/Split/Highlight is on, the viewer may show the Gallery still
  and current clip at the same time.

Safe still capture:

1. Open `00_color_prep_all_takes`.
2. Go to the Color page.
3. Turn `Image Wipe`, `Split`, and `Highlight` off.
4. Do not hover over Gallery stills while judging the current frame.
5. Click the target angle clip in the Color page filmstrip.
6. Stop playback on the frame you want to save.
7. Right-click the viewer and choose `Grab Still`.
8. Rename the new still immediately with take and angle.

For the next take:

1. Stay in `00_color_prep_all_takes`.
2. Select the matching angle in the next take.
3. Right-click the renamed Gallery Still.
4. Apply Grade.
5. Adjust exposure and white balance for the new take.

Treat stills as starting points, not finished copies.

## 10. Manual Multicam Conversion

After color prep, duplicate `00_color_prep_all_takes` before multicam
conversion or downstream editing. Keep the original color-prep timeline as the
grade source you can return to.

Manual multicam conversion is intentionally outside this repo's standard
automation. If conversion or editing damages the structure, rebuild from the
duplicate rather than overwriting the color-prep source.

## 11. Build the Piece Timeline

After color prep and manual multicam conversion, assemble the piece timeline.

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

The source angle matching should already be done before this stage.

## 12. Final Timeline Finishing

Apply only light Local Grades on the final piece timeline.

Use final timeline grades for:

- Smoothing take-to-take transitions.
- Minor global brightness matching.
- Final tone polish.

Do not fix angle-specific problems here. If one angle is wrong, return to
`00_color_prep_all_takes` or its duplicated source timeline and fix the source
angle there.

## 13. Audio Cleanup

The final timeline should use only the external audio path.

Confirm:

- Camera audio is not mixed into the final output.
- Multiple scratch tracks are not causing phase issues.
- Take boundaries are not audibly abrupt.
- If needed, replace with a Logic/DAW-processed final audio file.

## 14. Pre-Export Checklist

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

## 15. YouTube SDR Export

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

## 16. CLI Summary

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

## 17. Shortest Practical Route

```text
1. Put media into <session-root>/incoming.
2. Run the skill/CLI through Stage A/B.
3. Read reports/operator-handoff.md.
4. Open `00_color_prep_all_takes`.
5. In Color page, turn wipe/split/highlight off.
6. Match source angle clips with Local Grades.
7. Rename Gallery Stills and use them as starting points for the next take.
8. Duplicate the color-prep timeline before manual multicam conversion.
9. Assemble the piece timeline.
10. Switch angles and cut.
11. Lightly finish on the final timeline.
12. Confirm 29.97, Rec.709, and external-only audio.
13. Export SDR for YouTube.
```
