# davinci-session-bootstrap skill

User-global Claude Code skill that wraps the piano-guard CLI to bootstrap
a new piano session in DaVinci Resolve, end to end.

Invoke from any directory in Claude Code:

```
/davinci-session-bootstrap <session-root>
```

The skill reads `SKILL.md` and walks Claude through three stages:

1. **group-session** — `incoming/*.mp4 + audio.{wav,aif,...}` → `takes/take-XX/`.
2. **prepare-resolve-session** — create / open Resolve project, generate
   audio-edit proxies, waveform-sync each take.
3. **AI cross-angle color review** — render stills, propose per-clip CDLs,
   preview offline, commit to `auto-state-99-v1` in Resolve. Detail lives
   in `color-review.md` (loaded on demand).

Operator can opt out of stage 3 per session ("skip color" / 「色は手動でやる」).

## First-time setup

The skill bundles its own copy of piano-guard inside this directory.
You must build the venv once:

```bash
~/.claude/skills/davinci-session-bootstrap/scripts/install.sh
```

This requires:

- `uv` (https://docs.astral.sh/uv/, or `brew install uv`)
- Python 3.11 available on the system
- About 3 GB of disk for the bundled torch / opencv / sam2 wheels
- Internet access on first install (later runs are offline)

The build takes 1–3 minutes on a fresh machine.

## Updating the bundled CLI

When piano-guard upstream changes:

```bash
~/.claude/skills/davinci-session-bootstrap/scripts/sync-from-source.sh
~/.claude/skills/davinci-session-bootstrap/scripts/install.sh   # only if pyproject.toml or uv.lock changed
```

`sync-from-source.sh` reads from `~/ghq/github.com/g960059/davinci-automation`
by default. Override with `PIANO_GUARD_SRC=/path/to/repo` if your local
clone lives elsewhere.

The editable install picks up Python source edits without re-running
install.sh — only re-install when dependency metadata changes.

## The `.venv` is a disposable cache

`scripts/install.sh` creates `.venv/` with all bundled dependencies. This
directory is **not** durable:

- After macOS / Homebrew / Python upgrades, the venv's interpreter
  symlink may break. Re-run `install.sh` to rebuild.
- Do not check `.venv/` into git or sync it via Dropbox / iCloud — it is
  machine-local and large (~3 GB).
- If `scripts/pg --help` ever fails, the first thing to try is re-running
  `install.sh`.

## What the skill expects on disk

For a new session at `<session-root>` (e.g. `/Volumes/PortableSSD/my-session/`):

```
<session-root>/
├── incoming/
│   ├── <camera files>.mp4     # one per (take, angle) — at least one
│   └── audio.wav              # Logic Pro bounce or sync-recorder; required
└── (everything else is created by the skill)
```

After bootstrap:

```
<session-root>/
├── incoming/                  # emptied; files moved into takes/ or excluded/
├── session.yaml               # session-level config including reference_angle
├── takes/
│   └── take-XX/
│       ├── angle-X.mp4
│       ├── audio.wav          # original
│       ├── audio-edit.wav     # 48 kHz / 24-bit proxy for Resolve sync
│       └── take.yaml
├── reports/
│   ├── prepare-resolve-session.json
│   ├── stills/                # Stage C: per-clip PNGs (HLG→Rec.709 tonemapped)
│   │   └── <take>/<angle>.png
│   └── manual-cdl.json        # Stage C: CDLs committed to Resolve
└── resolve/                   # .drp snapshot of the bootstrapped project
```

## Scope

In scope (Stage C):

- **Color review** (per-clip Primary Balance / CDL). Operator can opt out
  per session if they want to grade manually in Resolve.

Out of scope:

- **Editorial assembly**: multicam clips, Session_Assembly timeline,
  Piece_* timelines remain manual operations in Resolve.
- **Logic Pro automation**: piano-guard reads the `.logicx` path as a
  sidecar reference but does not drive Logic Pro.
- **Auto-installation**: the operator runs `install.sh` once; the skill
  never silently runs network/build inside a Claude turn.
- **LUT-based grading**: by design only Primary Balance / CDL is written
  to Resolve, since LUTs are opaque and not operator-editable.

## Source of the vendored CLI

The bundled `src/piano_guard/` is a copy of
`~/ghq/github.com/g960059/davinci-automation/src/piano_guard/` at the time
of the last `sync-from-source.sh` run. Check the upstream repo for the
canonical source and tests.
