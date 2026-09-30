# Using the components in an app

## Quickest: load them as globals (prototypes, static pages)
```html
<link rel="stylesheet" href="design/tokens.css">
<link rel="stylesheet" href="design/components/bundle.css">
<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="design/components/bundle.js"></script>
<div id="root"></div>
<script>
  const { MessageGroup, Composer } = window.Huddle;
  ReactDOM.createRoot(document.getElementById("root")).render(
    React.createElement(MessageGroup, { author: "Maya Chen", kind: "other", messages: ["hello"] })
  );
</script>
```

## In a bundler app (Vite, Next.js and so on)
`bundle.js` is a classic script that reads `window.React` and writes `window.Huddle`. The simplest way in a bundler app:

1. Import `design/tokens.css` and `design/components/bundle.css` at the app root.
2. Set `window.React = React` once, before importing the bundle, then `import "./design/components/bundle.js"`.
3. Read the components from `window.Huddle`, and use `components/index.d.ts` for the types (its `Window` declaration is already there).

For a long-lived app, better to convert `bundle.js` into ES modules, one file per component, keeping the same props and class names. Ask Claude Code to do that in one pass and to keep `bundle.css` as is. The class prefix `hd-` and the CSS variables must not change.

## Themes
Light is the default and dark follows the OS. Set `data-theme="light"` or `"dark"` on `<html>` to force one. Do not set backgrounds on `html` from a different theme than the body.

## Reference
Open `prototype/huddle-prototype.html` in a browser. Its readable source, `components/HuddleScreen/preview.html`, shows how the components fit together: grouping messages into `MessageGroup`s, driving receipt status, passing `agent` state to `ThreadHeader`, `AgentIndicator` and `SessionDetails`.
