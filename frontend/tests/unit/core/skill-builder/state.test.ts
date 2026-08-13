import { describe, expect, test } from "@rstest/core";

import {
  SKILL_BUILDER_MAX_ATTACHMENT_BYTES,
  skillBuilderFilesPanelReveal,
  skillBuilderHasCandidateFiles,
  skillBuilderMergeAttachment,
} from "@/core/skill-builder";

describe("skillBuilderFilesPanelReveal", () => {
  test("stays closed while the interview has no files", () => {
    expect(skillBuilderFilesPanelReveal(false, 0)).toBe("close");
  });

  test("opens when candidate files first appear", () => {
    expect(skillBuilderFilesPanelReveal(false, 3)).toBe("open");
  });

  test("keeps the user's closed panel closed while files remain", () => {
    expect(skillBuilderFilesPanelReveal(true, 3)).toBe("keep");
  });

  test("closes when candidate files are cleared", () => {
    expect(skillBuilderFilesPanelReveal(true, 0)).toBe("close");
  });
});

describe("skillBuilderHasCandidateFiles", () => {
  test("hides the files trigger until a candidate package exists", () => {
    expect(skillBuilderHasCandidateFiles([])).toBe(false);
    expect(skillBuilderHasCandidateFiles([{ path: "SKILL.md" }])).toBe(true);
  });
});

describe("skillBuilderMergeAttachment", () => {
  test("appends a new attachment and replaces a same-name upload", () => {
    const first = skillBuilderMergeAttachment([], {
      name: "接口说明.md",
      content: "v1",
    });
    expect(first).toEqual({
      ok: true,
      attachments: [{ name: "接口说明.md", content: "v1" }],
    });
    if (!first.ok) throw new Error("unreachable");

    const replaced = skillBuilderMergeAttachment(first.attachments, {
      name: "接口说明.md",
      content: "v2",
    });
    expect(replaced).toEqual({
      ok: true,
      attachments: [{ name: "接口说明.md", content: "v2" }],
    });
  });

  test("rejects path-like names, oversized files, and too many files", () => {
    expect(
      skillBuilderMergeAttachment([], { name: "../.env", content: "x" }).ok,
    ).toBe(false);
    expect(
      skillBuilderMergeAttachment([], {
        name: "big.txt",
        content: "a".repeat(SKILL_BUILDER_MAX_ATTACHMENT_BYTES + 1),
      }),
    ).toEqual({ ok: false, error: "单个附件不能超过 256 KB。" });

    const four = ["a.md", "b.md", "c.md", "d.md"].reduce<
      { name: string; content: string }[]
    >((current, name) => {
      const merged = skillBuilderMergeAttachment(current, {
        name,
        content: "x",
      });
      if (!merged.ok) throw new Error(merged.error);
      return merged.attachments;
    }, []);
    expect(
      skillBuilderMergeAttachment(four, { name: "e.md", content: "x" }).ok,
    ).toBe(false);
  });
});
