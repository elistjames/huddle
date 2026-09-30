import type { ServerFrameInput } from "../contracts";
import type { SocketLike } from "../net/connection";

/** A WebSocket the test drives by hand: open it, push frames, drop it, read what was sent. */
export class FakeSocket implements SocketLike {
  static created: FakeSocket[] = [];
  static last(): FakeSocket {
    const s = FakeSocket.created[FakeSocket.created.length - 1];
    if (!s) throw new Error("no socket created");
    return s;
  }
  static reset(): void {
    FakeSocket.created = [];
  }

  readyState = 0;
  readonly sent: Record<string, unknown>[] = [];
  closedWith: number | null = null;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.created.push(this);
  }

  send(data: string): void {
    if (this.readyState !== 1) throw new Error("not open");
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(code = 1000): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.closedWith = code;
    this.onclose?.(new CloseEvent("close", { code }));
  }

  // ---- test drivers ----
  open(): void {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }

  receive(frame: ServerFrameInput & { v?: number }): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify({ v: 1, ...frame }) }));
  }

  receiveRaw(data: string): void {
    this.onmessage?.(new MessageEvent("message", { data }));
  }

  /** The server or the network drops the socket. */
  drop(code = 1006): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onclose?.(new CloseEvent("close", { code }));
  }

  types(): unknown[] {
    return this.sent.map((f) => f.type);
  }
}
