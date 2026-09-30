# Receipt

The delivery marks beside your own messages. Only your messages get one. It has four states, matching the delivery model in the PRD and Deep Dive:

| `status` | Mark | Meaning |
| --- | --- | --- |
| `sending` | clock | Your client has sent it; the server hasn't confirmed yet. |
| `stored` | ✓ | The server has saved it. |
| `delivered` | ✓✓ | At least one other person has received it. |
| `agent-read` | ✓✓ + amber ✓ | The AI has read it, so the agent's read cursor has passed this message. |

- The first two ticks are `ink-muted`. The third is `amber`, because amber means the AI and only the AI.
- The third tick means the AI has *received and read* the message. It does not mean the AI will answer. Say "Read by Huddle AI", not "seen", in the accessible label (`agentName` sets the name).
- If the AI is paused, resting or away, messages stop at ✓✓ until it catches up. Don't fake the third tick.
- A message that fails to send shows no receipt. `MessageBubble` shows the error and a Retry action instead.
- Every state has an `aria-label` and a tooltip, so it never relies on colour alone.
- `MessageBubble` renders `Receipt` for you when `from="self"` and `status` is set. Use it directly only in custom layouts.
