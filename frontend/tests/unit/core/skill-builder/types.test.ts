import { describe, expect, test } from "@rstest/core";

import {
  skillBuilderSessionResponseSchema,
  skillBuilderSessionSchema,
  skillBuilderTurnInputSchema,
} from "@/core/skill-builder/types";

const session = {
  id: "11111111-1111-4111-8111-111111111111",
  project_id: "22222222-2222-4222-8222-222222222222",
  owner_user_id: "user-1",
  thread_id: "33333333-3333-4333-8333-333333333333",
  slug: "paper-review",
  display_name: "paper-review",
  status: "validated",
  revision: 4,
  messages: [
    {
      id: "message-1",
      role: "assistant",
      content: "候选 Skill 已通过检查。",
      created_at: "2026-07-27T08:00:00Z",
    },
  ],
  active_clarification: null,
  progress: [
    {
      id: "draft",
      label: "生成候选文件",
      status: "completed",
    },
  ],
  files: [
    {
      path: "SKILL.md",
      media_type: "text/markdown",
      size_bytes: 42,
      sha256: "a".repeat(64),
      encoding: "utf-8",
      content: "---\nname: paper-review\n---\n\n# Paper review",
    },
    {
      path: "references/guide.md",
      media_type: "text/markdown",
      size_bytes: 7,
      sha256: "b".repeat(64),
      encoding: "utf-8",
      content: "# Guide",
    },
  ],
  draft_checksum: "c".repeat(64),
  validation: {
    draft_checksum: "c".repeat(64),
    validated_at: "2026-07-27T08:01:00Z",
    description: "Review academic papers",
    frontmatter: { name: "paper-review" },
    compatibility: ">=2.1",
    secret_requirements: [{ name: "SEARCH_TOKEN", optional: true }],
    scan_decision: "warn",
    scan_rule_ids: ["external-network"],
    scan_summary: { warnings: 1 },
  },
  error_code: null,
  error_message: null,
  created_skill_id: null,
  created_at: "2026-07-27T07:58:00Z",
  updated_at: "2026-07-27T08:01:00Z",
};

describe("skill builder contracts", () => {
  test("strictly parses a resumable validated UTF-8 candidate package", () => {
    expect(skillBuilderSessionSchema.parse(session)).toEqual(session);
    expect(
      skillBuilderSessionResponseSchema.parse({
        data: session,
        request_id: "request-1",
      }).data.files,
    ).toHaveLength(2);
  });

  test("rejects unknown private fields and non-UTF-8 candidate files", () => {
    expect(() =>
      skillBuilderSessionSchema.parse({
        ...session,
        sandbox_path: "/tmp/private",
      }),
    ).toThrow();
    expect(() =>
      skillBuilderSessionSchema.parse({
        ...session,
        files: [{ ...session.files[0], encoding: "base64" }],
      }),
    ).toThrow();
    expect(() =>
      skillBuilderSessionSchema.parse({
        ...session,
        files: [{ ...session.files[0], path: " ../private.md" }],
      }),
    ).toThrow();
  });

  test("requires checksum-bound file edits inside a draft update turn", () => {
    const input = {
      input: {
        kind: "draft_update",
        expected_draft_checksum: "c".repeat(64),
        changes: [
          {
            op: "replace",
            path: "SKILL.md",
            content: "# Updated",
            media_type: "text/markdown",
          },
          { op: "delete", path: "references/old.md" },
        ],
      },
      expected_revision: 4,
      idempotency_key: "draft-key",
    };
    expect(skillBuilderTurnInputSchema.parse(input)).toEqual(input);
    expect(() =>
      skillBuilderTurnInputSchema.parse({
        ...input,
        input: { ...input.input, expected_draft_checksum: undefined },
      }),
    ).toThrow();
  });
});
