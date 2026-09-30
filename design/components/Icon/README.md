# Icon

The Huddle icon set: 28 icons from Lucide, drawn inline so they pick up the text colour around them.

**Used in v1:** send, check, clock (receipts and "sending"), wifi-off, circle-alert (connection and error states), rotate-ccw (retry), pause and play (agent control), download (export), chevron-down (load earlier), x (close), users, settings, ellipsis.

**Reserved, not in v1:** paperclip, smile, reply, copy, trash-2, pin, bell, bell-off, search, plus, user-plus, log-out, message-square, check-check. They cover attachments, reactions, replies, edit and delete, notifications and multi-session navigation, which the PRD marks as out of scope or phase 3. Keep them out of the UI until the spec adds the feature.

- Default size is 20px with a 1.5px stroke. At 16px and below the stroke goes up to 2px so the icon stays crisp.
- Colour follows the text: `ink-muted` at rest, `ink` when hovered or active, `danger` for errors. Never amber. The AI is shown by its avatar, not an icon.
- Message receipts are not icons. Use `Receipt`, which draws one, two or three ticks (the third in amber).
- Icons are decorative by default (`aria-hidden`). Pass `label` when an icon is the only thing that explains itself, or put the label on the button with `Button`'s `icon` and `label` props.
- The names are listed in `Icon.names`. The original SVGs are in the Icons asset group.
- To add an icon, copy the exact Lucide SVG into `assets/Icons/` and add it to the bundle. Don't draw your own.
