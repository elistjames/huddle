# JoinForm

The first screen for a guest who opens a huddle link. No account, no password.

- One field, "Your name", and a Join button that stays disabled until there's a name.
- Help text under the field: "No account needed. This is the name others will see."
- `onJoin(name)` is yours to provide. `initialName` pre-fills a returning guest.
- After joining, show the thread with the header in the Connecting state and a `ThreadSkeleton`.
