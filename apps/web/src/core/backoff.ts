/** Client reconnect backoff (Deep Dive §2): jittered exponential, full jitter, base 500 ms, cap 30 s. */
export const BACKOFF_BASE_MS = 500;
export const BACKOFF_CAP_MS = 30_000;

/** The ceiling for `attempt` (0-based): min(cap, base * 2^attempt). */
export function backoffCeiling(attempt: number): number {
  const exp = Math.min(Math.max(0, attempt), 30); // 2^30 * 500 is far past the cap
  return Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** exp);
}

/** Full jitter: a uniformly random delay in [0, ceiling). `random` returns [0, 1). */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  return Math.floor(random() * backoffCeiling(attempt));
}
