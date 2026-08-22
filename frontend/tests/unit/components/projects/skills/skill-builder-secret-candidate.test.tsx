import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { skillFrontmatterResponseIsCurrent } from "@/components/projects/assets/skill-secret-declarations-editor";
import {
  SkillBuilderCreateSecretSuccess,
  SkillBuilderRevisionCommitSuccess,
  skillBuilderCreateCommitHref,
  skillBuilderCompletedVersionHref,
  skillBuilderCreatedSecretSetup,
  skillBuilderCreatedSecretSetupFromSession,
  skillBuilderDraftChanges,
  skillBuilderDraftMutationSnapshot,
} from "@/components/projects/skills/skill-builder-workspace";
import { I18nProvider } from "@/core/i18n/context";
import {
  skillBuilderCandidateValidationCurrent,
  skillBuilderCanCommitCandidate,
  skillBuilderCanValidateCandidate,
  skillBuilderCommitResponseSchema,
  skillBuilderFileDraftContent,
  updateSkillBuilderFileDraft,
  type SkillBuilderCommitResponse,
  type SkillBuilderFile,
  type SkillBuilderSession,
} from "@/core/skill-builder";

const NOW = "2026-08-19T08:00:00+08:00";
const SKILL_ID = "55555555-5555-4555-8555-555555555555";
const VERSION_ID = "66666666-6666-4666-8666-666666666666";
const NEWER_VERSION_ID = "77777777-7777-4777-8777-777777777777";

function file(path: string, content: string): SkillBuilderFile {
  return {
    path,
    media_type: "text/markdown",
    size_bytes: new TextEncoder().encode(content).byteLength,
    sha256: "a".repeat(64),
    encoding: "utf-8",
    content,
  };
}

function session(
  overrides: Partial<SkillBuilderSession> = {},
): SkillBuilderSession {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    project_id: "22222222-2222-4222-8222-222222222222",
    owner_user_id: "owner-1",
    thread_id: "33333333-3333-4333-8333-333333333333",
    slug: "secret-reader",
    display_name: "Secret reader",
    status: "validated",
    revision: 4,
    messages: [],
    active_clarification: null,
    progress: [],
    files: [file("SKILL.md", "---\nname: secret-reader\n---\n")],
    draft_checksum: "b".repeat(64),
    validation: {
      draft_checksum: "b".repeat(64),
      validated_at: NOW,
      description: "ok",
      frontmatter: {},
      compatibility: null,
      secret_requirements: [{ name: "API_KEY", optional: false }],
      scan_decision: "allow",
      scan_rule_ids: [],
      scan_summary: {},
    },
    error_code: null,
    error_message: null,
    created_skill_id: null,
    created_skill_version_id: null,
    session_kind: "create",
    target_skill_id: null,
    base_version_id: null,
    base_version_number: null,
    base_payload_checksum: null,
    target_skill_deleted: false,
    base_files: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function commitResponse(
  overrides: Partial<SkillBuilderCommitResponse["data"]> = {},
): SkillBuilderCommitResponse {
  return {
    data: {
      session: session({
        status: "completed",
        created_skill_id: SKILL_ID,
        created_skill_version_id: VERSION_ID,
        files: [],
      }),
      skill: {
        id: SKILL_ID,
        scope: "project",
        project_id: "22222222-2222-4222-8222-222222222222",
        slug: "secret-reader",
        display_name: "Secret reader",
        status: "suspended",
        current_version_id: VERSION_ID,
        revision: 1,
        created_by_user_id: "owner-1",
        created_at: NOW,
        updated_at: NOW,
      },
      version: null,
      ...overrides,
    },
    request_id: "request-1",
  };
}

describe("Skill Builder secret candidate drafts", () => {
  test("patches the root SKILL.md draft without depending on the selected file", () => {
    const files = [
      file("SKILL.md", "original skill"),
      file("references/notes.md", "original notes"),
    ];
    const current = { "references/notes.md": "edited notes" };

    const next = updateSkillBuilderFileDraft(
      files,
      current,
      "SKILL.md",
      "patched skill",
    );

    expect(next).toEqual({
      "references/notes.md": "edited notes",
      "SKILL.md": "patched skill",
    });
    expect(skillBuilderFileDraftContent(files, next, "SKILL.md")).toBe(
      "patched skill",
    );
    expect(
      skillBuilderFileDraftContent(files, next, "references/notes.md"),
    ).toBe("edited notes");

    const currentSession = session({
      revision: 9,
      draft_checksum: "c".repeat(64),
      files,
    });
    expect(
      skillBuilderDraftMutationSnapshot(
        currentSession,
        skillBuilderDraftChanges(files, next),
      ),
    ).toEqual({
      expectedRevision: 9,
      expectedDraftChecksum: "c".repeat(64),
      changes: [
        {
          op: "replace",
          path: "SKILL.md",
          content: "patched skill",
          media_type: "text/markdown",
        },
        {
          op: "replace",
          path: "references/notes.md",
          content: "edited notes",
          media_type: "text/markdown",
        },
      ],
    });
  });

  test("keeps a local SKILL.md draft while parse is pending or invalid and invalidates old validation", () => {
    const current = session();
    const drafts = { "SKILL.md": "invalid but preserved draft" };

    expect(
      skillBuilderCandidateValidationCurrent(current, drafts, "pending"),
    ).toBe(false);
    expect(
      skillBuilderCandidateValidationCurrent(current, drafts, "invalid"),
    ).toBe(false);
    expect(
      skillBuilderCanValidateCandidate(current, drafts, "pending", false),
    ).toBe(false);
    expect(skillBuilderCanCommitCandidate(current, drafts, "invalid")).toBe(
      false,
    );
    expect(drafts["SKILL.md"]).toBe("invalid but preserved draft");
    expect(skillBuilderCandidateValidationCurrent(current, {}, "valid")).toBe(
      true,
    );
    expect(skillBuilderCanValidateCandidate(current, {}, "valid", false)).toBe(
      true,
    );
    expect(skillBuilderCanCommitCandidate(current, {}, "valid")).toBe(true);
  });

  test("drops an out-of-order patch before it can overwrite the newer Builder draft", () => {
    const files = [file("SKILL.md", "server source")];
    const currentDrafts = { "SKILL.md": "newer local source" };
    const accepted = skillFrontmatterResponseIsCurrent({
      generation: 3,
      currentGeneration: 4,
      sourceContent: "older local source",
      currentContent: currentDrafts["SKILL.md"],
      sourceSha256: "a".repeat(64),
      responseSourceSha256: "a".repeat(64),
    });
    const next = accepted
      ? updateSkillBuilderFileDraft(
          files,
          currentDrafts,
          "SKILL.md",
          "stale patched source",
        )
      : currentDrafts;

    expect(accepted).toBe(false);
    expect(next).toEqual(currentDrafts);
  });
});

describe("Skill Builder create secret setup", () => {
  test("round-trips the durable exact created version in the session contract", () => {
    const parsed = skillBuilderCommitResponseSchema.parse(commitResponse());

    expect(parsed.data.session.created_skill_version_id).toBe(VERSION_ID);
  });

  test("rebuilds the create secret action from a refreshed durable session", () => {
    const completed = session({
      status: "completed",
      created_skill_id: SKILL_ID,
      created_skill_version_id: VERSION_ID,
      files: [],
    });

    expect(skillBuilderCreatedSecretSetupFromSession(completed)).toEqual({
      skillId: SKILL_ID,
      skillVersionId: VERSION_ID,
      requirementNames: ["API_KEY"],
    });
    expect(
      skillBuilderCompletedVersionHref("/projects/demo/skills", completed, {
        configureSecrets: true,
      }),
    ).toBe(
      `/projects/demo/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_ID}&configure_secrets=1`,
    );
  });

  test("rebuilds the exact revise Draft action from a refreshed durable session", () => {
    const completed = session({
      status: "completed",
      session_kind: "revise",
      target_skill_id: SKILL_ID,
      base_version_id: NEWER_VERSION_ID,
      base_version_number: 1,
      base_payload_checksum: "c".repeat(64),
      created_skill_id: SKILL_ID,
      created_skill_version_id: VERSION_ID,
      files: [],
    });

    expect(
      skillBuilderCompletedVersionHref("/projects/demo/skills", completed, {
        configureSecrets: false,
      }),
    ).toBe(
      `/projects/demo/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_ID}`,
    );
    expect(
      skillBuilderCompletedVersionHref("/projects/demo/skills", completed, {
        configureSecrets: true,
      }),
    ).toBe(
      `/projects/demo/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_ID}&configure_secrets=1`,
    );
    expect(skillBuilderCreatedSecretSetupFromSession(completed)).toEqual({
      skillId: SKILL_ID,
      skillVersionId: VERSION_ID,
      requirementNames: ["API_KEY"],
    });
  });

  test("derives the exact saved create version and requirements from the checked commit response", () => {
    expect(skillBuilderCreatedSecretSetup(commitResponse())).toEqual({
      skillId: SKILL_ID,
      skillVersionId: VERSION_ID,
      requirementNames: ["API_KEY"],
    });
  });

  test("routes a create without declarations to the exact committed version", () => {
    const response = commitResponse({
      session: session({
        status: "completed",
        created_skill_id: SKILL_ID,
        created_skill_version_id: VERSION_ID,
        files: [],
        validation: {
          ...session().validation!,
          secret_requirements: [],
        },
      }),
    });

    expect(
      skillBuilderCreateCommitHref("/projects/demo/skills", response),
    ).toBe(
      `/projects/demo/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_ID}`,
    );
  });

  test("fails closed when a create response has no durable exact version", () => {
    const response = commitResponse({
      session: session({
        status: "completed",
        created_skill_id: SKILL_ID,
        created_skill_version_id: null,
        files: [],
      }),
    });

    expect(
      skillBuilderCreateCommitHref("/projects/demo/skills", response),
    ).toBeNull();
  });

  test("prefers the strict exact commit version over a newer live pointer on idempotent replay", () => {
    const original = commitResponse();
    const replay = skillBuilderCommitResponseSchema.parse({
      ...original,
      data: {
        ...original.data,
        skill: {
          ...original.data.skill,
          current_version_id: NEWER_VERSION_ID,
        },
        version: {
          id: VERSION_ID,
          skill_id: SKILL_ID,
          version_number: 1,
          relation: "candidate",
          description: "Secret reader",
          frontmatter: {},
          compatibility: null,
          secret_requirements: [{ name: "API_KEY", optional: false }],
          scan_decision: "allow",
          scan_rule_ids: [],
          scan_summary: {},
          file_views: [],
          supersedes_version_id: null,
          payload_checksum: "b".repeat(64),
          revoked_at: null,
          revoked_by_user_id: null,
          revocation_reason_code: null,
          governance_status: "active",
          binding_eligible: false,
          created_by_user_id: "owner-1",
          created_at: NOW,
        },
      },
    });

    expect(skillBuilderCreatedSecretSetup(replay)).toEqual({
      skillId: SKILL_ID,
      skillVersionId: VERSION_ID,
      requirementNames: ["API_KEY"],
    });
  });

  test("does not offer create setup for a revise Draft or a Skill without declarations", () => {
    expect(
      skillBuilderCreatedSecretSetup(
        commitResponse({
          session: session({
            status: "completed",
            session_kind: "revise",
            target_skill_id: SKILL_ID,
            base_version_id: VERSION_ID,
            base_version_number: 1,
            base_payload_checksum: "c".repeat(64),
            created_skill_id: SKILL_ID,
            created_skill_version_id: VERSION_ID,
            files: [],
          }),
        }),
      ),
    ).toBeNull();
    expect(
      skillBuilderCreatedSecretSetup(
        commitResponse({
          session: session({
            status: "completed",
            created_skill_id: SKILL_ID,
            files: [],
            validation: {
              ...session().validation!,
              secret_requirements: [],
            },
          }),
        }),
      ),
    ).toBeNull();
  });

  test("renders a primary navigation action for configuring the created Skill", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <SkillBuilderCreateSecretSuccess
          requirementCount={1}
          href={`/projects/demo/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_ID}&configure_secrets=1`}
        />
      </I18nProvider>,
    );

    expect(html).toContain("1 项环境变量需要配置运行秘密");
    expect(html).toContain("配置秘密");
    expect(html).toContain(`skill_id=${SKILL_ID}`);
    expect(html).toContain(`skill_version_id=${VERSION_ID}`);
    expect(html).toContain("configure_secrets=1");
  });

  test("routes a revised Candidate Version with declarations to exact-version mappings", () => {
    const html = renderToStaticMarkup(
      <I18nProvider initialLocale="zh-CN">
        <SkillBuilderRevisionCommitSuccess
          versionNumber={2}
          secretRequirementCount={1}
          href={`/projects/demo/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_ID}&configure_secrets=1`}
        />
      </I18nProvider>,
    );

    expect(html).toContain("1 项环境变量需要配置运行秘密");
    expect(html).toContain("配置秘密");
    expect(html).toContain("configure_secrets=1");
  });
});
