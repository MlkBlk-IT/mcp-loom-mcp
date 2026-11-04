# Mcp-Loom — Release notes

> This file is the release annotation for each published version. It is maintained in English;
> the rest of the repository (code, comments, internal docs) is written in French.

---

## Unreleased — 2026-11-04

### Changed

- **Dashboard v4 rewrite in `dash/`** (no engine change): brighter glass palette, simplified panels
  with details in a side drawer, standalone simple canvas charts (calls/min, p50/p95 latency,
  savings), less text. Three new nav sections in English — **Quickstart**, **How-to**, **Help** —
  plus full dashboard translation to English (`lang="en"`, assets `?v=8`).

### Fixed

- **Dashboard stuck on "connecting..." with empty charts**: a `SyntaxError` at `dash/static/app.js:901`
  (`linked+' 'sessions linked'`, introduced during the English translation) prevented the whole
  script from running. Fixed to `linked+' sessions linked'`; `node --check` clean.
- **SSE endpoint** (`GET /api/stream`) now sends `Access-Control-Allow-Origin: *` and
  `Access-Control-Allow-Headers: Cache-Control` for strict browsers.

### Verified

- `GET /api/snapshot` → 200 (`db.exists: true`, 48 sessions, 1684 chunks, `saved_ratio: 0.5481`).
  `GET /api/stream` → 200 `text/event-stream` with CORS headers and live `data:` flux.
  Full browser render after `Ctrl+Shift+R` not screenshot-verified in this session.

---

## v0.1.3 — 2026-10-26

**Prepared for publication — the first registry publish is still pending.**

### Added

- **The packages are built, tagged and ready to publish.** The repository, the PyPI distribution and
  the npm package all carry the same name — `mcp-loom-mcp` — and the README now gives the registry
  install commands (`uv tool install mcp-loom-mcp`, `pip install mcp-loom-mcp`,
  `npx -y mcp-loom-mcp`) with links to both registry pages. Those commands return a 404 until the
  first publish happens; install from source until then.
- **PyPI publication will use trusted publishing (OIDC).** The `Publish` workflow mints a short-lived
  token from the GitHub Actions identity: no PyPI API token is stored on any machine.
- The install guide now states which name is the package and which one is the console script
  (`mcp-loom-mcp` vs `loom-mcp`), and the `uvx` snippets pass `--from mcp-loom-mcp`.

### Fixed

- **`docs/INSTALL.md` still told users to install `loom-mcp`** — a name that belongs to other
  projects on PyPI and npm. The install commands, the Claude Desktop / Hermes / Node-only client
  snippets and the wheel + sdist filenames now use the real names.
- The v0.1.0 notes claimed `pip install loom-mcp` / `npx loom-mcp`: v0.1.0 was a GitHub Release
  only and never reached a registry.

### Changed

- Version bumped to 0.1.3 — `pyproject.toml` and `package.json` now match the `v0.1.3` tag and this
  release.

### Not changed

No code, no tool, no test — the context engine is untouched. This release publishes the package and
fixes the documentation.

---

## v0.1.2 — 2026-10-26

**Package renamed to `mcp-loom-mcp`, and the README finally has an Installation section.**

### Fixed

- **The distribution name was unpublishable.** `loom-mcp` on PyPI is a *Loom video management*
  MCP server by another author, and `loom-mcp` on npm is `Spongeacer/Loom-MCP-Server`. Publishing
  under that name is impossible, and `pip install loom-mcp` / `npx loom-mcp` install someone else's
  code. The distribution is now **`mcp-loom-mcp`** (`pyproject.toml`, `package.json`), a name that is
  free on both registries — `mcp-loom` is taken on both too.
- **The README had no installation section at all**, while the project was already published as a
  GitHub Release. It now documents the install-from-source command, the MCP client snippet, the
  dev-extras setup and the admin CLI.

### Changed

- npm bin renamed `loom-mcp` → `mcp-loom-mcp`, so `npx mcp-loom-mcp` resolves to this project.
- The Python console scripts are unchanged: `loom-mcp` (the server MCP clients spawn) and `loom`
  (the admin CLI). The import package is still `loom`.
- **The GitHub repository was renamed `Mcp-Loom` → `mcp-loom-mcp`**, so the repo, the PyPI
  distribution and the npm package now carry the same name. The old URL
  (`github.com/MlkBlk-IT/Mcp-Loom`) redirects automatically; all internal links were updated.
- Version bumped to 0.1.2.

### Not changed

Nothing in the context engine — no code path, no tool, no test. This release only fixes the
packaging identity and the documentation.

---

## v0.1.1 — 2026-10-26

**Documentation accuracy + dashboard configuration.** This release contains no change to the
context engine itself — it fixes the documentation and the local dashboard wiring.

### Added
- **Configurable dashboard port** — `dash/server.py` reads `LOOM_DASH_PORT` (default `8775`).
  The port was previously hard-coded to `8765`, which collided with other local services.
- **`UPDATE-NOTES.md`** — this file, the release annotation for each version.
- **README version badge** pointing at the GitHub releases.

### Fixed
- **Dashboard pointed at a test database** — `dash/tray.py` opened `D:\Loom\_test\loom_claude.db`
  instead of the real drawer. It now uses the standard `C:\Users\Tech\.loom\loom.db` location, and
  the tray opens the dashboard on the configurable port.
- **README tool count** — the tool table listed 13 tools; the server actually exposes **17**
  (`ctx_verify`, `ctx_transfer`, `ctx_rebuild_index` and `ctx_vector_stats` were missing).
- **README test count** — the README claimed "38 green tests" and the banner claimed "72 tests
  pass". The measured state is **83 passed / 19 failed / 1 collection error** out of 103 test
  functions in 20 files.

### Removed
- Stale dashboard captures (`dash/shot-*.png`, `loom-shot-v4.js`) and the `.bak` editing backups
  under `dash/static/` — working artefacts, not deliverables. `*.bak*` is now git-ignored.

### Changed
- `README.md` — tool table completed to 17 tools, the test-suite section rewritten to state the
  real numbers, the WIP banner refreshed (2026-10-26) and pointed at `PROGRESS.md`.
- Version bumped to 0.1.1 (`pyproject.toml`, `package.json`).

### Honest status — read before using this
- **Not ready for production.** No token savings have been demonstrated yet: the published
  `saved_pct` figures (94–99%) measured the anchor size alone and are **invalid**.
- The current real measurement (LM Studio, small local models, small documents) shows Loom
  **costing more** tokens than the baseline, because of the fixed anchor overhead and the tool
  schemas. The large-context test with a cloud model has not been run.
- The decision criterion was frozen before the benchmark: the same success rate with **≥ 35% fewer
  total tokens** (in + out), retrieval and tool tax included. Otherwise: pivot or stop.
- 4 of the 19 test failures are **real anchor regressions** (`port` kind no longer produced, rare
  kinds no longer reaching the `strict` anchor, dates still extracted as numbers). The other 15
  come from a code refactor that was not followed by a test update.
- The `saved_pct` numbers in the benchmark tables below are kept for history, with the invalidation
  notice above.

---

## v0.1.0 — 2026-10-25

- First installable release — **published as a GitHub Release only**. The distribution was named
  `loom-mcp` at the time, but that name belongs to other projects on PyPI and npm, so this version
  was never pushed to a registry; it was renamed to `mcp-loom-mcp` in v0.1.2.
- Portable MCP server — 17 tools, SQLite + FTS5 drawer, admin CLI (`loom health`, `loom stats`,
  `loom vacuum`, `loom search`…), wheel + sdist + npm wrapper.
