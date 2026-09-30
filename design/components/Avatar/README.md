# Avatar

A person or the AI.

- `kind="person"`: a `paper-sunken` disc with two initials in `ink` (one initial at `size="sm"`).
- `kind="agent"`: an `amber` disc with an `on-amber` initial. This is the only amber avatar in the product.
- `size`: `md` (32px) or `sm` (24px).
- `idle`: for a paused, resting or away AI. The disc drops its amber fill for `paper-sunken` with a `line-strong` outline and an `amber-ink` initial, so the thread reads as quiet.
- The v1 spec has no presence, so avatars have no online or away dot. If presence comes later it needs a new spec and a new component.
- Every avatar has an accessible name; the AI's ends with "(AI)".
