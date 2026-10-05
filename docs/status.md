# Build status and open items (2026-09-30)

Built to spec, all merged on the working branch: `libs/contracts`, `services/chat`, `services/gateway`, `services/agent`, `apps/web`, harness (`scripts/gate.sh`, CI, scanners). Verified: gate green; 294 Python tests with real Postgres 16 + Redis; web lint/typecheck/167 tests/build; `scripts/e2e_smoke.py` runs real chat + gateway over real stores (two guests, ordering, idempotent resend, receipt cursors, reconnect catch-up).

## Not verified
- **No real Ollama or Llama models were run.** Decider judgment, writer quality/length, safety check, real token counts, latency vs the 10 s/45 s timeouts, `num_ctx` effect and `away` detection are only tested against mocks and a scripted fake model.
- The web client has not been driven against the real gateway/chat in a browser (only its mock backend, and Python-level E2E for the services).
- Docker images could not be built exactly as committed here (ghcr.io blocked in the sandbox); scratch copies built and ran. `docker compose up` has not been run end to end.
- Branch protection is not configured (see `docs/harness.md`); CI ran on the PR and its failures were fixed on the branch (see the PR).
- No independent code review has been done (harness step 5).

## Decisions needed from the owner
1. **Agent eval shortfalls: fixed (D-021).** Participation share is within ±30% of 1/N for N = 2-6 on several seeds, and a 500-message session uses ~400k of 500k tokens without resting. These are results against the scripted fake model and a simulated clock, not a real Llama; re-check on real Ollama. The changes are recorded as D-021 in the Deep Dive: please confirm them.
2. **Amber tick with a single human:** the client draws the amber ✓ only on top of ✓✓, so a huddle with one human never shows it (spec §2 derives the ticks separately; design `Receipt` stacks them).
3. **Spec gaps resolved by the builders** (confirm or write into the spec): gateway close codes (1008/1002/1001/1011), sixth connection refused, `typ=access` JWTs; chat: PATCH agent `status` values, name 1-40 chars, `client_msg_id` must be a UUID, joins emitted as system messages; agent: definition of "engagement", name-mention matching, kill switch shows `away`, catch-up read cursor after resume; web: see R-007 and `apps/web/README.md`.
4. Not done by design: session export/ending (MVP session never closes), least-privilege Postgres role for the agent, phase 3.

## Independent review (2026-09-30) and what was done
A fresh agent reviewed the branch against the specs and found no blockers. Fixed on the branch: (1) the agent re-checks the newest messages before committing so two workers cannot post two agent messages in a row; (2) seeded `agents.config` keys now come from `libs/contracts` and a test keeps them in step with the agent's `PolicyConfig` (`max_consecutive_agent_messages` is clamped to 1); (3) chat enforces D-020 for `away` as well as paused/resting; (4) CI push triggers use `master`; (5) the agent name is restricted to a safe character set at PATCH time and sanitised again in every prompt.

Left open (owner decision): unlimited guest join (`/auth/guest` has no rate limit and joins are permanent in v1, which slowly loosens the 1/N share cap); a lost `/auth/refresh` response signs the guest out because rotation looks like reuse (a short grace for the previous token would fix it); agent `service_token` defaults to empty instead of failing fast; the resting threshold ignores safety-check and summarizer tokens; `member.left` / `session.updated` are never emitted and redaction has no write path (phase 3).

## Agent tuning from manual QA (D-022)
Two humans chatting with Huddle AI got no reply, even to "Hey huddle are you there?" and "Maybe the AI has an idea". Changes: short-name and "the AI" mentions (context-aware, so "the huddle was fun" does not count), no share cap until the window has 10 messages, a decider prompt that answers when addressed or when a question met "I don't know", and a 1,000-token decider input cap (1,500 fit worse: a 500-message session used the whole 500k window and rested). Still only tested against the scripted model: whether the real 3B decider now says yes to these cases needs another run on Ollama. If it still declines, the next lever is a deterministic rule: always answer a direct mention.

## Follow-up QA (D-023)
The agent asked Eli a question, Eli answered ("general suggestion"), and the agent stayed silent. It also wrote about Eli in the third person to Eli. Fix: answering a question the agent asked, or addressing it by name, now skips the decider (gate-level rule; "yes" counts as an answer), and the writer prompt speaks in the first person to people and builds on their answer. The deterministic part is tested; whether the real writer now reads well still needs another run on Ollama.

## Real-model decider eval
`make eval-decider` (see `docs/harness.md`) runs 35 labelled conversations through the real pipeline against Ollama and reports decider accuracy, precision and recall, per-case miss reasons, and latency. It has been run only against a stub server here (no Ollama in the build sandbox): the harness, the gate-decided cases and the output format are verified; the model numbers are not measured yet. Run it on your machine and use the misses to drive the next prompt change.

## First real-model eval (2026-10-05, llama3.2:3b on the owner's machine) and D-024
Decider accuracy 68% (precision 89%, recall 48%); every gate-decided case passed (addressed, answered, trivial, not-addressed: 100%); latency p50 0.2 s. The recall problem was mostly mechanical: the model's answer was often not valid JSON (cut off at the 50-token cap or wrapped in prose) and an unparseable answer counts as "stay quiet" (8 of 35 cases). Fix: structured output with a schema, lenient parsing, and the raw answer shown by the eval. Two real judgement misses remained (a wrong fact: "No fact-checking needed here"; a question another person had already answered): the decider prompt now states when to speak and when not to. Re-run `make eval-decider` to measure. Cost note: the longer prompt brings the worst 500-message eval seed to ~495k of 500k tokens (no resting); if resting shows up in real use, raise the budget capacity (it is a tunable).
