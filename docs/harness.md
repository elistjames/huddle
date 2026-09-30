# Agent development harness

Adapted from the "Security from the first commit" post in https://github.com/fespino/resgraph (harness engineering: a control that isn't enforced, alarmed or measured is decoration). Only what fits this project is used.

## The loop every change follows

1. **Issue first.** Open an issue (task template) that cites spec sections / D-numbers. Intent lives in the issue, implementation in the PR.
2. **Branch, implement against the spec.** Cite D-numbers in code comments and PR text. Never contradict a locked decision silently: change the spec (and add a decision) first.
3. **Run `scripts/gate.sh` before every commit.** It is the same set of legs CI runs, in one command. All legs run even if one fails.
4. **Push and open a PR.** CI runs the gate plus scanners. Fix everything in one round trip.
5. **Independent review.** The reviewer sees only the spec and the diff, never the implementer's conversation (an agent reviewing its own code is an echo chamber). Use a fresh agent/session (`/code-review`) or a human.
6. **Merge only when required checks are green.**

## Layers

| Layer | What | Where |
|---|---|---|
| Merge gate | ruff format + lint, bandit, pyright (strict), pytest (+ coverage), INDEX check; web: lint, typecheck, test, build | `scripts/gate.sh`, `.github/workflows/ci.yml` |
| Integration tests | Real Postgres/Redis. Skip locally if unreachable; **fail** in CI (`HUDDLE_REQUIRE_STORES=1`) | `@pytest.mark.integration` |
| Secret scan | TruffleHog, verified only, PR + weekly | `secret-scan.yml` |
| Dependency CVEs | OSV-Scanner on `uv.lock`, weekly + on lockfile change | `osv-scan.yml` |
| Workflow audit | zizmor on `.github/`; waivers need a written reason | `zizmor.yml`, `.github/zizmor.yml` |
| SAST | bandit + pyright run in the gate. CodeQL (advisory first; promote to required once stable) is in `codeql.yml` but skips itself while the repository is private, because code scanning is free only for public repositories; it starts running automatically once the repo is public | `codeql.yml` |
| Supply chain | Locked deps (`uv.lock`, `package-lock.json`), Dependabot weekly, least-privilege `permissions:`, `persist-credentials: false`, `timeout-minutes`, concurrency groups | all workflows |
| Exceptions | Every scanner has a documented waiver path: bandit inline `# nosec BXXX` with rationale per site; zizmor `.github/zizmor.yml`; OSV `osv-scanner.toml`; TruffleHog `.trufflehogignore` | |
| Repo map | `INDEX.md` must list every package/dir (`scripts/check_index.py`) | |

## Spec anti-drift

- `docs/specs/*` are the source of truth. Contract types live in `libs/contracts` and mirror Deep Dive §3/§4/§6; a change to either must change both in the same PR.
- New decisions get an R-number in `docs/decisions.md`.

## Deliberately not adopted (yet)

OpenSSF Scorecard badge, SBOM/signing/provenance, container scanning, fuzzing beyond property tests: deferred until there are releases and built images (same call the reference project made).

## Platform settings to apply by hand (cannot be set from code)

- Protect `main`: no direct pushes, no force-push/deletion, `enforce_admins`, require the `test`, `web`, `TruffleHog`, `OSV-Scanner` checks; add `CodeQL` once stable. Keep `zizmor` non-required (path-conditional).
- Enable secret scanning with push protection, private vulnerability reporting, Dependabot security updates.
- Third-party actions are pinned to commit SHAs with the version in a trailing comment; Dependabot (github-actions ecosystem, 7-day cooldown) maintains the pins, and zizmor fails the build on an unpinned action.
