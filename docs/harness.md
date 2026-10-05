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
| Merge gate | ruff format + lint, bandit, pyright (strict), pytest (+ coverage), INDEX check; web: lint, typecheck, test, build. `gate.sh` runs the integration tests too when `HUDDLE_TEST_POSTGRES_DSN` and `HUDDLE_TEST_REDIS_URL` are set (start stores with `eval "$(scripts/dev-stores.sh)"`) so it matches CI; without them it says it skipped them | `scripts/gate.sh`, `.github/workflows/ci.yml` |
| Integration tests | Real Postgres/Redis. Skip locally if unreachable; **fail** in CI (`HUDDLE_REQUIRE_STORES=1`) | `@pytest.mark.integration` |
| Secret scan | TruffleHog, verified only, PR + weekly | `secret-scan.yml` |
| Dependency CVEs | OSV-Scanner on `uv.lock`, weekly + on lockfile change | `osv-scan.yml` |
| Workflow audit | zizmor on `.github/`; waivers need a written reason | `zizmor.yml`, `.github/zizmor.yml` |
| SAST | bandit + pyright run in the gate. CodeQL (advisory first; promote to required once stable) is in `codeql.yml` but skips itself while the repository is private, because code scanning is free only for public repositories; it starts running automatically once the repo is public | `codeql.yml` |
| Supply chain | Locked deps (`uv.lock`, `package-lock.json`), Dependabot weekly, least-privilege `permissions:`, `persist-credentials: false`, `timeout-minutes`, concurrency groups | all workflows |
| Exceptions | Every scanner has a documented waiver path: bandit inline `# nosec BXXX` with rationale per site; zizmor `.github/zizmor.yml`; OSV `osv-scanner.toml`; TruffleHog `.trufflehogignore` | |
| Repo map | `INDEX.md` must list every package/dir (`scripts/check_index.py`) | |

## Real-model decider eval (run it by hand)

CI cannot run Ollama, so the agent evals in `services/agent/tests/evals` use a scripted stand-in model: they prove the rules around the model (gate, budget, pipeline), not the model's judgement. To measure the real decider, run the labelled conversations in `services/agent/src/huddle_agent/evals/cases.py` against your Ollama:

```sh
make eval-decider                                   # decider only, 3 repeats per case
make eval-decider a="--with-writer"                 # also run the real writer and check its replies
make eval-decider a="--only stuck -v --repeats 5"   # one category/case, every trial's reason
make eval-decider a="--json /tmp/decider.json --min-accuracy 0.8"   # for scripting
```

Cases the deterministic gate decides (addressing the agent, answering its question, greetings) never reach the model and are reported under `gate`; the `decider` rows are the model's judgement. Each miss prints the decider's own reason. Add a case whenever manual testing finds a miss; the labels come from the spec policy (§5), and CI checks that every gate-decided case matches its label (`test_agent_decider_eval.py`). Options: `--decider`, `--writer`, `--url`, `--decider-tokens`, `--decider-timeout`.

## Spec anti-drift

- `docs/specs/*` are the source of truth. Contract types live in `libs/contracts` and mirror Deep Dive §3/§4/§6; a change to either must change both in the same PR.
- New decisions get an R-number in `docs/decisions.md`.

## Deliberately not adopted (yet)

OpenSSF Scorecard badge, SBOM/signing/provenance, container scanning, fuzzing beyond property tests: deferred until there are releases and built images (same call the reference project made).

## Platform settings to apply by hand (cannot be set from code)

- **Protect `master`:** import `.github/rulesets/master-protection.json` (Settings → Rules → Rulesets → New ruleset → Import a ruleset). It blocks deletion and force-push, requires a pull request (0 approvals, since this is a solo repo; raise it when there are more people) with review threads resolved, and requires the `test`, `web`, `TruffleHog` and `OSV-Scanner` checks. It has no bypass actors, so it applies to admins too. Every required check must run on every PR (a required check that is skipped by a `paths:` filter blocks merging), so OSV-Scanner has no path filter. Add `CodeQL` once the repo is public and it has a stable history; keep `zizmor` non-required (it only runs when `.github/` changes).
- Enable secret scanning with push protection, private vulnerability reporting, Dependabot security updates.
- Third-party actions are pinned to commit SHAs with the version in a trailing comment; Dependabot (github-actions ecosystem, 7-day cooldown) maintains the pins, and zizmor fails the build on an unpinned action.
