# Huddle design system: rules for Claude Code

Huddle is a real-time group chat where people and one AI agent share a thread. This folder is its design system. Build UI from it and keep to these rules.

## Sources of truth
Product behaviour comes from the Notion specs, not from this folder. If they disagree, the specs win and this system should be updated.

- PRD: https://www.notion.so/eb17d720d67f82968716817f4a66fc54
- TDD: https://www.notion.so/3ea7d720d67f8000962dc439ba6cab06
- Deep Dive (UI states table is in section 10): https://www.notion.so/a3e803163b2748d3a5680a0f49a555de
- Design system page: https://www.notion.so/3ea7d720d67f81d895bfec1d692d702d

## Layout of this folder
- `tokens.css`: CSS variables, light and dark. Import it once at the app root. `tokens.json` is the same data for tooling.
- `components/bundle.js` and `components/bundle.css`: the React 18 components, class prefix `hd-`. `bundle.js` is one classic script that expects `window.React` and sets `window.Huddle`. See `docs/USAGE.md` for how to use them in a bundler-based app.
- `components/index.d.ts`: the props for every component. Treat it as the API.
- `components/<Name>/README.md`: usage rules. `preview.html` lists the states, but these files rely on the design system host for tokens and React, so read them as examples rather than opening them directly.
- `prototype/huddle-prototype.html`: the whole v1 flow, working, self-contained. Open it in a browser to see the intended behaviour. `components/HuddleScreen/preview.html` is its readable source.
- `assets/`: logos and Lucide icons (ISC licence, see `assets/Icons/LICENSE.txt`).
- `README.md`: the brand book (voice, colour, type, space, receipts, UI states).

## Rules
1. **Amber means the AI, and only the AI.** Use `amber`, `amber-soft` and `amber-ink` only for the agent's avatar, name, bubbles, writing dots, budget meter and the third receipt tick. Never for warnings, highlights or focus rings. Focus is `--focus` (blue).
2. **Use tokens, never raw values.** No hard-coded colours, spacing or radii. Everything is in `tokens.css`. Both themes must work: check light and dark.
3. **Use the components.** Do not restyle bubbles, avatars or the composer. Compose `MessageGroup`, `Composer`, `ThreadHeader` and the rest. If something is missing, add a component in the same style and document it.
4. **Receipts follow the delivery model.** Use `Receipt` (via `MessageBubble` with `status`): `sending` clock, `stored` one tick, `delivered` two ticks (at least one other person has it), `agent-read` two ticks plus an amber third (the agent's read cursor passed the message). Drive them from server events, never guess. The third tick means read, not that a reply is coming.
5. **AI states come from `agent.status`.** `following`, `writing`, `paused`, `resting`, `away`, with `budget {remaining, capacity, refills_at}`. Show them with `AgentIndicator`. Agent errors never appear in the thread, only as paused, resting or away.
6. **Stay in scope.** v1 is one huddle opened from a link with guest join. Phase 3 (multiple sessions, invites, leaving, sign-up) has `SessionRow` ready but keep it out of v1 screens. Out of scope, so do not build them: attachments, reactions, edit and delete, @mentions, human typing indicators, presence dots, push or email notifications. The icons for them are reserved, not for use.
7. **Copy is plain and short.** "Huddle AI" (or its configured name), never "Assistant" or "Bot". "is following along", never "typing". The exact wording for AI states, errors and system notes is a proposal until Eli confirms it. Change it in the one place it lives (the components) and in the README.
8. **Accessibility.** Text is at least 4.5:1 in both themes. Nothing relies on colour alone: every state has words or an accessible label. Keep focus rings, `aria-live` on the agent indicator, and reduced-motion handling.
9. **Message limits.** 4,000 characters per message (`Composer` enforces and shows a counter). Server error codes: `RATE_LIMITED`, `MESSAGE_TOO_LONG`, `NOT_MEMBER`, `SESSION_CLOSED`, mapped to copy in `MessageBubble`.

## Open questions (do not decide silently)
- Does a paused, resting or away agent still advance its read cursor? Today the design shows the third tick only once the cursor moves.
- Final wording of the AI states, error lines and system notes.
- Budget meter format: tokens with "Refills continuously" (current) or a percentage with a refill time.

## When you change the design system
Update the component, its `README.md`, `index.d.ts`, the brand `README.md` and the preview together, then check light and dark. The source of the components is `bundle.js` and `bundle.css`.
