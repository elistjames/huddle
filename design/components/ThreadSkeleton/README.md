# ThreadSkeleton

Grey placeholder bubbles while message history loads. Use it while the header shows the Connecting dot and the composer is locked.

- Five rows in a fixed order: three from others, one from you, so the shape reads as a chat.
- `paper-sunken` shapes with a slow fade that stops under `prefers-reduced-motion`.
- It is a `status` region labelled "Loading messages".
- Replace it with real messages as soon as the history arrives. Don't show it for more than a few seconds; after that show a connection error.
