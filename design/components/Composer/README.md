# Composer

The message input at the bottom of the thread.

- `paper-raised` with a `line-strong` border and `radius-md`. When focused, it gets a 2px `focus` ring.
- Enter sends and Shift+Enter adds a new line. The field grows up to 160px tall.
- The Send button is a small primary `Button` and is disabled while the field is empty.
- There is no attach button. Attachments are out of scope for v1.
- `maxLength` defaults to 4000 characters (the server limit). A counter appears at 90% and turns `danger` at the limit; sending over the limit is blocked and the server would reject it with `MESSAGE_TOO_LONG`.

| `state` | Look |
| --- | --- |
| `ready` | Placeholder "Message the huddle". |
| `connecting` | Sunken, locked, placeholder "Connecting…". |
| `reconnecting` | Sunken, locked, placeholder "Reconnecting…". |
| `closed` | Replaced by "This huddle has ended" and an Export button (`onExport`). Override the text with `closedText`. |

- You provide: `onSend(text)`. The composer clears itself after sending.
