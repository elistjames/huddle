/** "10:42 AM": the same format the design components use for times (fmtTime in bundle.js). */
export function formatTime(t: string | number | Date): string {
  const d = t instanceof Date ? t : new Date(t);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** Minute bucket used to split message groups (MessageGroup README: new group when the minute changes). */
export function minuteKey(t: string | number | Date): number {
  const d = t instanceof Date ? t : new Date(t);
  const ms = d.getTime();
  return Number.isNaN(ms) ? 0 : Math.floor(ms / 60_000);
}
