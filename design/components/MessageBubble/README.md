# MessageBubble

A single message. Most of the time `MessageGroup` renders these for you. Use `MessageBubble` directly only for custom layouts.

- `from="other"`: `paper-raised` with a `line` border, aligned left.
- `from="agent"`: `amber-soft` background with `ink` text, aligned left. Same size, shape and type as a person's message, so the AI reads as an equal participant.
- `from="self"`: `ink` fill with `on-ink` text, aligned right, with a `Receipt` to its left.
- `tail`: the corner nearest the speaker drops to `radius-sm`. Put it on the last bubble of a run only.
- `status` (own messages only): `sending`, `stored`, `delivered` or `agent-read`. It draws the matching `Receipt`.
- `status="failed"` gives the bubble a dashed `danger` border (the text stays at full contrast) and shows an error line in `danger` under it. Pass `errorCode` to pick the copy:

| `errorCode` | Copy | Retry |
| --- | --- | --- |
| (none) | Didn't send | yes |
| `RATE_LIMITED` | Sending too fast | yes |
| `MESSAGE_TOO_LONG` | Too long to send (4,000 characters max) | no |
| `NOT_MEMBER` | You're not in this huddle | no |
| `SESSION_CLOSED` | This huddle has ended | no |

- Retry calls `onRetry`. Errors that can't be fixed by retrying don't show the button.
- Width is capped at 560px (85% on narrow screens). Text wraps and keeps line breaks.
- The v1 spec has no reactions, replies, edit, delete or attachments, so a bubble has no hover actions.
