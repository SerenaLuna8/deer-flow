export const USERNAME_PATTERN = /^[A-Za-z][A-Za-z0-9_]{2,31}$/;

export function normalizeUsername(value: string): string {
  return value.trim().toLowerCase();
}

export function parseUsername(value: string): string | null {
  const normalized = normalizeUsername(value);
  return USERNAME_PATTERN.test(normalized) ? normalized : null;
}
