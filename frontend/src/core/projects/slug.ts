export const PROJECT_SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/u;

export const PROJECT_SLUG_HELP =
  "仅支持小写英文字母、数字和连字符（-），长度为 3–63 位；连字符不能位于开头或结尾，也不能连续使用。";

export interface ProjectSlugCopy {
  slugRequired: string;
  slugTooShort: string;
  slugTooLong: string;
  slugInvalid: string;
}

const DEFAULT_PROJECT_SLUG_COPY: ProjectSlugCopy = {
  slugRequired: "请输入项目标识。",
  slugTooShort: "项目标识至少需要 3 个字符。",
  slugTooLong: "项目标识不能超过 63 个字符。",
  slugInvalid:
    "项目标识只能使用小写英文字母、数字和单个连字符（-），且不能以连字符开头或结尾。",
};

export function normalizeProjectSlug(value: string): string {
  return value.trim().toLocaleLowerCase();
}

export function projectSlugError(
  value: string,
  copy: ProjectSlugCopy = DEFAULT_PROJECT_SLUG_COPY,
): string | null {
  const normalized = normalizeProjectSlug(value);
  if (normalized.length === 0) return copy.slugRequired;
  if (normalized.length < 3) return copy.slugTooShort;
  if (normalized.length > 63) return copy.slugTooLong;
  if (!PROJECT_SLUG_PATTERN.test(normalized)) {
    return copy.slugInvalid;
  }
  return null;
}
