/**
 * Message bodies render as plain text and code only; never as HTML (Deep Dive §8 "Rendering").
 * This splits a body into text, inline code and fenced code blocks. The UI renders each segment
 * as a React text node inside an element, so markup in a body is shown literally.
 */
export type Segment =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "block"; text: string; lang: string };

const FENCE = /```([^\n`]*)\n?([\s\S]*?)```/g;
const INLINE = /`([^`\n]+)`/g;

function splitInline(text: string, out: Segment[]): void {
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index;
    if (at > last) out.push({ kind: "text", text: text.slice(last, at) });
    out.push({ kind: "code", text: m[1] ?? "" });
    last = at + m[0].length;
  }
  if (last < text.length) out.push({ kind: "text", text: text.slice(last) });
}

export function parseBody(body: string): Segment[] {
  const out: Segment[] = [];
  let last = 0;
  for (const m of body.matchAll(FENCE)) {
    const at = m.index;
    // A block already starts and ends on its own line, so the newline beside a fence is dropped.
    if (at > last) splitInline(body.slice(last, at).replace(/\n$/, ""), out);
    const code = (m[2] ?? "").replace(/\n$/, "");
    out.push({ kind: "block", text: code, lang: (m[1] ?? "").trim() });
    last = at + m[0].length;
    if (body[last] === "\n") last++;
  }
  if (last < body.length) splitInline(body.slice(last), out);
  return out.filter((s) => s.kind !== "text" || s.text !== "");
}
