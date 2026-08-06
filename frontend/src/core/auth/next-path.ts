const DEFAULT_AUTH_NEXT_PATH = "/workspace";
const CONTROL_CHARACTER_PATTERN = /[\u0000-\u001f\u007f]/u;

function isSafeAbsolutePathReference(value: string): boolean {
  return (
    value.startsWith("/") &&
    !value.startsWith("//") &&
    !value.includes("\\") &&
    !value.includes(":") &&
    !CONTROL_CHARACTER_PATTERN.test(value)
  );
}

export function safeInternalNextPath(
  next: string | null,
  fallback = DEFAULT_AUTH_NEXT_PATH,
): string {
  if (!next || !isSafeAbsolutePathReference(next)) return fallback;

  let decoded = next;
  for (let pass = 0; pass < 3; pass += 1) {
    try {
      const nextDecoded = decodeURIComponent(decoded);
      if (!isSafeAbsolutePathReference(nextDecoded)) return fallback;
      if (nextDecoded === decoded) break;
      decoded = nextDecoded;
    } catch {
      return fallback;
    }
  }
  return next;
}
