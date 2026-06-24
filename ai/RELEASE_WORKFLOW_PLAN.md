# Plan: Simplify the release workflow & repair the 0.2.0 mess

## Context

Two compounding defects produced a broken release state:

1. **Fragile version detection.** `release.yml`'s version step used
   `re.search(r'^## \[(\d+\.\d+\.\d+)\]\s*$', changelog)` — which scans the
   **whole** CHANGELOG and matches the *first* undated bare version heading
   anywhere. An orphaned `## [0.2.0]` heading (never dated, ~line 519) was a
   landmine. When 0.8.0 was released and its heading got dated, a second
   release run fell through to `## [0.2.0]` and **downgraded** the project
   0.8.0 → 0.2.0 (commit `14509cf`, current `origin/main` HEAD). The only
   guard ("tag must not exist") didn't catch it because `v0.2.0` had never
   been tagged.

2. **Redundant / mis-wired workflows.** `release.yml` (dispatch-only) creates
   the tag + GitHub release, then *publishes the release before binaries
   exist* and dispatches `release-build.yml` to attach assets afterward.
   `release-build.yml` also has a `push: tags: v*.*.*` trigger that **never
   fires for CI-created tags** — GitHub does not trigger workflows from events
   made with the default `GITHUB_TOKEN`. So there are two build entry points,
   one of them dead, and an ordering that leaves a published release with no
   assets if the build fails.

Goal: one workflow, one clear release path, version detection that cannot
downgrade or pick a stray heading, and a clean `main` back at 0.8.0.

## Current broken state (verified)

- `origin/main` HEAD = `14509cf` "Release v0.2.0"; `pyproject.toml` and
  `src/pony/version.py` say `0.2.0` (should be `0.8.0`).
- Tag `v0.2.0` still exists locally **and on the remote** (only the GitHub
  *release* object was deleted by the user).
- CHANGELOG `## [0.2.0]` is mis-dated `2026-06-24` (real era: 2026-04-17,
  between 0.1.0 and 0.3.0 which are both 2026-04-17).
- `v0.8.0` tag + release are intact.

---

## Part A — Repair `main` and the stray tag

1. `git revert --no-commit 14509cf`. This restores `pyproject.toml` and
   `version.py` to `0.8.0`, but it also *un-dates* `## [0.2.0]` (reintroducing
   the landmine). So in the same staged change, **edit `CHANGELOG.md`** to set
   `## [0.2.0] - 2026-04-17` and keep its version link. Commit:
   `Revert erroneous 0.2.0 downgrade; restore 0.8.0`.
2. Delete the stray tag locally and on the remote:
   - `git tag -d v0.2.0`
   - `git push origin :refs/tags/v0.2.0`
3. Push the revert commit to `main`.
4. Verify: `pyproject.toml`/`version.py` == `0.8.0`; `git tag -l` has no
   `v0.2.0`; CHANGELOG first heading is `## [0.8.0] - 2026-06-24`.

(The GitHub `v0.2.0` *release* is already gone per the user; nothing to do
there.)

---

## Part B — Replace two workflows with one `release.yml`

**Delete `.github/workflows/release-build.yml` entirely.** Its build matrix
folds into the new workflow; its `validate-tag` job and dead `push: tags`
trigger are dropped.

**Rewrite `.github/workflows/release.yml`** as a single `workflow_dispatch`
workflow (keep the `prerelease` boolean input) with three sequential jobs and
`concurrency: { group: release, cancel-in-progress: false }` to stop two
release runs from racing (the proximate cause of the double-run).

### Job 1 — `prepare` (ubuntu-latest)
Checkout `fetch-depth: 0` with `GITHUB_TOKEN`; configure git bot. Python step,
**hardened version detection**:
- Read CHANGELOG; locate the **first** `^## \[` heading only (not a whole-file
  search). Require it to be an **undated bare version** `## [X.Y.Z]`. Abort
  with a clear message if the first heading is dated, is `[Unreleased]`, or
  isn't a bare semver — this makes the orphaned-heading landmine impossible.
- `new = X.Y.Z`, `tag = vX.Y.Z`, `today = date.today()`.
- **Guard 1:** abort if `git rev-parse refs/tags/{tag}` succeeds (tag exists).
- **Guard 2:** read current version from `pyproject.toml`; abort unless
  `tuple(new) > tuple(old)` (strict monotonic increase — blocks any
  downgrade like 0.8.0→0.2.0).
- Stamp `pyproject.toml` + `src/pony/version.py` to `new`; date the heading to
  `## [{new}] - {today}`; add/refresh the `[{new}]:` release link.
- Commit `Release v{new}`, **push the commit to `main`**, and emit outputs
  `version`, `tag`, `old_version`, and `sha` (`git rev-parse HEAD`).
- Do **not** create the tag here — it is created in `publish` after the build
  passes, so a failed build never leaves a dangling tag or published release.

### Job 2 — `build` (needs: prepare; matrix linux/macos/windows)
Reuse the existing `release-build.yml` build steps verbatim, but checkout
`ref: ${{ needs.prepare.outputs.sha }}` (the bumped commit) instead of a tag:
setup-uv → `uv sync --group dev --group build --group docs` → `pytest` →
`mkdocs build --strict` → Inno Setup on Windows →
`build.py --skip-tests --skip-docs --installer` → `upload-artifact`
(`name: pony-${platform}-${tag}`, `path: artifacts/`).

### Job 3 — `publish` (needs: build; ubuntu-latest)
Checkout `ref: ${{ needs.prepare.outputs.sha }}`, `fetch-depth: 0`, with token;
configure git.
- Create + push the tag: `git tag -a {tag} -m "Release {version}"` then
  `git push origin {tag}`.
- Build release notes (port the existing "Build release body" step: CHANGELOG
  section for `{version}` + README first paragraph + install block) →
  `release_body.md`.
- `actions/download-artifact@v4` with `path: dist`, `merge-multiple: true`.
- `gh release create {tag} --title "Pony Express {version}"
  --notes-file release_body.md [--prerelease] dist/*` — publishing the release
  **with all binaries attached in one shot, after the build succeeds.**

### Why this is simpler & correct
- One workflow, one trigger (`Actions → Create Release → Run workflow`), one
  linear path: prepare → build → publish.
- No cross-workflow dispatch, no dead `push: tags` trigger, no reliance on
  `GITHUB_TOKEN` triggering another workflow.
- Release is created **after** binaries exist → never a release with no assets.
- Version is validated once and cannot downgrade or match a stray heading.

---

## Part C — Keep docs in sync

- `ai/CONVENTIONS.md:47` — update the release line: detection reads **only the
  first CHANGELOG heading** and the version must be **strictly greater** than
  the current one; tag must not already exist. Drop any implication that a
  heading anywhere in the file is picked up.
- `docs/development.md:180-184` ("CI" section) — replace the `release-build.yml`
  description with the single `release.yml` flow (dispatch → prepare/build/
  publish; binaries attached on publish). Remove the "runs on every version
  tag / on Release publication" wording.

No `CHANGELOG.md` release-notes entry is added for this CI change (internal
tooling, not user-facing) and **no version strings are touched** outside the
Part A repair, per the project rules.

---

## Verification

1. **Detection logic, locally and offline.** Extract the Part-1 Python
   detection into a throwaway local run against the repaired `CHANGELOG.md` and
   confirm: (a) with a fresh `## [0.9.0]` first heading it returns `0.9.0`;
   (b) with the first heading dated (no new section) it aborts; (c) a heading
   `## [0.7.0]` (≤ current 0.8.0) aborts on the monotonic guard; (d) the
   orphaned `## [0.2.0]` deeper in the file is never reached.
2. **Workflow lint.** Run `actionlint` on `.github/workflows/release.yml` if
   available; otherwise validate YAML parses and job `needs`/outputs wiring.
3. **Repo gates unaffected:** `uv run python -m pytest` (workflow changes don't
   touch code, but confirm green and coverage ≥ 85%).
4. **End-to-end (user-triggered, post-merge):** cut a real patch release via
   the dispatch and confirm a single run produces the tag, the 3-platform
   assets, and a published release with notes — with nothing left dangling on
   failure.

## Files touched
- `.github/workflows/release.yml` — rewritten (single 3-job workflow)
- `.github/workflows/release-build.yml` — **deleted**
- `pyproject.toml`, `src/pony/version.py`, `CHANGELOG.md` — Part A repair only
- `ai/CONVENTIONS.md`, `docs/development.md` — doc sync
