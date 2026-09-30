# Build status and open items (2026-09-30)

Built to spec, all merged on the working branch: `libs/contracts`, `services/chat`, `services/gateway`, `services/agent`, `apps/web`, harness (`scripts/gate.sh`, CI, scanners). Verified: gate green; 294 Python tests with real Postgres 16 + Redis; web lint/typecheck/167 tests/build; `scripts/e2e_smoke.py` runs real chat + gateway over real stores (two guests, ordering, idempotent resend, receipt cursors, reconnect catch-up).

## Not verified
- **No real Ollama or Llama models were run.** Decider judgment, writer quality/length, safety check, real token counts, latency vs the 10 s/45 s timeouts, `num_ctx` effect and `away` detection are only tested against mocks and a scripted fake model.
- The web client has not been driven against the real gateway/chat in a browser (only its mock backend, and Python-level E2E for the services).
- Docker images could not be built exactly as committed here (ghcr.io blocked in the sandbox); scratch copies built and ran. `docker compose up` has not been run end to end.
- CI workflows have never run on GitHub; actions are not SHA-pinned; branch protection is not configured.
- No independent code review has been done (harness step 5).

## Decisions needed from the owner
1. **Agent eval shortfalls** (kept visible as strict xfails in `services/agent/tests/evals`):
   - Participation share for N = 2 reaches ~0.24 vs target 0.5 ±30% (cooldown doubling + one-message cap).
   - Cost: a 500-message session uses ~486k of 500k tokens and rests for part of it (Deep Dive §11 wants one window, never resting). Options: smaller decider context, larger capacity, fewer evaluations.
2. **Amber tick with a single human:** the client draws the amber ✓ only on top of ✓✓, so a huddle with one human never shows it (spec §2 derives the ticks separately; design `Receipt` stacks them).
3. **Spec gaps resolved by the builders** (confirm or write into the spec): gateway close codes (1008/1002/1001/1011), sixth connection refused, `typ=access` JWTs; chat: PATCH agent `status` values, name 1-40 chars, `client_msg_id` must be a UUID, joins emitted as system messages; agent: definition of "engagement", name-mention matching, kill switch shows `away`, catch-up read cursor after resume; web: see R-007 and `apps/web/README.md`.
4. Not done by design: session export/ending (MVP session never closes), least-privilege Postgres role for the agent, phase 3.
