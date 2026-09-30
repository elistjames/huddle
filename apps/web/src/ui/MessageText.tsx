import { parseBody } from "../core/text";

/**
 * A message body as plain text, inline code and code blocks (Deep Dive §8). Every segment is a
 * React text child, so any markup in the body is shown literally, never parsed as HTML.
 */
export function MessageText({ body }: { body: string }) {
  const segments = parseBody(body);
  return (
    <>
      {segments.map((s, i) => {
        if (s.kind === "block") {
          return (
            <pre key={i} className="msg-pre" data-lang={s.lang || undefined}>
              <code>{s.text}</code>
            </pre>
          );
        }
        if (s.kind === "code") {
          return (
            <code key={i} className="msg-code">
              {s.text}
            </code>
          );
        }
        return <span key={i}>{s.text}</span>;
      })}
    </>
  );
}
