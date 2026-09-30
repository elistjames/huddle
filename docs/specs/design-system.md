# Huddle Design System (spec page)

> **Source of truth for UI copy and states.** Notion: https://www.notion.so/3ea7d720d67f81d895bfec1d692d702d (last edited 2026-09-29).
> The full system (tokens, React components, icons, logos, prototype, usage rules) lives in [`design/`](../../design/). Start with [`design/README.md`](../../design/README.md) (brand book) and [`design/CLAUDE.md`](../../design/CLAUDE.md) (rules for building UI). This page only records what the Notion page adds beyond that folder.

There is no Figma file. The Notion page plus `design/` are the design source of truth. Colour, type and spacing values are a draft, open to revision.

## Principles (from Notion)

1. Quiet by default: paper-toned neutrals, thin lines instead of shadows.
2. Amber means AI, and only AI (avatar, name, messages, writing state, budget, third receipt tick). Focus rings are blue.
3. An equal participant: agent bubbles are the same size, shape and type as everyone else's; no sparkles, gradients or glow.
4. Ink is you: your messages sit on dark ink, right-aligned; everyone else on the left.
5. Say the state: every state in Deep Dive §10 has visible words; nothing relies on colour alone.
6. Only what the app does: components exist for v1 and phase 3 only.

## Scope

- **v1:** one huddle opened from a link, guest join, live chat with one AI, receipts, AI states, budget, pause/resume/rename, connection states, export when a huddle ends.
- **Phase 3:** multiple huddles and the list (`SessionRow`, built but unused in v1), invites, leaving, sign-up.
- **Not designed (out of scope):** attachments, reactions, edit/delete, @mentions, human typing indicators, presence dots, push/email notifications. Paperclip, smile, reply and bell icons are reserved only.

## Open questions (unresolved in Notion)

- [ ] Should a paused, resting or away agent still advance its read cursor? The design shows the third tick only once the cursor moves, so messages sent in those states wait at ✓✓.
- [ ] Confirm the wording of AI states, error lines and system notes. The spec does not fix it; the copy in `design/README.md` is a proposal.
- [ ] Budget meter: tokens with "Refills continuously" (as designed, matching the tokens-not-dollars rule D-016) or a percentage with a refill time?

## Next steps (from Notion)

- [ ] Review the draft colours and type with the team.
- [ ] Share the design system on Claude so the team can open it.
- [ ] Design the phase 3 screens: sign-up, creating a huddle, inviting people, the huddle list.
- [ ] Mobile layout for the thread and the details panel.
