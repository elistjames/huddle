# BudgetMeter

How much the AI can still say. The budget is a token bucket that refills continuously.

- Shows "312k of 500k tokens" and a bar. The fill is `amber` because it is the AI's budget. It's a `meter` role with a text value, so the number is always there for screen readers and anyone who can't see the colour.
- When the bucket is empty (`resting`) the note changes to "Resting until 7:46 PM"; pass `refillsAt` (a Date).
- Otherwise the note reads "Refills continuously".
- Props: `remaining`, `capacity`, `refillsAt`, `resting`, `name`.
- Show it in the details panel, not the thread. The thread only says "resting · back at {time}".
