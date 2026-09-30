# Button

- `variant="primary"`: `ink` fill, for the one main action on a screen (Send, Join).
- `variant="quiet"`: `paper-raised` with a `line-strong` border, for secondary actions (Pause, Resume, Export).
- `variant="ghost"`: text only, for tertiary actions (Rename, Cancel, Load earlier) and icon-only buttons.
- `size`: `md` (36px) or `sm` (32px).
- `icon` adds a Lucide icon. With no children it makes an icon-only button, which needs `label` for its accessible name.
- `block` makes it full width.
- Focus shows a 2px `focus` (blue) ring. Buttons are never amber; amber is for the AI.
