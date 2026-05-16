# Stage C — Cross-angle color review (closed loop)

Supporting file for `SKILL.md`. Read this when entering Stage C.

You (Claude) look at rendered stills, compare angles, propose ASC CDLs,
**preview each proposal offline before committing to Resolve**, and iterate
until the visual matches the reference. The operator confirms at the end.

**You are the vision + judgment layer. The CLI is the mechanical layer.**

Commit target: a named remote version `auto-state-99-v1` on each clip's
source pool item. `Version 1` (ungraded) is never touched, so the operator
can always bypass.

## Inputs required

- A session root with `session.yaml` and `takes/take-XX/angle-*.mp4` —
  produced by Stage A.
- DaVinci Resolve running and the session's project open — produced by
  Stage B.
- Resolve project color management on **YRGB Auto SDR Rec.709** (Node 1 in
  Rec.709 Scene). Stage B applies this; if the operator changed it,
  preview-cdl's offline math will diverge from Resolve's actual application
  — surface that and ask the operator to revert before committing.

## Closed-loop workflow

You drive the loop end-to-end. Operator only confirms at the end.

### 1. Render stills (once per session)

```bash
"${CLAUDE_SKILL_DIR}/scripts/pg" render-stills "<session-root>" --json
```

Outputs `<session-root>/reports/stills/<take_id>/<angle>.png` (HLG → Rec.709
SDR tonemapped, 960 px wide).

### 2. Identify the reference angle

Read `<session-root>/session.yaml` — `reference_angle` is set by Stage A
(operator picked it as the WB anchor). If somehow null, ask the operator
before proceeding rather than guessing.

State the reference + reasoning in your reply, e.g.:
> "Using `angle-b` as reference per session.yaml. Side-view has window
> daylight grounding white-key WB; targets will be matched to it."

### 3. For each non-reference clip — closed-loop iteration

```
[3a] Read the source still: <session>/reports/stills/<take>/<angle>.png
[3b] Read the reference still: <session>/reports/stills/<take>/<ref>.png
[3c] Visually compare → propose slope+offset
[3d] pg preview-cdl --source-png S --slope R,G,B --offset R,G,B --out preview.png
[3e] Read the preview PNG with Read tool
[3f] Compare preview to reference. Match? → goto 3g. Off? → revise, back to 3d.
[3g] pg manual-cdl ... → Resolve writes auto-state-99-v1
```

Preview-cdl is ~100 ms per call and offline — iterate as many times as
needed before committing.

#### Visual analysis cheatsheet

When comparing target still to reference still:

- **White keys**: are target keys warmer (more R/yellow), cooler (more B),
  dimmer, brighter? Estimate the per-channel ratio you'd need to multiply
  target by to match reference. That's the slope.
- **Piano body**: same question, but **discount reflections** mentally.
  If > 30% of the body is reflection (window in shot behind piano), trust
  white-key judgment more.
- **Skin tones** (if pianist visible): warmer/cooler/redder?
- **Overall luma**: target underexposed → offset > 0; overexposed → offset < 0.

#### Conservative bounds

Start within these ranges; widen only with strong visual evidence:

- `slope` per channel: **0.85 – 1.15** for typical cross-angle drift.
- `offset` per channel: **−0.02 to 0.02**.

Wider slopes (>1.5 or <0.7) tend to clip highlights or crush shadows.
Don't propose those.

#### Stopping the loop

Stop iterating preview-cdl when:

- Preview's white keys roughly match reference's white keys (visually
  equivalent is enough — not pixel parity).
- Preview's overall cast aligns with reference (no obvious R/G/B push).
- Preview's skin / body tones don't look broken.

Then call manual-cdl to commit. **Don't iterate forever** — 3–5 preview
cycles per clip is the practical limit.

### 4. Commit via manual-cdl

```bash
"${CLAUDE_SKILL_DIR}/scripts/pg" manual-cdl "<session-root>" \
  --clip-id "take-04/angle-c" \
  --slope "1.08,0.95,1.18" \
  --offset "0.02,0.0,-0.01" \
  --json
```

Writes to `auto-state-99-v1` remote version. Verify the JSON:
- `applied[0].setcdl_rc` must be `true`
- `tools_present` includes `Primary Balance`

If skipped, the reason field tells you why (e.g. `multicam_exists` →
need to apply before multicam creation; `clip_not_found` → Stage B
hasn't run for this clip).

### 5. Final operator verification

After all clips committed, briefly summarize what you applied:

```
Take 01:
  angle-b → identity (reference)
  angle-c → slope [0.91, 0.98, 1.06] (cool the warm tungsten cast)
  angle-d → slope [0.95, 0.99, 1.04] (subtle cool)

Take 04:
  angle-b → slope [1.05, 0.97, 1.01]
  ...
```

Then ask the operator to load each clip's `auto-state-99-v1` in Resolve's
Color page (right-click thumbnail → Remote Versions → auto-state-99-v1 →
Load) and confirm the look.

If the operator pushes back on a specific clip ("take-03/angle-c is still
too warm"), iterate via preview-cdl again, then re-apply manual-cdl on
that one clip. Don't redo clips the operator accepted.

## Design principles to respect

- **Every grade is a starter draft.** The operator opens Resolve and
  tweaks. Primary Balance via SetCDL is editable; LUTs are opaque — never
  propose LUTs from this stage.
- **Write only to named remote versions.** Never touch `Version 1` (the
  ungraded source). Always commit to `auto-state-99-v1` so the operator
  can bypass.
- **One session per Stage C invocation.** Don't batch across sessions —
  each has its own reference + lighting context.
- **Explain your judgment.** When proposing a CDL, say what you're
  correcting for ("reducing B slope to balance against sky in angle-d
  reflection"). Helps the operator catch systematic errors and trust
  your calls over time.

## Common pitfalls

- **Mistaking reflections for content.** Piano lid mirrors windows, sheet
  music, pianist's face. When judging body color, discount reflective
  regions; rely on white keys more if the body is mostly reflection.
- **Over-correcting shadows.** HLG 8-bit linearized blacks are noisy
  around luma 0.005–0.02. Don't chase shadow color precision; the data
  isn't there.
- **Matching to a wrong reference.** If the reference angle is itself
  off-neutral (e.g. warm tungsten room), don't try to neutralize it to
  D65. Match targets to it as-is. The operator can apply a creative WB
  layer on top in post.
- **Day vs night propagation.** A grade fit on a day take ≠ the right
  grade for a night take of the same angle. Treat them as separate
  sub-sessions; pick a reference per regime if needed.
- **Trusting preview-cdl when project color management isn't aligned.**
  If the operator's project is NOT YRGB Auto SDR Rec.709, the offline
  preview's CDL math diverges from Resolve's actual application. Flag
  this and ask the operator to verify directly in Resolve before
  committing.

## Escape hatches

- **Iteration not converging?** After 5 preview cycles without visually
  matching the reference, stop. Either:
  (a) The reference itself is content-mismatched (different scene, not
      just different angle) — pick a more comparable reference.
  (b) The target needs a transform CDL can't express (cross-channel
      matrix, non-linear curve). Mark the clip as "needs manual Resolve
      grading" and move on.
- **Identity reset.** To clear an `auto-state-99-v1` grade, manual-cdl
  with `--slope 1,1,1 --offset 0,0,0`. Operator can also reset via
  Color page Node 1 → Reset Node Grade.
- **Pre-existing pipeline grades.** Some sessions may carry
  `auto-state-NN-v1` versions from legacy `auto-color-normalize` runs.
  They coexist with `auto-state-99-v1` and the operator A/Bs in the
  Versions panel — leave them alone unless asked to clean up.

## CLI reference

All commands are idempotent. Output JSON reports live in
`<session-root>/reports/`.

- `pg render-stills <session-root> [--width N] [--json]`
- `pg preview-cdl --source-png S --slope R,G,B --offset R,G,B --out preview.png [--json]`
- `pg manual-cdl <session-root> --clip-id X --slope R,G,B --offset R,G,B [--dry-run] [--json]`
- `pg grab-still <session-root> --clip-id X --out path.png [--version-name V] [--json]`
  — Drives Resolve to render the actual graded viewer frame.
  **Broken on Resolve 20.3.2** (ExportStills returns False; tracked but
  not fixed). Use `preview-cdl` for the offline equivalent until then.
