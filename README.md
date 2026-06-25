# MyPilot device delivery (this repo is named `openpilot` on purpose)

**This is not a place to read or edit code.** It exists only so a comma device can install MyPilot:
the comma installer clones `github.com/<owner>/openpilot@<branch>`, so the repo **must** be named
`openpilot`.

## What's here

- **`mypilot-*` branches** — auto-generated, installable builds = an upstream base
  (sunnypilot / openpilot / frogpilot) + the MyPilot agent overlay. Install one on a comma 4 via
  **Settings → Software → Custom Software**:
  - `castanley/mypilot-mici` — sunnypilot base (recommended, validated) · `…-staging`
  - `castanley/mypilot-mici-op` — openpilot base (experimental) · `…-op-staging`
  - `castanley/mypilot-mici-frog` — frogpilot base (experimental) · `…-frog-staging`
- **`master`** — hosts the `sync-mypilot` workflow and this note (it otherwise mirrors the upstream
  tree for reference). The installable builds live on the branches above.

## Source of truth

Everything is built from the **MyPilot monorepo — https://github.com/castanley/mypilot**. The
`sync-mypilot` workflow clones it and runs `mypilot-mici/publish.sh` daily, rebuilding each branch =
`<upstream base>` + the freshly assembled MyPilot overlay and force-pushing only what changed. No
driving/safety code is modified on any branch.

**Do not hand-edit the `mypilot-*` branches** — changes are overwritten on the next sync. Open
issues / PRs against the monorepo instead.
