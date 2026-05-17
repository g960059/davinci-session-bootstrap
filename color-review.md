# Optional Color Review Aids

This repo no longer treats AI CDL as the production color path. The standard
workflow stops after Stage B and hands the session to the Resolve operator for
manual multicam color work with Local Grades and Gallery Stills.

The commands below are still useful as review aids:

```bash
pg render-stills "<session-root>" --json
pg review-manifest "<session-root>" --json
pg contact-sheet "<session-root>" --json
```

They render stills, describe angle coverage, and create a contact sheet. They
do not apply grades in Resolve.

## Inputs

- `<session-root>/session.yaml`.
- `<session-root>/takes/take-XX/angle-*.mp4`.
- Resolve project prepared by Stage B.
- Project color management left on the Stage B path:
  `Rec.2100 HLG` input to `Rec.709 Gamma 2.4` timeline/output with
  `DaVinci` input/output DRT.

## Review Use

`review-manifest` does not require a reference angle. It lists every rendered
clip still for visual review. Use the contact sheet for fast session-wide
scanning, then inspect individual stills as needed.

Read `color-review-manifest.json.angle_summary`:

- `all_angles`: every angle label present anywhere in the session.
- `common_angles`: angle labels present in every take.
- `missing_by_take`: take-level camera coverage gaps.

The contact sheet uses fixed angle columns from `all_angles`; rows do not shift
when a take is missing an angle.

## Manual Resolve Color Policy

- Grade inside each multicam timeline after `Open in Timeline`.
- Use Local Grades for the standard workflow.
- Match angles within the same take first.
- Use Gallery Stills as starting points for the next take.
- Use the final piece timeline only for light take-to-take finishing.

Visual priorities:

- White keys are the primary neutral reference.
- Discount piano-body reflections when judging color.
- Use skin tone only when visible and comparable.
- Keep the black piano finish from crushing too hard.
- Preserve a believable gold plate and wood floor color.
- Do not force exact sameness across takes when sunlight naturally changes.
- Do not neutralize the whole session to an imagined D65 target.

## Experimental CDL Commands

`preview-cdl` and `manual-cdl` are retained for debugging older sessions and
small CDL experiments. They are not part of the standard skill workflow.

Commit target: remote version `auto-state-99-v1` on the source MediaPoolItem.
`Version 1` is never touched.

```bash
pg preview-cdl --source-png still.png \
  --slope 1,1,1 \
  --offset 0,0,0 \
  --out preview.png \
  --json

pg manual-cdl "<session-root>" \
  --clip-id "take-04/angle-c" \
  --slope "1,1,1" \
  --offset "0,0,0" \
  --dry-run \
  --json
```

`manual-cdl` only writes CDL values to the named remote version. It does not
change the project-wide HLG -> SDR transform. Identity CDL must therefore look
the same as the corrected base image. If the base image is clipped, yellow, or
otherwise broken, fix Resolve color management first instead of expecting
`auto-state-99-v1` to repair it.

Normal CDL experiment bounds:

- `slope`: `0.90` to `1.10`.
- `offset`: `-0.02` to `0.02`.

Anything beyond that should be done manually in Resolve, not pushed through
CDL automation.

## Failure Handling

- `still_missing`: rerun `render-stills`; if the source clip is missing, fix the
  take directory first.
- `multicam_exists`: `manual-cdl` is too late for that project state. Use manual
  Resolve grading inside the multicam timeline.
- `clip_not_found`: rerun Stage B so source clips are present in the Media Pool.

## CLI Reference

- `pg inspect-resolve-session <session-root> [--json]`
- `pg operator-handoff <session-root> [--json]`
- `pg render-stills <session-root> [--width N] [--json]`
- `pg review-manifest <session-root> [--json]`
- `pg contact-sheet <session-root> [--out path.png] [--json]`
- `pg preview-cdl --source-png S --slope R,G,B --offset R,G,B --out preview.png [--json]`
- `pg manual-cdl <session-root> --clip-id X --slope R,G,B --offset R,G,B [--dry-run] [--json]`
