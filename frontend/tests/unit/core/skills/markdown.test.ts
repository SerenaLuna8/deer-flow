import { describe, expect, test } from "@rstest/core";

import { splitSkillMarkdown } from "@/core/skills/markdown";

describe("splitSkillMarkdown", () => {
  test.each([
    ["---\nname: demo\n---\n# Body\n", "name: demo", "# Body\n"],
    ["---\r\nname: demo\r\n---\r\n# Body\r\n", "name: demo", "# Body\r\n"],
    ["\uFEFF---\nname: demo\n---\n# Body", "name: demo", "# Body"],
    ["---  \nname: demo\n---\t\n# Body", "name: demo", "# Body"],
  ])(
    "splits a parser-compatible leading frontmatter fence",
    (source, frontmatter, body) => {
      expect(splitSkillMarkdown(source)).toEqual({ frontmatter, body });
    },
  );

  test.each([
    "# Body\n---\nrest",
    "---\nname: demo\n# no closing fence",
    "plain text",
  ])("leaves non-frontmatter content intact", (source) => {
    expect(splitSkillMarkdown(source)).toEqual({
      frontmatter: null,
      body: source,
    });
  });
});
