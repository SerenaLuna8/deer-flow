import { createHash } from "node:crypto";

import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";
import type { AssetVersion, ProjectAssetItem } from "@/core/shared-assets";
import type { SkillBuilderSession } from "@/core/skill-builder";

type SkillAssetVersion = Extract<AssetVersion, { skill_id: string }>;

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const SKILL_ID = "20000000-0000-4000-8000-000000000001";
const VERSION_1_ID = "30000000-0000-4000-8000-000000000001";
const VERSION_2_ID = "30000000-0000-4000-8000-000000000002";
const CREDENTIAL_ID = "40000000-0000-4000-8000-000000000001";
const CREDENTIAL_VERSION_ID = "50000000-0000-4000-8000-000000000001";
const SESSION_ID = "60000000-0000-4000-8000-000000000001";
const THREAD_ID = "70000000-0000-4000-8000-000000000001";
const TIMESTAMP = "2026-08-19T08:00:00+08:00";
const API_TOKEN = "API_TOKEN";
const SOURCE_API_TOKEN = "TOKEN_VALUE";
const ANALYTICS_KEY = "ANALYTICS_KEY";
const BUILDER_TOKEN = "BUILDER_TOKEN";

type Requirement = { name: string; optional: boolean };

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function skillContent(
  requirements: readonly Requirement[],
  name = "browser-secret",
): string {
  const requiredSecrets = requirements
    .map(
      (requirement) =>
        `  - name: ${requirement.name}\n    optional: ${requirement.optional}`,
    )
    .join("\n");
  return `---\nname: ${name}\ndescription: Browser Skill secret coverage.\nrequired-secrets:\n${requiredSecrets}\nsecrets-autonomous: true\n---\n\n# Browser Skill\n`;
}

const initialContent = skillContent([{ name: API_TOKEN, optional: false }]);
const patchedContent = skillContent([
  { name: API_TOKEN, optional: false },
  { name: ANALYTICS_KEY, optional: true },
]);
const builderInitialContent = skillContent([], "builder-secret");
const builderPatchedContent = skillContent(
  [{ name: BUILDER_TOKEN, optional: false }],
  "builder-secret",
);

const project: Project = {
  id: PROJECT_ID,
  slug: "alpha",
  display_name: "Alpha Project",
  description: "Skill secret browser coverage",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "private_work.read_own",
    "private_work.create",
    "shared_assets.read",
    "shared_assets.edit",
    "shared_assets.execute",
    "shared_assets.manage_bindings",
    "mcp.credentials.approve",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 1,
  agent_count: 0,
  skill_count: 1,
  mcp_count: 0,
  quota_summary: {
    members: { used: 1, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-alpha",
};

function projectSkill(
  assetVersion: number,
  currentPublishedVersionId: string = VERSION_1_ID,
): ProjectAssetItem {
  return {
    id: SKILL_ID,
    scope: "project",
    project_id: PROJECT_ID,
    slug: "browser-secret",
    display_name: "Browser Secret",
    description: "Browser Skill secret coverage.",
    status: "suspended",
    current_published_version_id: currentPublishedVersionId,
    version: assetVersion,
    capabilities: [
      "shared_assets.read",
      "shared_assets.edit",
      "shared_assets.manage_bindings",
    ],
    binding: null,
    created_by_user_id: ACCOUNT_ID,
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
  };
}

function importedSkillItem(item: ProjectAssetItem) {
  return {
    id: item.id,
    scope: item.scope,
    project_id: item.project_id,
    slug: item.slug,
    display_name: item.display_name,
    status: item.status,
    current_published_version_id: item.current_published_version_id,
    version: item.version,
    created_by_user_id: item.created_by_user_id,
    created_at: item.created_at,
    updated_at: item.updated_at,
  };
}

function skillVersion({
  id,
  number,
  workflow,
  content,
  requirements,
  supersedes,
}: {
  id: string;
  number: number;
  workflow: "draft" | "published";
  content: string;
  requirements: Requirement[];
  supersedes: string | null;
}): SkillAssetVersion {
  const checksum = sha256(content);
  return {
    id,
    skill_id: SKILL_ID,
    version_number: number,
    workflow_status: workflow,
    description: "Browser Skill secret coverage.",
    frontmatter: {
      name: "browser-secret",
      "required-secrets": requirements,
      "secrets-autonomous": true,
    },
    compatibility: null,
    secret_requirements: requirements,
    scan_decision: "allow",
    scan_rule_ids: [],
    scan_summary: { result: "clean" },
    file_views: [
      {
        path: "SKILL.md",
        media_type: "text/markdown",
        size_bytes: Buffer.byteLength(content, "utf8"),
        sha256: checksum,
      },
    ],
    supersedes_version_id: supersedes,
    payload_checksum: checksum,
    revoked_at: null,
    revoked_by_user_id: null,
    revocation_reason_code: null,
    governance_status: "active",
    binding_eligible: workflow === "published",
    created_by_user_id: ACCOUNT_ID,
    created_at: TIMESTAMP,
  };
}

const publishedV1 = skillVersion({
  id: VERSION_1_ID,
  number: 1,
  workflow: "published",
  content: initialContent,
  requirements: [{ name: API_TOKEN, optional: false }],
  supersedes: null,
});

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function projectionFor(content: string) {
  const requirements: Requirement[] = [];
  for (const name of [API_TOKEN, ANALYTICS_KEY, BUILDER_TOKEN]) {
    if (content.includes(`name: ${name}`)) {
      requirements.push({ name, optional: name === ANALYTICS_KEY });
    }
  }
  return {
    required_secrets: requirements,
    secrets_autonomous: true,
    secrets_autonomous_explicit: true,
    shorthand_count: 0,
  };
}

function frontmatterParseResponse(content: string, sourceSha256: string) {
  return {
    source_sha256: sourceSha256,
    valid: true,
    patchable: true,
    projection: projectionFor(content),
    diagnostics: [],
    request_id: `parse-${sourceSha256.slice(0, 8)}`,
  };
}

type SkillMockOptions = {
  initiallyImported?: boolean;
  initialDraft?: boolean;
};

async function mockProjectSkillSecrets(
  page: Page,
  { initiallyImported = true, initialDraft = false }: SkillMockOptions = {},
) {
  const activationDraft = skillVersion({
    id: VERSION_2_ID,
    number: 2,
    workflow: "draft",
    content: initialContent,
    requirements: [{ name: API_TOKEN, optional: false }],
    supersedes: VERSION_1_ID,
  });
  let hasSkill = initiallyImported;
  let asset = projectSkill(initialDraft ? 2 : 1);
  let versions: SkillAssetVersion[] = initialDraft
    ? [activationDraft, publishedV1]
    : [publishedV1];
  const contentByVersion = new Map([
    [VERSION_1_ID, initialContent],
    [VERSION_2_ID, initialContent],
  ]);
  const patchBodies: unknown[] = [];
  const forkBodies: unknown[] = [];
  const publishBodies: unknown[] = [];
  const bindingBodies: unknown[] = [];
  const bindingsByVersion = new Map<
    string,
    Record<string, { credentialVersionId: string; sourceEnvFieldName: string }>
  >();
  const bindingRevisionByVersion = new Map<string, number>();
  const unexpectedRequests: string[] = [];

  function bindingResponse(versionId: string) {
    const version = versions.find((item) => item.id === versionId);
    const configured = bindingsByVersion.get(versionId) ?? {};
    return {
      skill_id: SKILL_ID,
      skill_version_id: versionId,
      revision: bindingRevisionByVersion.get(versionId) ?? 0,
      requirements: (version?.secret_requirements ?? []).map((requirement) => {
        const binding = configured[requirement.name];
        return {
          ...requirement,
          configured: Boolean(binding),
          mapping_status: binding ? "configured" : "missing",
          credential_id: binding ? CREDENTIAL_ID : null,
          credential_version_id: binding?.credentialVersionId ?? null,
          credential_display_name: binding ? "Browser API token" : null,
          credential_version_number: binding ? 1 : null,
          source_env_field_name: binding?.sourceEnvFieldName ?? null,
          eligible_credentials: [
            {
              credential_id: CREDENTIAL_ID,
              credential_version_id: CREDENTIAL_VERSION_ID,
              display_name: "Browser API token",
              version_number: 1,
              env_fields: [SOURCE_API_TOKEN],
            },
          ],
        };
      }),
      request_id: `request-bindings-${versionId}`,
    };
  }

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;
    const skillsBase = `/api/projects/${PROJECT_ID}/skills`;

    if (path === "/api/v1/auth/me" && method === "GET") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "owner@example.test",
        username: "owner",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status" && method === "GET") {
      return json(route, { needs_setup: false, registration_enabled: true });
    }
    if (path === "/api/projects" && method === "GET") {
      return json(route, { items: [project], next_cursor: null });
    }
    if (path === `/api/projects/${PROJECT_ID}/enter` && method === "POST") {
      return json(route, project);
    }
    if (
      path === `/api/projects/${PROJECT_ID}/private-work/readiness` &&
      method === "GET"
    ) {
      return json(route, {
        status: "ready",
        code: "READY",
        request_id: "private-work-ready",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/automations/readiness` &&
      method === "GET"
    ) {
      return json(route, {
        status: "ready",
        code: "READY",
        scheduler_enabled: true,
        scheduler_status: "running",
        project_private_work_ready: true,
        schema_ready: true,
        request_id: "automations-ready",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/skill-builder/sessions` &&
      method === "GET"
    ) {
      return json(route, { data: [], request_id: "builder-sessions" });
    }
    if (path === skillsBase && method === "GET") {
      return json(route, {
        system_items: [],
        project_items: hasSkill ? [asset] : [],
        request_id: "request-skills",
      });
    }
    if (path === `${skillsBase}/import` && method === "POST") {
      hasSkill = true;
      asset = projectSkill(1);
      versions = [publishedV1];
      return json(
        route,
        {
          item: importedSkillItem(asset),
          version: publishedV1,
          request_id: "request-import",
        },
        201,
      );
    }
    if (path === `${skillsBase}/${SKILL_ID}/activate` && method === "POST") {
      return json(
        route,
        {
          detail: {
            code: "SKILL_CREDENTIAL_BINDINGS_INCOMPLETE",
            message: "Required Skill Credential bindings are incomplete",
            request_id: "request-activate-incomplete",
          },
        },
        422,
      );
    }
    if (path === `${skillsBase}/${SKILL_ID}/versions` && method === "GET") {
      return json(route, { data: versions, request_id: "request-versions" });
    }
    const fileMatch = new RegExp(
      `^${skillsBase}/${SKILL_ID}/versions/([^/]+)/files/content$`,
      "u",
    ).exec(path);
    if (fileMatch && method === "GET") {
      const versionId = fileMatch[1] ?? "";
      const content = contentByVersion.get(versionId) ?? initialContent;
      const version = versions.find((item) => item.id === versionId);
      return json(route, {
        data: {
          path: "SKILL.md",
          media_type: "text/markdown",
          size_bytes: Buffer.byteLength(content, "utf8"),
          sha256: sha256(content),
          preview_status: "ready",
          encoding: "utf-8",
          content,
          source_payload_checksum: version?.payload_checksum ?? sha256(content),
          asset_version: asset.version,
        },
        request_id: `request-file-${versionId}`,
      });
    }
    if (path === `${skillsBase}/frontmatter/parse` && method === "POST") {
      const body = request.postDataJSON() as {
        content: string;
        source_sha256: string;
      };
      return json(
        route,
        frontmatterParseResponse(body.content, body.source_sha256),
      );
    }
    if (path === `${skillsBase}/frontmatter/patch` && method === "POST") {
      const body = request.postDataJSON() as {
        content: string;
        source_sha256: string;
        required_secrets: Requirement[];
        secrets_autonomous: boolean;
      };
      patchBodies.push(body);
      const content = skillContent(body.required_secrets);
      return json(route, {
        source_sha256: body.source_sha256,
        result_sha256: sha256(content),
        content,
        changed: content !== body.content,
        changed_fields: ["required-secrets"],
        projection: {
          required_secrets: body.required_secrets,
          secrets_autonomous: body.secrets_autonomous,
          secrets_autonomous_explicit: true,
          shorthand_count: 0,
        },
        diagnostics: [],
        request_id: "request-patch",
      });
    }
    const bindingMatch = new RegExp(
      `^${skillsBase}/${SKILL_ID}/versions/([^/]+)/credential-bindings$`,
      "u",
    ).exec(path);
    if (bindingMatch && method === "GET") {
      return json(route, bindingResponse(bindingMatch[1] ?? ""));
    }
    if (bindingMatch && method === "PUT") {
      const versionId = bindingMatch[1] ?? "";
      const body = request.postDataJSON() as {
        expected_revision: number;
        bindings: Array<{
          name: string;
          credential_version_id: string;
          source_env_field_name: string;
        }>;
      };
      bindingBodies.push(body);
      bindingsByVersion.set(
        versionId,
        Object.fromEntries(
          body.bindings.map((binding) => [
            binding.name,
            {
              credentialVersionId: binding.credential_version_id,
              sourceEnvFieldName: binding.source_env_field_name,
            },
          ]),
        ),
      );
      bindingRevisionByVersion.set(versionId, body.expected_revision + 1);
      return json(route, bindingResponse(versionId));
    }
    if (
      path === `${skillsBase}/${SKILL_ID}/versions/${VERSION_1_ID}/fork` &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as {
        changes: Array<{ path: string; content?: string }>;
      };
      forkBodies.push(body);
      const content =
        body.changes.find((change) => change.path === "SKILL.md")?.content ??
        patchedContent;
      const draftV2 = skillVersion({
        id: VERSION_2_ID,
        number: 2,
        workflow: "draft",
        content,
        requirements: [
          { name: API_TOKEN, optional: false },
          { name: ANALYTICS_KEY, optional: true },
        ],
        supersedes: VERSION_1_ID,
      });
      contentByVersion.set(VERSION_2_ID, content);
      versions = [draftV2, publishedV1];
      asset = projectSkill(2);
      return json(route, { data: draftV2, request_id: "request-fork" });
    }
    if (
      path ===
        `${skillsBase}/${SKILL_ID}/versions/${VERSION_2_ID}/publish-plan` &&
      method === "GET"
    ) {
      const draft = versions.find((version) => version.id === VERSION_2_ID);
      const configured = bindingsByVersion.get(VERSION_2_ID) ?? {};
      const apiTokenConfigured = Boolean(configured[API_TOKEN]);
      return json(route, {
        skill_id: SKILL_ID,
        skill_version_id: VERSION_2_ID,
        asset_version: asset.version,
        payload_checksum: draft?.payload_checksum ?? sha256(patchedContent),
        binding_revision: bindingRevisionByVersion.get(VERSION_2_ID) ?? 0,
        secrets_autonomous: true,
        ready: apiTokenConfigured,
        required_count: 1,
        configured_required_count: apiTokenConfigured ? 1 : 0,
        invalid_count: 0,
        requirements: [
          {
            name: API_TOKEN,
            optional: false,
            mapping_status: apiTokenConfigured ? "configured" : "missing",
          },
          {
            name: ANALYTICS_KEY,
            optional: true,
            mapping_status: configured[ANALYTICS_KEY]
              ? "configured"
              : "missing",
          },
        ],
        request_id: "request-publish-plan",
      });
    }
    if (
      path === `${skillsBase}/${SKILL_ID}/versions/${VERSION_2_ID}/publish` &&
      method === "POST"
    ) {
      publishBodies.push(request.postDataJSON());
      const draft = versions.find((version) => version.id === VERSION_2_ID)!;
      const publishedV2 = {
        ...draft,
        workflow_status: "published" as const,
        binding_eligible: true,
      };
      versions = [publishedV2, publishedV1];
      asset = projectSkill(3, VERSION_2_ID);
      return json(route, {
        data: publishedV2,
        request_id: "request-publish",
      });
    }

    unexpectedRequests.push(`${method} ${path}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return {
    patchBodies,
    forkBodies,
    bindingBodies,
    publishBodies,
    unexpectedRequests,
  };
}

function builderFile(content: string) {
  return {
    path: "SKILL.md",
    media_type: "text/markdown",
    size_bytes: Buffer.byteLength(content, "utf8"),
    sha256: sha256(content),
    encoding: "utf-8" as const,
    content,
  };
}

function builderSession({
  content,
  checksum,
  revision,
  status,
  validated,
  completed = false,
}: {
  content: string;
  checksum: string;
  revision: number;
  status: "draft_ready" | "validated" | "completed";
  validated: boolean;
  completed?: boolean;
}): SkillBuilderSession {
  const requirements = projectionFor(content).required_secrets;
  return {
    id: SESSION_ID,
    project_id: PROJECT_ID,
    owner_user_id: ACCOUNT_ID,
    thread_id: THREAD_ID,
    slug: "builder-secret",
    display_name: "Builder Secret",
    status,
    revision,
    messages: [],
    active_clarification: null,
    progress: [],
    files: [builderFile(content)],
    draft_checksum: checksum,
    validation: validated
      ? {
          draft_checksum: checksum,
          validated_at: TIMESTAMP,
          description: "Candidate declarations are valid.",
          frontmatter: { name: "builder-secret" },
          compatibility: null,
          secret_requirements: requirements,
          scan_decision: "allow",
          scan_rule_ids: [],
          scan_summary: { result: "clean" },
        }
      : null,
    error_code: null,
    error_message: null,
    created_skill_id: completed ? SKILL_ID : null,
    session_kind: "create",
    target_skill_id: null,
    base_version_id: null,
    base_version_number: null,
    base_payload_checksum: null,
    target_skill_deleted: false,
    base_files: [],
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
  };
}

async function mockBuilderSecretCandidate(page: Page) {
  const initialChecksum = "a".repeat(64);
  const savedChecksum = sha256(builderPatchedContent);
  let session = builderSession({
    content: builderInitialContent,
    checksum: initialChecksum,
    revision: 1,
    status: "validated",
    validated: true,
  });
  const patchBodies: unknown[] = [];
  const draftBodies: unknown[] = [];
  const validateBodies: unknown[] = [];
  const commitBodies: unknown[] = [];
  const unexpectedRequests: string[] = [];
  const sessionsBase = `/api/projects/${PROJECT_ID}/skill-builder/sessions`;
  const skillsBase = `/api/projects/${PROJECT_ID}/skills`;

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/v1/auth/me" && method === "GET") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "owner@example.test",
        username: "owner",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status" && method === "GET") {
      return json(route, { needs_setup: false, registration_enabled: true });
    }
    if (path === "/api/projects" && method === "GET") {
      return json(route, { items: [project], next_cursor: null });
    }
    if (path === `/api/projects/${PROJECT_ID}/enter` && method === "POST") {
      return json(route, project);
    }
    if (
      path === `/api/projects/${PROJECT_ID}/private-work/readiness` &&
      method === "GET"
    ) {
      return json(route, {
        status: "ready",
        code: "READY",
        request_id: "private-work-ready",
      });
    }
    if (
      path === `/api/projects/${PROJECT_ID}/automations/readiness` &&
      method === "GET"
    ) {
      return json(route, {
        status: "ready",
        code: "READY",
        scheduler_enabled: true,
        scheduler_status: "running",
        project_private_work_ready: true,
        schema_ready: true,
        request_id: "automations-ready",
      });
    }
    if (path === "/api/models" && method === "GET") {
      return json(route, {
        models: [
          {
            name: "mock-model",
            model: "mock-model",
            display_name: "Mock model",
            supports_thinking: false,
            supports_reasoning_effort: false,
            supports_vision: false,
            supports_vision_bridge: false,
            is_default: true,
          },
        ],
        token_usage: { enabled: false },
      });
    }
    if (path === sessionsBase && method === "GET") {
      return json(route, {
        data: [
          {
            id: SESSION_ID,
            slug: session.slug,
            display_name: session.display_name,
            status: session.status,
            revision: session.revision,
            updated_at: TIMESTAMP,
            session_kind: "create",
          },
        ],
        request_id: "request-builder-sessions",
      });
    }
    if (path === `${sessionsBase}/${SESSION_ID}` && method === "GET") {
      return json(route, {
        data: session,
        request_id: `request-session-${session.revision}`,
      });
    }
    if (path === `${skillsBase}/frontmatter/parse` && method === "POST") {
      const body = request.postDataJSON() as {
        content: string;
        source_sha256: string;
      };
      return json(
        route,
        frontmatterParseResponse(body.content, body.source_sha256),
      );
    }
    if (path === `${skillsBase}/frontmatter/patch` && method === "POST") {
      const body = request.postDataJSON() as {
        content: string;
        source_sha256: string;
        required_secrets: Requirement[];
        secrets_autonomous: boolean;
      };
      patchBodies.push(body);
      const content = skillContent(body.required_secrets, "builder-secret");
      return json(route, {
        source_sha256: body.source_sha256,
        result_sha256: sha256(content),
        content,
        changed: content !== body.content,
        changed_fields: ["required-secrets"],
        projection: {
          required_secrets: body.required_secrets,
          secrets_autonomous: body.secrets_autonomous,
          secrets_autonomous_explicit: true,
          shorthand_count: 0,
        },
        diagnostics: [],
        request_id: "request-builder-patch",
      });
    }
    if (path === `${sessionsBase}/${SESSION_ID}/turns` && method === "POST") {
      const body = request.postDataJSON() as {
        input: {
          kind: string;
          changes?: Array<{ path: string; content?: string }>;
        };
      };
      draftBodies.push(body);
      const content =
        body.input.changes?.find((change) => change.path === "SKILL.md")
          ?.content ?? builderPatchedContent;
      session = builderSession({
        content,
        checksum: savedChecksum,
        revision: 2,
        status: "draft_ready",
        validated: false,
      });
      return json(route, {
        data: session,
        request_id: "request-builder-draft",
      });
    }
    if (
      path === `${sessionsBase}/${SESSION_ID}/validate` &&
      method === "POST"
    ) {
      validateBodies.push(request.postDataJSON());
      session = builderSession({
        content: builderPatchedContent,
        checksum: savedChecksum,
        revision: 3,
        status: "validated",
        validated: true,
      });
      return json(route, {
        data: session,
        request_id: "request-builder-validate",
      });
    }
    if (path === `${sessionsBase}/${SESSION_ID}/commit` && method === "POST") {
      commitBodies.push(request.postDataJSON());
      session = builderSession({
        content: builderPatchedContent,
        checksum: savedChecksum,
        revision: 4,
        status: "completed",
        validated: true,
        completed: true,
      });
      const item = importedSkillItem(projectSkill(1));
      const version = skillVersion({
        id: VERSION_1_ID,
        number: 1,
        workflow: "published",
        content: builderPatchedContent,
        requirements: [{ name: BUILDER_TOKEN, optional: false }],
        supersedes: null,
      });
      version.payload_checksum = savedChecksum;
      return json(route, {
        data: { session, skill: item, version },
        request_id: "request-builder-commit",
      });
    }

    unexpectedRequests.push(`${method} ${path}${url.search}`);
    return json(route, { detail: "unexpected browser-test request" }, 599);
  });

  return {
    patchBodies,
    draftBodies,
    validateBodies,
    commitBodies,
    unexpectedRequests,
  };
}

test("archive required-secrets guides the user to Credential configuration", async ({
  page,
}) => {
  const api = await mockProjectSkillSecrets(page, {
    initiallyImported: false,
  });

  await page.goto("/projects/alpha/skills");
  await page.getByRole("button", { name: "新建 Skill" }).click();
  await page.getByRole("menuitem", { name: "上传压缩包" }).click();
  await page.getByLabel("Skill 压缩包").setInputFiles({
    name: "browser-secret.skill",
    mimeType: "application/zip",
    buffer: Buffer.from("deterministic-mocked-archive"),
  });
  await page.getByRole("button", { name: "上传并创建" }).click();

  await expect(page).toHaveURL(
    `/projects/alpha/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_1_ID}&configure_credentials=1`,
  );
  const detail = page.getByRole("dialog", { name: "Browser Secret" });
  await expect(detail).toBeVisible();
  const runtimeCredentialsTab = detail.getByRole("tab", {
    name: "Runtime credentials",
  });
  await expect(runtimeCredentialsTab).toHaveAttribute("aria-selected", "true");
  const bindingHeading = detail.getByRole("heading", {
    name: "2. Project Credential mappings",
  });
  await expect(bindingHeading).toBeVisible();
  await expect(
    detail.getByText("Map each Skill environment variable", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    detail.getByRole("option", { name: "Select a Credential" }),
  ).toBeAttached();
  const bindingSelect = detail.getByLabel(`Project Credential · ${API_TOKEN}`);
  await bindingSelect.selectOption(CREDENTIAL_VERSION_ID);
  const sourceSelect = detail.getByLabel(
    `Source environment variable · ${API_TOKEN}`,
  );
  await sourceSelect.selectOption(SOURCE_API_TOKEN);
  await detail.getByRole("button", { name: "Save mappings" }).click();
  await detail.getByRole("tab", { name: "Files" }).click();
  await expect(bindingHeading).toBeHidden();
  await runtimeCredentialsTab.click();
  await expect(bindingSelect).toHaveValue(CREDENTIAL_VERSION_ID);
  await expect(sourceSelect).toHaveValue(SOURCE_API_TOKEN);
  expect(api.bindingBodies).toEqual([
    {
      expected_revision: 0,
      bindings: [
        {
          name: API_TOKEN,
          credential_version_id: CREDENTIAL_VERSION_ID,
          source_env_field_name: SOURCE_API_TOKEN,
        },
      ],
    },
  ]);
  expect(api.unexpectedRequests).toEqual([]);
});

test("version form saves exact source-field mappings before read-only publish", async ({
  page,
}) => {
  const api = await mockProjectSkillSecrets(page);

  await page.goto(`/projects/alpha/skills?skill_id=${SKILL_ID}`);
  const detail = page.getByRole("dialog", { name: "Browser Secret" });
  await expect(detail).toBeVisible();
  await detail.getByRole("button", { name: "创建新版本" }).click();
  const editingStatus = detail.getByRole("region", {
    name: "版本编辑状态",
  });
  await expect(
    editingStatus.getByRole("button", { name: "放弃修改" }),
  ).toBeVisible();
  await expect(
    editingStatus.getByRole("button", { name: "保存为新版本" }),
  ).toBeVisible();
  await detail.getByRole("tab", { name: "Runtime credentials" }).click();
  await expect(
    detail.getByRole("heading", {
      name: "1. Environment variable declarations",
    }),
  ).toBeVisible();
  await expect(
    detail.getByText("当前正在编辑 SKILL.md。", { exact: false }),
  ).toBeVisible();
  await expect(
    detail.getByLabel(new RegExp("Project Credential", "u")),
  ).toHaveCount(0);

  await detail.getByLabel("Variable name").fill(ANALYTICS_KEY);
  await detail.getByLabel("Make the new variable optional").click();
  await detail.getByRole("button", { name: "Add" }).click();
  await expect(detail.getByText(ANALYTICS_KEY, { exact: true })).toBeVisible();

  await detail.getByRole("button", { name: "View SKILL.md" }).click();
  const source = detail.getByLabel("编辑 SKILL.md");
  await expect(source).toHaveValue(/required-secrets:/u);
  await expect(source).toHaveValue(new RegExp(`name: ${ANALYTICS_KEY}`, "u"));

  await detail.getByRole("button", { name: "保存为新版本" }).click();
  await expect(
    detail.getByRole("tab", { name: "Runtime credentials" }),
  ).toHaveAttribute("aria-selected", "true");
  await detail
    .getByLabel(`Project Credential · ${API_TOKEN}`)
    .selectOption(CREDENTIAL_VERSION_ID);
  await detail
    .getByLabel(`Source environment variable · ${API_TOKEN}`)
    .selectOption(SOURCE_API_TOKEN);
  const saveMappings = detail.getByRole("button", { name: "Save mappings" });
  await saveMappings.click();
  const advancedSettings = detail.getByText("Advanced settings", {
    exact: true,
  });
  await expect(saveMappings).toBeVisible();
  await expect(advancedSettings).toBeVisible();
  const [saveMappingsBox, advancedSettingsBox] = await Promise.all([
    saveMappings.boundingBox(),
    advancedSettings.boundingBox(),
  ]);
  if (!saveMappingsBox || !advancedSettingsBox) {
    throw new Error("Credential mapping and advanced settings must be visible");
  }
  expect(advancedSettingsBox.y).toBeGreaterThan(
    saveMappingsBox.y + saveMappingsBox.height,
  );
  await expect(detail.getByRole("button", { name: "发布版本" })).toBeEnabled();
  await detail.getByRole("button", { name: "发布版本" }).click();

  const publishDialog = page.getByRole("dialog", {
    name: "Publish Skill version",
  });
  await expect(publishDialog).toBeVisible();
  await expect(
    publishDialog.getByLabel(new RegExp("Project Credential", "u")),
  ).toHaveCount(0);
  await expect(publishDialog.getByText("Publish checks passed")).toBeVisible();
  await publishDialog.getByRole("button", { name: "Publish version" }).click();

  await expect(publishDialog).toHaveCount(0);
  expect(api.patchBodies).toHaveLength(1);
  expect(api.forkBodies).toHaveLength(1);
  expect(api.bindingBodies).toEqual([
    {
      expected_revision: 0,
      bindings: [
        {
          name: API_TOKEN,
          credential_version_id: CREDENTIAL_VERSION_ID,
          source_env_field_name: SOURCE_API_TOKEN,
        },
      ],
    },
  ]);
  expect(api.forkBodies[0]).toMatchObject({
    changes: [
      {
        op: "replace",
        path: "SKILL.md",
        content: patchedContent,
        media_type: "text/markdown",
      },
    ],
  });
  expect(api.publishBodies).toEqual([
    {
      expected_asset_version: 2,
      expected_payload_checksum: sha256(patchedContent),
      expected_binding_revision: 1,
      acknowledge_stale_base: false,
    },
  ]);
  expect(JSON.stringify(api.publishBodies)).not.toMatch(
    /token-value|plaintext|ciphertext/iu,
  );
  expect(api.unexpectedRequests).toEqual([]);
});

test("failed activation leaves a Draft for the exact published Credential section", async ({
  page,
}) => {
  const api = await mockProjectSkillSecrets(page, { initialDraft: true });

  await page.goto(
    `/projects/alpha/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_2_ID}`,
  );
  const detail = page.getByRole("dialog", { name: "Browser Secret" });
  await expect(detail).toBeVisible();
  await detail.getByRole("switch", { name: "启用 Browser Secret" }).click();

  await expect(page).toHaveURL(
    `/projects/alpha/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_1_ID}&configure_credentials=1`,
  );
  await expect(
    detail.getByRole("heading", { name: "2. Project Credential mappings" }),
  ).toBeVisible();
  expect(api.unexpectedRequests).toEqual([]);
});

test("publish preflight is read-only and routes a missing Draft back to mappings", async ({
  page,
}) => {
  const api = await mockProjectSkillSecrets(page, { initialDraft: true });

  await page.goto(
    `/projects/alpha/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_2_ID}`,
  );
  const detail = page.getByRole("dialog", { name: "Browser Secret" });
  await detail.getByRole("button", { name: "发布版本" }).click();
  const publishDialog = page.getByRole("dialog", {
    name: "Publish Skill version",
  });
  await expect(
    publishDialog.getByText("Publish checks did not pass"),
  ).toBeVisible();
  await expect(
    publishDialog.getByRole("button", { name: "Publish version" }),
  ).toBeDisabled();
  await expect(
    publishDialog.getByLabel(new RegExp("Project Credential", "u")),
  ).toHaveCount(0);
  await publishDialog
    .getByRole("button", { name: "Go to Runtime credentials" })
    .click();
  await expect(
    detail.getByRole("tab", { name: "Runtime credentials" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    detail.getByRole("heading", { name: "2. Project Credential mappings" }),
  ).toBeVisible();
  expect(api.publishBodies).toHaveLength(0);
  expect(api.unexpectedRequests).toEqual([]);
});

test("Builder declaration edits invalidate checks, then revalidate and expose Credential setup after create", async ({
  page,
}) => {
  const api = await mockBuilderSecretCandidate(page);

  await page.goto(`/projects/alpha/skills/new/${SESSION_ID}`);
  await expect(page.getByText("Checks passed", { exact: true })).toBeVisible();
  const createSkill = page.getByRole("button", { name: "Create Skill" });
  await expect(createSkill).toBeEnabled();

  await page.getByRole("tab", { name: "Runtime credentials" }).click();
  await page.getByLabel("Variable name").fill(BUILDER_TOKEN);
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText(BUILDER_TOKEN, { exact: true })).toBeVisible();
  await expect(page.getByText("Checks passed", { exact: true })).toHaveCount(0);
  await expect(createSkill).toBeDisabled();

  await page.getByRole("button", { name: "Save changes" }).click();
  const checkSkill = page.getByRole("button", { name: "Check Skill" });
  await expect(checkSkill).toBeEnabled();
  await checkSkill.click();
  await expect(page.getByText("Checks passed", { exact: true })).toBeVisible();
  await expect(createSkill).toBeEnabled();

  await createSkill.click();
  const confirmation = page.getByRole("dialog", { name: "Create Skill?" });
  await confirmation.getByRole("button", { name: "Create" }).click();

  await expect(
    page.getByText(
      "Skill created and suspended with 1 environment variable declaration. Configure runtime credentials before enabling it.",
      { exact: true },
    ),
  ).toBeVisible();
  const configure = page.getByRole("link", { name: "Configure credentials" });
  await expect(configure).toHaveAttribute(
    "href",
    `/projects/alpha/skills?skill_id=${SKILL_ID}&skill_version_id=${VERSION_1_ID}&configure_credentials=1`,
  );

  expect(api.patchBodies).toHaveLength(1);
  expect(api.draftBodies).toHaveLength(1);
  expect(api.validateBodies).toHaveLength(1);
  expect(api.commitBodies).toHaveLength(1);
  expect(api.unexpectedRequests).toEqual([]);
});
