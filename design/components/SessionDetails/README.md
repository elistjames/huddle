# SessionDetails

The side panel behind the face pile. It holds everything you can do to the AI or learn about the huddle.

- **AI:** the `AgentIndicator`, plus Pause (when the AI is following or writing), Resume (when paused) and Rename. Rename opens an inline field with Save and Cancel. Each action adds a `SystemNote` to the thread.
- **Budget:** the `BudgetMeter`, if the agent has one.
- **People:** everyone in the huddle, with "you" beside yourself.
- `onPause`, `onResume`, `onRename(name)` and `onClose` are yours to provide.
- Pausing stops the AI from reading and replying; people can still talk. Resting and away can't be changed from here.
- No invite, leave or remove actions in v1; those are phase 3.
