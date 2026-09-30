# ThreadHeader

The top of an open huddle: title, connection state, who's here and what the AI is doing.

- `title` is the huddle name. The dot beside it is the connection: `connected` (`success`), `connecting` and `reconnecting` (hollow, `ink-muted`). In the `closed` state the sub-line says "Ended" and the dot goes away.
- The sub-line reads "3 people and Huddle AI". When the AI is paused, resting or away it becomes the count plus the `AgentIndicator`, so you can see why it's quiet.
- The face pile on the right shows up to four people, "+N" beyond that, then the AI's avatar. Pressing it calls `onOpenDetails`.
- Props: `members` (`{ name }[]`), `agent` (`{ name, state, backAt }`), `connection`.
- v1 shows one huddle, so there is no back button, no invite button and no search.
