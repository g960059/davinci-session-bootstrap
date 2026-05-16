# davinci-session-bootstrap skill

User-global Claude Code skill that wraps the piano-guard CLI to bootstrap a
new piano session in DaVinci Resolve.

Invoke from any directory in Claude Code:

```
/davinci-session-bootstrap <session-root>
```

The skill reads `SKILL.md` and walks Claude through `group-session` →
`prepare-resolve-session`. It does **not** include color stages — those
remain in `/color-review` (project-level skill in the davinci-automation
repo) or in the standalone `pg auto-color-normalize` command.

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
│   └── prepare-resolve-session.json
└── resolve/                   # .drp snapshot of the bootstrapped project
```

## What the skill does NOT do

- **Color grading**: handed off to `/color-review` or `pg auto-color-normalize`.
- **Editorial assembly**: multicam clips, Session_Assembly timeline,
  Piece_* timelines remain manual operations in Resolve.
- **Logic Pro automation**: piano-guard reads the `.logicx` path as a
  sidecar reference but does not drive Logic Pro.
- **Auto-installation**: the operator runs `install.sh` once; the skill
  never silently runs network/build inside a Claude turn.

## Source of the vendored CLI

The bundled `src/piano_guard/` is a copy of
`~/ghq/github.com/g960059/davinci-automation/src/piano_guard/` at the time
of the last `sync-from-source.sh` run. Check the upstream repo for the
canonical source and tests.
