import { describe, expect, test } from "@rstest/core";

import {
  SKILL_BUILDER_MAX_ATTACHMENT_BYTES,
  skillBuilderCanCommit,
  skillBuilderComposerDisabled,
  skillBuilderFilesPanelReveal,
  skillBuilderHasCandidateFiles,
  skillBuilderMergeAttachment,
  type SkillBuilderSession,
} from "@/core/skill-builder";

const NOW = "2026-08-13T08:00:00+08:00";

function session(
  overrides: Partial<SkillBuilderSession> = {},
): SkillBuilderSession {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    project_id: "22222222-2222-4222-8222-222222222222",
    owner_user_id: "owner-1",
    thread_id: "33333333-3333-4333-8333-333333333333",
    slug: "catalog-auditor",
    display_name: "Catalog auditor",
    status: "draft_ready",
    revision: 1,
    messages: [],
    active_clarification: null,
    progress: [],
    files: [
      {
        path: "SKILL.md",
        media_type: "text/markdown",
        size_bytes: 8,
        sha256: "a".repeat(64),
        encoding: "utf-8",
        content: "# Skill\n",
      },
    ],
    draft_checksum: "b".repeat(64),
    validation: {
      draft_checksum: "b".repeat(64),
      validated_at: NOW,
      description: "ok",
      frontmatter: {},
      compatibility: null,
      secret_requirements: [],
    },
    error_code: null,
    error_message: null,
    created_skill_id: null,
    session_kind: "revise",
    target_skill_id: "55555555-5555-4555-8555-555555555555",
    base_version_id: "66666666-6666-4666-8666-666666666666",
    base_version_number: 2,
    base_payload_checksum: "b".repeat(64),
    target_skill_deleted: false,
    base_files: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

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
    ).toEqual({ ok: false, error: "too_large" });

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

describe("skillBuilderComposerDisabled", () => {
  test("blocks the composer after the revision target is deleted", () => {
    expect(
      skillBuilderComposerDisabled(
        session({ target_skill_deleted: true, status: "failed" }),
        false,
      ),
    ).toBe(true);
  });

  test("keeps a failed create session conversational", () => {
    expect(
      skillBuilderComposerDisabled(
        session({
          session_kind: "create",
          target_skill_id: null,
          base_version_id: null,
          base_version_number: null,
          base_payload_checksum: null,
          status: "failed",
        }),
        false,
      ),
    ).toBe(false);
  });
});

describe("skillBuilderCanCommit", () => {
  test("allows a validated revision session to create a draft version", () => {
    expect(skillBuilderCanCommit(session({ status: "validated" }))).toBe(true);
  });

  test("rejects commit after the revision target is deleted", () => {
    expect(
      skillBuilderCanCommit(
        session({ status: "validated", target_skill_deleted: true }),
      ),
    ).toBe(false);
  });
});
