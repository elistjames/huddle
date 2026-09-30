# MessageGroup

A run of consecutive messages from one sender: the building block of a thread.

- `kind`: `other` (a person, left), `agent` (the AI, left, amber name) or `self` (you, right, no avatar and no name).
- `messages` is a list of strings, or objects `{ text, status, errorCode, onRetry }` for your own messages so each one can carry its own receipt or error.
- The avatar sits by the last bubble. Bubbles inside a group are `space-1` apart; groups are `space-4` apart.
- `agentName` names the AI in the receipt's accessible label ("Read by Huddle AI").
- Start a new group when the sender changes or the minute changes.
