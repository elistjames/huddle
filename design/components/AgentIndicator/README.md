# AgentIndicator

One line that says what the AI is doing right now. It sits just above the composer and in the details panel, and appears in the header when the AI is not active.

| `state` | Copy | Look |
| --- | --- | --- |
| `following` | Huddle AI is following along | Amber avatar. The default. |
| `writing` | Huddle AI is writing | Amber avatar with three moving dots. |
| `paused` | Huddle AI is paused | Idle avatar. A person paused it. |
| `resting` | Huddle AI is resting · back at 7:46 PM | Idle avatar. The budget is spent; pass `backAt` (a Date). |
| `away` | Huddle AI is away right now | Idle avatar. The agent is disconnected. |

- "Following along", not "typing". The AI isn't a person and doesn't type.
- Idle states drop the amber fill, so a quiet AI looks quiet. Never colour a resting or away state red; nothing is wrong.
- The dots pause under `prefers-reduced-motion`.
- It is a polite live region, so screen readers hear state changes.
- `listening` is accepted as an alias for `following`.
