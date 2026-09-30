# SessionRow

**Phase 3. Not used in v1.** The v1 app opens a single huddle from a link, so there is no huddle list. This component is here so the design is ready when multiple sessions arrive.

- A button on `paper-sunken`: title, optional AI marker, time, message preview and unread count.
- `active` marks the open huddle (`paper-raised` plus `aria-current`).
- `unread` shows a `micro` count in an ink badge, "99+" above 99. Unread state isn't in the v1 spec either.
- Don't place it in v1 screens.
