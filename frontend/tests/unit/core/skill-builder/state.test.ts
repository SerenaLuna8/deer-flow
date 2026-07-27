import { describe, expect, test } from "@rstest/core";

import {
  initialSkillBuilderFilePath,
  normalizeSkillBuilderSlug,
  reconcileSkillBuilderFileSelection,
  skillBuilderCanCommit,
  skillBuilderComposerDisabled,
  skillBuilderSlugError,
} from "@/core/skill-builder/state";
import type { SkillBuilderSession } from "@/core/skill-builder/types";

const session = {
  id: "11111111-1111-4111-8111-111111111111",
  project_id: "22222222-2222-4222-8222-222222222222",
  owner_user_id: "user-1",
  thread_id: "33333333-3333-4333-8333-333333333333",
  slug: "paper-review",
  display_name: "paper-review",
  status: "draft_ready",
  revision: 2,
  messages: [],
  active_clarification: null,
  progress: [],
  files: [],
  draft_checksum: null,
  validation: null,
  error_code: null,
  error_message: null,
  created_skill_id: null,
  created_at: "2026-07-27T07:58:00Z",
  updated_at: "2026-07-27T07:58:00Z",
} satisfies SkillBuilderSession;

const files = [
  {
    path: "SKILL.md",
    media_type: "text/markdown",
    size_bytes: 7,
    sha256: "a".repeat(64),
    encoding: "utf-8" as const,
    content: "# Skill",
  },
  {
    path: "references/guide.md",
    media_type: "text/markdown",
    size_bytes: 7,
    sha256: "b".repeat(64),
    encoding: "utf-8" as const,
    content: "# Guide",
  },
];

describe("skill builder state", () => {
  test("normalizes and validates project-local Skill slugs", () => {
    expect(normalizeSkillBuilderSlug(" Paper Review ")).toBe("paper-review");
    expect(skillBuilderSlugError("ab")).toContain("3");
    expect(skillBuilderSlugError("paper-review")).toBeNull();
  });

  test("selects SKILL.md only for the first generated package", () => {
    expect(initialSkillBuilderFilePath(files)).toBe("SKILL.md");
    expect(reconcileSkillBuilderFileSelection(null, [], files)).toBe(
      "SKILL.md",
    );
    expect(
      reconcileSkillBuilderFileSelection("SKILL.md", files, [
        ...files,
        { ...files[1], path: "scripts/run.py" },
      ]),
    ).toBe("SKILL.md");
    expect(
      reconcileSkillBuilderFileSelection("references/guide.md", files, [
        ...files,
        { ...files[1], path: "assets/template.md" },
      ]),
    ).toBe("references/guide.md");
    expect(
      reconcileSkillBuilderFileSelection(null, files, [
        ...files,
        { ...files[1], path: "scripts/generated.py" },
      ]),
    ).toBeNull();
  });

  test("locks conversation for generation, clarification, mutation or local edits", () => {
    expect(skillBuilderComposerDisabled(session, false)).toBe(false);
    expect(
      skillBuilderComposerDisabled({ ...session, status: "generating" }, false),
    ).toBe(true);
    expect(skillBuilderComposerDisabled(session, false, true)).toBe(true);
  });

  test("commits only a validation bound to the current draft checksum", () => {
    const validation = {
      draft_checksum: "c".repeat(64),
      validated_at: "2026-07-27T08:00:00Z",
      description: "Review papers",
      frontmatter: { name: "paper-review" },
      compatibility: null,
      secret_requirements: [],
      scan_decision: "allow" as const,
      scan_rule_ids: [],
      scan_summary: {},
    };
    expect(
      skillBuilderCanCommit({
        ...session,
        status: "validated",
        files,
        draft_checksum: "c".repeat(64),
        validation,
      }),
    ).toBe(true);
    expect(
      skillBuilderCanCommit({
        ...session,
        status: "validated",
        files,
        draft_checksum: "d".repeat(64),
        validation,
      }),
    ).toBe(false);
  });
});
