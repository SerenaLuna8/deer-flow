/**
 * Detach a submitted write-only value from its browser control immediately.
 *
 * The returned value may live on the active request promise stack, while the
 * control is cleared before any asynchronous request or cache invalidation can
 * begin.
 */
export function consumeWriteOnlyInput<T>(
  value: T,
  clearControl: () => void,
): T {
  clearControl();
  return value;
}
