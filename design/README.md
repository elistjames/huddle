# Huddle

People and AI in one conversation. Huddle is a real-time group chat where teammates talk to each other the way they always do, and an AI agent sits in the same thread, reads the same context, and speaks up only when it has something worth adding.

The design has one job: keep the conversation calm and readable, and make it obvious at a glance which voice is the AI's, without making the AI the loudest thing on screen.

> Status: second draft. Reconciled with the updated PRD, Technical Design Document and Deep Dive in Notion. There is no Figma file, so the values here are proposals to refine. Where the specs are silent, the copy and layout here are proposals and are marked as such.

## Scope

The design system covers v1 and prepares for phase 3. Components exist only for things the app will actually do.

- **v1:** one huddle opened from a link. Guest join with a name. Live chat with one AI agent. Three-tick message receipts. AI states (following, writing, paused, resting, away). AI budget. Pause, resume and rename the AI. Connection states. Export when a huddle ends.
- **Phase 3:** multiple sessions and the huddle list (`SessionRow`), invites, leaving a huddle, sign-up. Built as components but not used in v1 screens.
- **Out of scope:** attachments, reactions, edit and delete, @mentions, human typing indicators, presence dots, push and email notifications. No components, no icons in use.

## Principles

1. **Quiet by default.** Paper-toned neutrals, hairlines instead of shadows, one weight of emphasis. The chat content is the interface.
2. **Amber means AI, and only AI.** The agent is the only thing that gets colour. Its avatar, name, bubbles, "writing" state, budget and the third message tick use `amber`, `amber-soft` and `amber-ink`. Nothing else in the product uses amber: no marketing highlights, no warnings, no focus rings. That's why `focus` is blue.
3. **An equal participant.** The PRD's goal is for the agent to write about 1/N of the messages. Its bubbles are the same size, shape and type as everyone else's. Only the tint and the name set it apart. No sparkles, gradients or glow.
4. **Ink is you.** Your own messages sit on an `ink` fill with `on-ink` text, aligned right. Everyone else, human or agent, sits on the left.
5. **Say the state.** Every state in the UI states table (loading, empty, connecting, reconnecting, failed, paused, resting, away, ended) has a visible, worded treatment. Nothing relies on colour alone.

## Message receipts

Your messages carry a receipt that follows the delivery model in the PRD and Deep Dive:

| Status | Mark | Meaning |
| --- | --- | --- |
| `sending` | clock | Sent by your client, not yet confirmed. |
| `stored` | ✓ | The server has saved it. |
| `delivered` | ✓✓ | At least one other person has received it. |
| `agent-read` | ✓✓ + amber ✓ | The AI has read it. |

The third tick is the AI's and is amber for that reason. It means the agent's read cursor has passed the message, not that the AI will reply. When the AI is paused, resting or away, messages stay at ✓✓ until it catches up. A message that fails shows an error under the bubble and a Retry action when retrying can help.

## Voice

Plain, lowercase-friendly and brief, the way people write in chat. The UI speaks in short sentences and never oversells the AI.

- Agent label: **Huddle AI** (or the agent's configured name), never "Assistant" or "Bot".
- AI states: "Huddle AI is following along", "is writing", "is paused", "is away right now", "is resting · back at 7:46 PM". Never "typing".
- Empty huddle: "Nobody's said anything yet." then "Start the huddle."
- Join: "No account needed. This is the name others will see."
- Errors, under the failed bubble: "Didn't send" and "Sending too fast" (both with Retry), "Too long to send (4,000 characters max)", "You're not in this huddle", "This huddle has ended".
- System notes: "Maya Chen paused Huddle AI", "Eli renamed the AI to Sage".

Proposals: the exact wording of the AI states, error lines and system notes isn't in the spec. Confirm or change it before build.

## Colour

| Role | Tokens | Rule |
| --- | --- | --- |
| Grounds | `paper`, `paper-raised`, `paper-sunken` | Thread on `paper`; other people's bubbles, composer and panels on `paper-raised`; idle avatars and skeletons on `paper-sunken`. |
| Lines | `line`, `line-strong` | `line` for dividers and bubble borders; `line-strong` for input and control borders (3:1). |
| Text | `ink`, `ink-muted` | `ink` for message text; `ink-muted` for timestamps, names and placeholders. Both pass 4.5:1 on every ground in both themes. |
| You | `ink` fill + `on-ink` text | Your own bubbles and the primary button. |
| AI | `amber`, `amber-soft`, `amber-ink`, `on-amber` | Agent bubble on `amber-soft` with `ink` body text and the sender name in `amber-ink`. Avatar is an `amber` disc with an `on-amber` initial. Third receipt tick and budget fill in `amber`. |
| Status | `success`, `danger` | Connection dot and send errors. Use as text on `paper` or `paper-raised`. |

Contrast notes: in light mode, `amber` as text is 4.6:1 on `paper` but only 4.3:1 on `paper-sunken`. On sunken or tinted grounds use `amber-ink`. `success` is 4.4:1 on `paper-sunken`; the connection dot is a graphic, not text, and always has a label.

## Type

Inter throughout (Google Fonts, weights 400/500/600), with the system mono stack for code in messages.

- `body` 15/22: every message. Keep it at 15px so long agent replies stay easy to scan.
- `sender` 13/18 semibold: the name above a message group, in `ink` (or `amber-ink` for the agent).
- `caption` 13/18: timestamps and metadata in `ink-muted`.
- `heading` 15/22 semibold, `title` 20/28, `display` 28/34: panel headers, huddle names, empty states.
- `micro` 11/16: section labels and counts.
- `code` 13/20 mono: inline and block code inside messages.

## Space and shape

A 4px base: `space-1` 4, `space-2` 8, `space-3` 12, `space-4` 16, `space-6` 24, `space-8` 32.

- Bubble: `space-2` × `space-3` padding, `radius-lg` corners. The corner nearest the avatar is `radius-sm`, so a group of consecutive messages reads as one voice.
- Consecutive messages from one sender: `space-1` apart. New sender: `space-4`.
- Buttons and inputs `radius-sm`; composer, cards and panels `radius-md`; avatars `radius-full`.
- Depth comes from `line`. `shadow-pop` is only for menus and popovers.

## Iconography

Huddle uses Lucide outline icons (ISC licence). 28 are in the Icons asset group and in the `Icon` component; 14 are used in v1 and the rest are reserved for features the spec doesn't include yet. Use them at 20px with a 1.5px stroke (16px with a 2px stroke inside small buttons), coloured `ink-muted` at rest and `ink` when active. The AI is marked with its amber avatar, never with a sparkle or robot icon. Icons are never amber. Receipts are drawn by `Receipt`, not by icons.

## Logo

The mark is an `amber-soft` rounded square with a large `amber` disc and three small dots: a speaker and the "…" of a message on its way, which also reads as a friendly robot face. It's people and an AI in one conversation, in one shape.

- **Lockup:** the mark plus "huddle" in Inter semibold, lowercase, `ink`, tracked -0.02em. Use the `Logo` component.
- **Soft** (amber on amber-soft) is the default inside the product. **Solid** (amber-soft on amber) is the app icon, and the version to use at 16–24px.
- Leave clear space of a quarter of the mark's size on every side. Never recolour it outside the amber tokens, rotate it, or add effects.
- The mark stands for the brand, not the agent. In a chat, the AI is still shown by its `Avatar`.
- SVG files (light, dark, app icon) are in the Logos asset group.

## Components

React 18 components, exposed as `window.Huddle` (class prefix `hd-`). Each one has its own guidelines page.

- **Chat:** `MessageGroup`, `MessageBubble`, `Receipt`, `AgentIndicator`, `SystemNote`, `Composer`, `EmptyState`, `ThreadSkeleton`
- **Session:** `ThreadHeader`, `SessionDetails`, `BudgetMeter`, `JoinForm`
- **Identity:** `Logo`, `Avatar` (person or agent, with an idle style), `Icon` (the Lucide set)
- **Actions:** `Button` (primary, quiet, ghost, with an optional icon)
- **Phase 3:** `SessionRow`
- **Examples:** `HuddleScreen`, the whole v1 flow with a state switcher

A thread is a column of `MessageGroup`s on `paper`, with `space-6` gutters. `SystemNote`s sit between groups, and the `AgentIndicator` and `Composer` are pinned at the bottom under the `ThreadHeader`.

## UI states

| State | What shows |
| --- | --- |
| Join | `JoinForm` |
| Loading history | Connecting dot, `ThreadSkeleton`, locked composer ("Connecting…") |
| Empty | `EmptyState` |
| Live | Connected dot, messages with receipts, `AgentIndicator` above the composer |
| Reconnecting | Hollow dot, notice bar, locked composer ("Reconnecting…") |
| Send failed | Dashed `danger` bubble border, error line, Retry when it can help |
| AI paused | Header and indicator say "paused"; Resume in details |
| AI resting | "resting · back at {time}"; budget meter shows the empty bucket |
| AI away | "away right now" |
| Ended | "Ended" in the header; composer replaced by "This huddle has ended" and Export |

## Open questions

- Should a paused, resting or away agent still advance its read cursor? The design shows the third tick only once the cursor moves, so messages sent during those states stay at ✓✓ until the agent catches up.
- The AI state, error and system-note wording above is a proposal.
- Link the Figma file from the PRD if one is ever made.
