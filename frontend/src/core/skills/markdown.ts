export interface SkillMarkdownParts {
  frontmatter: string | null;
  body: string;
}

export function splitSkillMarkdown(source: string): SkillMarkdownParts {
  const withoutBom = source.startsWith("\uFEFF") ? source.slice(1) : source;
  const match = /^---[^\S\r\n]*\r?\n([\s\S]*?)\r?\n---[^\S\r\n]*\r?\n/.exec(
    withoutBom,
  );
  if (!match) {
    return { frontmatter: null, body: source };
  }
  return {
    frontmatter: match[1] ?? "",
    body: withoutBom.slice(match[0].length),
  };
}
