import { expect, test, type Page, type Route } from "@playwright/test";

import type { Project } from "@/core/projects/types";
import type {
  AssetVersion,
  ProjectAssetItem,
  ProjectCredentialItem,
  SkillFileForkInput,
} from "@/core/shared-assets";

import { mockLangGraphAPI } from "./utils/mock-api";

type AgentVersion = Extract<AssetVersion, { agent_id: string }>;
type SkillVersion = Extract<AssetVersion, { skill_id: string }>;
type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;
type CredentialVersion = Extract<AssetVersion, { credential_id: string }>;

const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const SECOND_PROJECT_ID = "10000000-0000-4000-8000-000000000002";
const SYSTEM_AGENT_ID = "20000000-0000-4000-8000-000000000001";
const PROJECT_AGENT_ID = "20000000-0000-4000-8000-000000000002";
const PROJECT_SKILL_ID = "20000000-0000-4000-8000-000000000003";
const PROJECT_MCP_ID = "20000000-0000-4000-8000-000000000004";
const CREDENTIAL_ID = "20000000-0000-4000-8000-000000000005";
const SYSTEM_CREDENTIAL_ID = "20000000-0000-4000-8000-000000000006";
const SYSTEM_SKILL_ID = "20000000-0000-4000-8000-000000000007";
const SYSTEM_VERSION_1 = "30000000-0000-4000-8000-000000000001";
const SYSTEM_VERSION_2 = "30000000-0000-4000-8000-000000000002";
const CREDENTIAL_VERSION_ID = "30000000-0000-4000-8000-000000000005";
const SYSTEM_CREDENTIAL_VERSION_ID = "30000000-0000-4000-8000-000000000006";
const SKILL_SOURCE_VERSION_ID = "30000000-0000-4000-8000-000000000021";
const SKILL_FORK_VERSION_ID = "30000000-0000-4000-8000-000000000022";
const SKILL_BLANK_VERSION_ID = "30000000-0000-4000-8000-000000000012";
const SKILL_SOURCE_CHECKSUM = "a".repeat(64);
const SKILL_SOURCE_FILE_CHECKSUM = "b".repeat(64);
const SKILL_FORK_FILE_CHECKSUM = "c".repeat(64);
const SKILL_FORK_CHECKSUM = "d".repeat(64);
const SKILL_SOURCE_CONTENT =
  "# Review Skill\n\nImmutable source version remains unchanged.";
const SKILL_FORK_CONTENT =
  "# Review Skill\n\nForked draft content is isolated from its source.";

const project: Project = {
  id: PROJECT_ID,
  slug: "research-lab",
  display_name: "Research Lab",
  description: "Shared research",
  icon: "folder",
  role: "admin",
  capabilities: [
    "project.read",
    "project.enter",
    "project.pin",
    "shared_assets.read",
    "shared_assets.execute",
    "shared_assets.edit",
    "shared_assets.manage_bindings",
    "mcp.credentials.approve",
  ],
  is_pinned: false,
  last_entered_at: null,
  member_count: 2,
  agent_count: 1,
  skill_count: 1,
  mcp_count: 1,
  quota_summary: {
    members: { used: 2, reserved: 0, limit: 20 },
    storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
    concurrent_runs: { used: 0, reserved: 0, limit: 3 },
    mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
  },
  status: "active",
  is_suspended: false,
  membership_version: 1,
  request_id: "request-project",
};

const secondProject: Project = {
  ...project,
  id: SECOND_PROJECT_ID,
  slug: "second-lab",
  display_name: "Second Lab",
  agent_count: 0,
  skill_count: 0,
  mcp_count: 0,
};

const now = "2026-07-14T00:00:00Z";
const systemCapabilities: ProjectAssetItem["capabilities"] = [
  "shared_assets.read",
  "shared_assets.execute",
  "shared_assets.manage_bindings",
];
const projectCapabilities: ProjectAssetItem["capabilities"] = [
  "shared_assets.read",
  "shared_assets.execute",
  "shared_assets.edit",
];
const mcpCapabilities: ProjectAssetItem["capabilities"] = [
  ...projectCapabilities,
  "mcp.credentials.approve",
];
const systemCredential: ProjectCredentialItem = {
  id: SYSTEM_CREDENTIAL_ID,
  scope: "system",
  project_id: null,
  name: "system-github",
  display_name: "System GitHub",
  credential_type: "token",
  status: "active",
  current_version_id: SYSTEM_CREDENTIAL_VERSION_ID,
  version: 1,
  created_by_user_id: "system-admin",
  created_at: now,
  updated_at: now,
  capabilities: ["shared_assets.read"],
};

function asset(
  id: string,
  slug: string,
  name: string,
  capabilities: ProjectAssetItem["capabilities"],
): ProjectAssetItem {
  const scope =
    id === SYSTEM_AGENT_ID || id === SYSTEM_SKILL_ID ? "system" : "project";
  return {
    id,
    scope,
    project_id: scope === "system" ? null : PROJECT_ID,
    slug,
    display_name: name,
    status: "active",
    current_published_version_id: scope === "system" ? SYSTEM_VERSION_1 : null,
    version: 1,
    created_by_user_id: "user-1",
    created_at: now,
    updated_at: now,
    capabilities,
    binding: null,
  };
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type AssetMock = {
  skillDeleteExpectedVersions: () => number[];
  staleConflicts: () => number;
  validatedMutations: () => number;
  skillFileRequests: () => Array<{ path: string; versionId: string }>;
  skillForkRequests: () => Array<{
    body: SkillFileForkInput;
    sourceVersionId: string;
  }>;
};

async function mockProjectAssets(
  page: Page,
  options: { skillFileWorkflow?: boolean } = {},
): Promise<AssetMock> {
  let systemAgent = asset(
    SYSTEM_AGENT_ID,
    "analyst",
    "Analyst",
    systemCapabilities,
  );
  let projectAgent = asset(
    PROJECT_AGENT_ID,
    "analyst",
    "Analyst",
    projectCapabilities,
  );
  let projectSkill = asset(
    PROJECT_SKILL_ID,
    "review-skill",
    "Review Skill",
    projectCapabilities,
  );
  let systemSkill = {
    ...asset(
      SYSTEM_SKILL_ID,
      "academic-paper-review",
      "academic-paper-review",
      systemCapabilities,
    ),
    description:
      "Review, analyze, critique, and summarize academic papers with structured methodology assessment and constructive feedback.",
  };
  let projectMcp = asset(
    PROJECT_MCP_ID,
    "github-mcp",
    "GitHub MCP",
    mcpCapabilities,
  );
  let binding: ProjectAssetItem["binding"] = null;
  let skillBinding: ProjectAssetItem["binding"] = null;
  let agentVersions: AgentVersion[] = [];
  const sourceSkillVersion: SkillVersion = {
    id: SKILL_SOURCE_VERSION_ID,
    skill_id: PROJECT_SKILL_ID,
    version_number: 1,
    workflow_status: "published",
    description: "Immutable source Skill",
    frontmatter: {},
    compatibility: null,
    secret_requirements: [],
    scan_decision: "allow",
    scan_rule_ids: [],
    scan_summary: {},
    file_views: [
      {
        path: "SKILL.md",
        media_type: "text/markdown",
        size_bytes: SKILL_SOURCE_CONTENT.length,
        sha256: SKILL_SOURCE_FILE_CHECKSUM,
      },
    ],
    supersedes_version_id: null,
    payload_checksum: SKILL_SOURCE_CHECKSUM,
    created_by_user_id: "user-1",
    created_at: now,
  };
  let skillVersions: SkillVersion[] = options.skillFileWorkflow
    ? [sourceSkillVersion]
    : [];
  let mcpVersions: McpVersion[] = [];
  let credential: ProjectCredentialItem | null = null;
  let credentialVersions: CredentialVersion[] = [];
  let staleConflicts = 0;
  let validatedMutations = 0;
  let projectSkillDeleted = false;
  const skillDeleteExpectedVersions: number[] = [];
  const skillFileRequests: Array<{ path: string; versionId: string }> = [];
  const skillForkRequests: Array<{
    body: SkillFileForkInput;
    sourceVersionId: string;
  }> = [];
  const skillFileContents = new Map<string, string>([
    [SKILL_SOURCE_VERSION_ID, SKILL_SOURCE_CONTENT],
  ]);

  if (options.skillFileWorkflow) {
    projectSkill = {
      ...projectSkill,
      current_published_version_id: SKILL_SOURCE_VERSION_ID,
    };
  }

  async function requireExpectedVersion(
    route: Route,
    actual: unknown,
    expected: number,
  ): Promise<boolean> {
    if (actual !== expected) {
      staleConflicts += 1;
      await json(
        route,
        {
          detail: {
            code: "asset_conflict",
            message: "Asset state conflict",
            request_id: "request-stale-version",
          },
        },
        409,
      );
      return false;
    }
    validatedMutations += 1;
    return true;
  }

  const systemVersions: AgentVersion[] = [
    {
      id: SYSTEM_VERSION_2,
      agent_id: SYSTEM_AGENT_ID,
      version_number: 2,
      workflow_status: "published",
      description: "System v2",
      soul: "",
      model_ref: "default",
      tool_groups: [],
      skill_version_ids: [],
      mcp_version_ids: [],
      supersedes_version_id: SYSTEM_VERSION_1,
      payload_checksum: "system-v2",
      created_by_user_id: "system-admin",
      created_at: now,
    },
    {
      id: SYSTEM_VERSION_1,
      agent_id: SYSTEM_AGENT_ID,
      version_number: 1,
      workflow_status: "published",
      description: "System v1",
      soul: "",
      model_ref: "default",
      tool_groups: [],
      skill_version_ids: [],
      mcp_version_ids: [],
      supersedes_version_id: null,
      payload_checksum: "system-v1",
      created_by_user_id: "system-admin",
      created_at: now,
    },
  ];

  await page.route(/\/api\/projects(?:\/.*)?(?:\?.*)?$/, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path.endsWith("/api/projects") && method === "GET") {
      await json(route, { items: [project, secondProject], next_cursor: null });
      return;
    }
    if (path.endsWith(`/api/projects/${PROJECT_ID}/enter`)) {
      await json(route, project);
      return;
    }
    if (path.endsWith(`/api/projects/${SECOND_PROJECT_ID}/enter`)) {
      await json(route, secondProject);
      return;
    }

    const projectId = path.includes(SECOND_PROJECT_ID)
      ? SECOND_PROJECT_ID
      : PROJECT_ID;
    if (projectId === SECOND_PROJECT_ID && method === "GET") {
      if (/\/(agents|skills|mcp-servers)$/.test(path)) {
        await json(route, {
          system_items: [],
          project_items: [],
          request_id: "request-second-assets",
        });
        return;
      }
      if (path.endsWith("/credentials")) {
        await json(route, {
          system_items: [],
          project_items: [],
          request_id: "request-second-credentials",
        });
        return;
      }
    }

    if (
      path.endsWith(`/api/projects/${PROJECT_ID}/agents`) &&
      method === "GET"
    ) {
      await json(route, {
        system_items: [{ ...systemAgent, binding }],
        project_items: [projectAgent],
        request_id: "request-agents",
      });
      return;
    }
    if (
      path.endsWith(`/api/projects/${PROJECT_ID}/skills`) &&
      method === "GET"
    ) {
      await json(route, {
        system_items: [{ ...systemSkill, binding: skillBinding }],
        project_items: projectSkillDeleted ? [] : [projectSkill],
        request_id: "request-skills",
      });
      return;
    }
    if (
      path.endsWith(`/api/projects/${PROJECT_ID}/mcp-servers`) &&
      method === "GET"
    ) {
      await json(route, {
        system_items: [],
        project_items: [projectMcp],
        request_id: "request-mcp",
      });
      return;
    }
    if (
      path.endsWith(`/api/projects/${PROJECT_ID}/credentials`) &&
      method === "GET"
    ) {
      await json(route, {
        system_items: [systemCredential],
        project_items: credential ? [credential] : [],
        request_id: "request-credentials",
      });
      return;
    }

    if (
      path.endsWith(`/api/projects/${PROJECT_ID}/skills/${PROJECT_SKILL_ID}`) &&
      method === "DELETE"
    ) {
      const body = request.postDataJSON() as {
        expected_asset_version: number;
      };
      skillDeleteExpectedVersions.push(body.expected_asset_version);
      if (projectSkillDeleted) {
        await json(
          route,
          {
            detail: {
              code: "asset_not_found",
              request_id: "request-skill-deleted",
            },
          },
          404,
        );
        return;
      }
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_asset_version,
          projectSkill.version,
        ))
      ) {
        return;
      }
      projectSkillDeleted = true;
      skillVersions = [];
      skillFileContents.clear();
      await route.fulfill({ status: 204, body: "" });
      return;
    }

    if (
      path.endsWith(
        `/api/projects/${PROJECT_ID}/skills/${PROJECT_SKILL_ID}/suspend`,
      ) &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as {
        expected_asset_version: number;
      };
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_asset_version,
          projectSkill.version,
        ))
      ) {
        return;
      }
      projectSkill = {
        ...projectSkill,
        status: "suspended",
        version: projectSkill.version + 1,
      };
      const {
        binding: _binding,
        capabilities: _capabilities,
        ...responseItem
      } = projectSkill;
      void _binding;
      void _capabilities;
      await json(route, {
        item: responseItem,
        request_id: "request-skill-suspended",
      });
      return;
    }

    if (
      path.endsWith(
        `/api/projects/${PROJECT_ID}/agents/${SYSTEM_AGENT_ID}/versions`,
      ) &&
      method === "GET"
    ) {
      await json(route, { data: systemVersions, request_id: "system-history" });
      return;
    }

    const skillFileMatch = new RegExp(
      `/skills/${PROJECT_SKILL_ID}/versions/([^/]+)/files/content$`,
    ).exec(path);
    if (skillFileMatch && method === "GET") {
      const versionId = skillFileMatch[1]!;
      const filePath = new URL(request.url()).searchParams.get("path") ?? "";
      skillFileRequests.push({ path: filePath, versionId });
      const version = skillVersions.find((item) => item.id === versionId);
      const content = skillFileContents.get(versionId);
      const file = version?.file_views.find((item) => item.path === filePath);
      if (!version || content === undefined || !file) {
        await json(
          route,
          {
            detail: {
              code: "asset_not_found",
              request_id: "request-skill-file-404",
            },
          },
          404,
        );
        return;
      }
      await json(route, {
        data: {
          ...file,
          preview_status: "ready",
          encoding: "utf-8",
          content,
          source_payload_checksum: version.payload_checksum,
          asset_version: projectSkill.version,
        },
        request_id: `skill-file-${versionId}`,
      });
      return;
    }

    const histories: Array<[string, AssetVersion[]]> = [
      [PROJECT_AGENT_ID, agentVersions],
      [PROJECT_SKILL_ID, skillVersions],
      [PROJECT_MCP_ID, mcpVersions],
      [CREDENTIAL_ID, credentialVersions],
    ];
    for (const [id, versions] of histories) {
      if (path.endsWith(`/${id}/versions`) && method === "GET") {
        await json(route, { data: versions, request_id: `history-${id}` });
        return;
      }
    }

    const skillForkMatch = new RegExp(
      `/skills/${PROJECT_SKILL_ID}/versions/([^/]+)/fork$`,
    ).exec(path);
    if (skillForkMatch && method === "POST") {
      const sourceVersionId = skillForkMatch[1]!;
      const body = request.postDataJSON() as SkillFileForkInput;
      skillForkRequests.push({ body, sourceVersionId });
      const sourceVersion = skillVersions.find(
        (version) => version.id === sourceVersionId,
      );
      if (
        !sourceVersion ||
        body.expected_asset_version !== projectSkill.version ||
        body.expected_source_payload_checksum !== sourceVersion.payload_checksum
      ) {
        staleConflicts += 1;
        await json(
          route,
          {
            detail: {
              code: "asset_conflict",
              request_id: "request-skill-fork-conflict",
            },
          },
          409,
        );
        return;
      }
      const replacement = body.changes.find(
        (change) => change.path === "SKILL.md",
      );
      if (replacement?.op !== "replace" || body.changes.length !== 1) {
        await json(
          route,
          {
            detail: {
              code: "asset_validation_failed",
              request_id: "request-skill-fork-invalid",
            },
          },
          422,
        );
        return;
      }
      validatedMutations += 1;
      const forkedVersion: SkillVersion = {
        ...sourceVersion,
        id: SKILL_FORK_VERSION_ID,
        version_number: 2,
        workflow_status: "draft",
        file_views: [
          {
            path: "SKILL.md",
            media_type: replacement.media_type,
            size_bytes: replacement.content.length,
            sha256: SKILL_FORK_FILE_CHECKSUM,
          },
        ],
        supersedes_version_id: sourceVersion.id,
        payload_checksum: SKILL_FORK_CHECKSUM,
        created_at: "2026-07-22T01:00:00Z",
      };
      skillFileContents.set(SKILL_FORK_VERSION_ID, replacement.content);
      skillVersions = [forkedVersion, ...skillVersions];
      projectSkill = { ...projectSkill, version: projectSkill.version + 1 };
      await json(
        route,
        { data: forkedVersion, request_id: "skill-file-fork" },
        201,
      );
      return;
    }

    if (
      path.endsWith(
        `/api/projects/${PROJECT_ID}/system-agent-bindings/${SYSTEM_AGENT_ID}/disable`,
      ) &&
      method === "POST" &&
      binding
    ) {
      const body = request.postDataJSON() as {
        expected_binding_version: number;
      };
      if (body.expected_binding_version !== binding.version) {
        staleConflicts += 1;
        await json(route, { detail: { code: "asset_conflict" } }, 409);
        return;
      }
      validatedMutations += 1;
      binding = {
        ...binding,
        enabled: false,
        version: binding.version + 1,
        updated_at: now,
      };
      await json(route, { ...binding, request_id: "binding-disable" });
      return;
    }
    if (
      path.endsWith(
        `/api/projects/${PROJECT_ID}/system-skill-bindings/${SYSTEM_SKILL_ID}/disable`,
      ) &&
      method === "POST" &&
      skillBinding
    ) {
      const body = request.postDataJSON() as {
        expected_binding_version: number;
      };
      if (body.expected_binding_version !== skillBinding.version) {
        staleConflicts += 1;
        await json(route, { detail: { code: "asset_conflict" } }, 409);
        return;
      }
      validatedMutations += 1;
      skillBinding = {
        ...skillBinding,
        enabled: false,
        version: skillBinding.version + 1,
        updated_at: now,
      };
      await json(route, {
        ...skillBinding,
        request_id: "skill-binding-disable",
      });
      return;
    }
    if (
      path.endsWith(`/api/projects/${PROJECT_ID}/system-agent-bindings`) &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as {
        asset_id: string;
        version_id: string;
        expected_binding_version?: number;
      };
      if (binding && body.expected_binding_version !== binding.version) {
        staleConflicts += 1;
        await json(route, { detail: { code: "asset_conflict" } }, 409);
        return;
      }
      validatedMutations += 1;
      binding = {
        project_id: PROJECT_ID,
        kind: "agent",
        asset_id: SYSTEM_AGENT_ID,
        version_id: body.version_id,
        enabled: true,
        version: (binding?.version ?? 0) + 1,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: now,
        updated_at: now,
      };
      systemAgent = {
        ...systemAgent,
        current_published_version_id: SYSTEM_VERSION_2,
      };
      await json(route, { ...binding, request_id: "binding-enable" }, 201);
      return;
    }
    if (
      path.endsWith(`/api/projects/${PROJECT_ID}/system-skill-bindings`) &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as {
        asset_id: string;
        version_id: string;
        expected_binding_version?: number;
      };
      if (
        body.asset_id !== SYSTEM_SKILL_ID ||
        (skillBinding && body.expected_binding_version !== skillBinding.version)
      ) {
        staleConflicts += 1;
        await json(route, { detail: { code: "asset_conflict" } }, 409);
        return;
      }
      validatedMutations += 1;
      skillBinding = {
        project_id: PROJECT_ID,
        kind: "skill",
        asset_id: SYSTEM_SKILL_ID,
        version_id: body.version_id,
        enabled: true,
        version: (skillBinding?.version ?? 0) + 1,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: now,
        updated_at: now,
      };
      systemSkill = {
        ...systemSkill,
        current_published_version_id: body.version_id,
      };
      await json(
        route,
        { ...skillBinding, request_id: "skill-binding-enable" },
        201,
      );
      return;
    }
    const bindingMove = new RegExp(
      `/system-agent-bindings/${SYSTEM_AGENT_ID}/(upgrade|rollback)$`,
    ).exec(path);
    if (bindingMove && method === "POST" && binding) {
      const body = request.postDataJSON() as {
        version_id: string;
        expected_binding_version: number;
      };
      if (body.expected_binding_version !== binding.version) {
        staleConflicts += 1;
        await json(route, { detail: { code: "asset_conflict" } }, 409);
        return;
      }
      validatedMutations += 1;
      binding = {
        ...binding,
        version_id: body.version_id,
        version: binding.version + 1,
        updated_at: now,
      };
      await json(route, { ...binding, request_id: "binding-move" });
      return;
    }

    if (path.endsWith(`/${PROJECT_AGENT_ID}/versions`) && method === "POST") {
      const body = request.postDataJSON() as {
        description: string;
        soul: string;
        model_ref: string;
        tool_groups: string[];
        skill_version_ids: string[];
        mcp_version_ids: string[];
        expected_asset_version: number;
      };
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_asset_version,
          projectAgent.version,
        ))
      ) {
        return;
      }
      const { expected_asset_version: _expected, ...input } = body;
      void _expected;
      const version: AgentVersion = {
        id: "30000000-0000-4000-8000-000000000011",
        agent_id: PROJECT_AGENT_ID,
        version_number: 1,
        workflow_status: "draft",
        ...input,
        supersedes_version_id: null,
        payload_checksum: "agent-checksum",
        created_by_user_id: "user-1",
        created_at: now,
      };
      agentVersions = [version];
      projectAgent = { ...projectAgent, version: 2 };
      await json(route, { data: version, request_id: "agent-create" }, 201);
      return;
    }
    if (path.endsWith(`/${PROJECT_SKILL_ID}/versions`) && method === "POST") {
      const body = request.postDataJSON() as {
        files: Array<{
          path: string;
          content_base64: string;
          media_type: string;
        }>;
        expected_asset_version: number;
      };
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_asset_version,
          projectSkill.version,
        ))
      ) {
        return;
      }
      const file = body.files[0];
      const content = file
        ? Buffer.from(file.content_base64, "base64").toString("utf8")
        : "";
      const frontmatter =
        /^---\nname: ([^\n]+)\ndescription: ([^\n]+)\n---(?:\n|$)/u.exec(
          content,
        );
      if (
        body.files.length !== 1 ||
        file?.path !== "SKILL.md" ||
        file.media_type !== "text/markdown" ||
        frontmatter?.[1] !== projectSkill.slug ||
        frontmatter[2]?.trim() === ""
      ) {
        await json(
          route,
          {
            detail: {
              code: "asset_validation_failed",
              request_id: "request-skill-create-invalid",
            },
          },
          422,
        );
        return;
      }
      const version: SkillVersion = {
        id: SKILL_BLANK_VERSION_ID,
        skill_id: PROJECT_SKILL_ID,
        version_number:
          Math.max(0, ...skillVersions.map((item) => item.version_number)) + 1,
        workflow_status: "draft",
        description: frontmatter[2]!,
        frontmatter: {
          name: frontmatter[1],
          description: frontmatter[2]!,
        },
        compatibility: null,
        secret_requirements: [],
        scan_decision: "allow",
        scan_rule_ids: [],
        scan_summary: {},
        file_views: [
          {
            path: "SKILL.md",
            media_type: "text/markdown",
            size_bytes: Buffer.byteLength(content),
            sha256: "skill-file",
          },
        ],
        supersedes_version_id: projectSkill.current_published_version_id,
        payload_checksum: "skill-checksum",
        created_by_user_id: "user-1",
        created_at: now,
      };
      skillVersions = [version, ...skillVersions];
      skillFileContents.set(version.id, content);
      projectSkill = { ...projectSkill, version: 2 };
      await json(route, { data: version, request_id: "skill-create" }, 201);
      return;
    }
    if (path.endsWith(`/${PROJECT_MCP_ID}/versions`) && method === "POST") {
      const body = request.postDataJSON() as McpVersion["definition"] & {
        expected_asset_version: number;
      };
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_asset_version,
          projectMcp.version,
        ))
      ) {
        return;
      }
      const { expected_asset_version: _expected, ...input } = body;
      void _expected;
      const version: McpVersion = {
        id: "30000000-0000-4000-8000-000000000013",
        mcp_server_id: PROJECT_MCP_ID,
        version_number: 1,
        workflow_status: "draft",
        definition: input,
        credential_slots: input.credential_slots.map((slot, index) => ({
          ...slot,
          id: `40000000-0000-4000-8000-00000000000${index + 1}`,
        })),
        credential_grants: [],
        supersedes_version_id: null,
        payload_checksum: "mcp-checksum",
        submitted_at: null,
        reviewed_at: null,
        reviewed_by_user_id: null,
        created_by_user_id: "user-1",
        created_at: now,
      };
      mcpVersions = [version];
      projectMcp = { ...projectMcp, version: 2 };
      await json(route, { data: version, request_id: "mcp-create" }, 201);
      return;
    }

    const publish = /\/versions\/([^/]+)\/publish$/.exec(path);
    if (publish && method === "POST") {
      if (path.includes(PROJECT_AGENT_ID)) {
        const body = request.postDataJSON() as {
          expected_asset_version: number;
        };
        if (
          !(await requireExpectedVersion(
            route,
            body.expected_asset_version,
            projectAgent.version,
          ))
        ) {
          return;
        }
        agentVersions = agentVersions.map((version) => ({
          ...version,
          workflow_status: "published",
        }));
        projectAgent = {
          ...projectAgent,
          version: 3,
          current_published_version_id: publish[1]!,
        };
        await json(route, {
          data: agentVersions[0],
          request_id: "agent-publish",
        });
        return;
      }
      if (path.includes(PROJECT_SKILL_ID)) {
        const body = request.postDataJSON() as {
          expected_asset_version: number;
        };
        if (
          !(await requireExpectedVersion(
            route,
            body.expected_asset_version,
            projectSkill.version,
          ))
        ) {
          return;
        }
        skillVersions = skillVersions.map((version) => ({
          ...version,
          workflow_status: "published",
        }));
        projectSkill = {
          ...projectSkill,
          version: 3,
          current_published_version_id: publish[1]!,
        };
        await json(route, {
          data: skillVersions[0],
          request_id: "skill-publish",
        });
        return;
      }
    }
    if (path.endsWith("/submit-approval") && method === "POST") {
      const body = request.postDataJSON() as {
        expected_asset_version: number;
      };
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_asset_version,
          projectMcp.version,
        ))
      ) {
        return;
      }
      mcpVersions = mcpVersions.map((version) => ({
        ...version,
        workflow_status: "pending_approval",
        submitted_at: now,
      }));
      projectMcp = { ...projectMcp, version: 3 };
      await json(route, { data: mcpVersions[0], request_id: "mcp-submit" });
      return;
    }
    if (path.endsWith("/approve") && method === "POST") {
      const body = request.postDataJSON() as {
        credential_versions: Record<string, string>;
        expected_asset_version: number;
      };
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_asset_version,
          projectMcp.version,
        ))
      ) {
        return;
      }
      const selectedCredentialVersion =
        body.credential_versions["github-token"];
      if (
        !selectedCredentialVersion ||
        !credential ||
        !credentialVersions.some(
          (version) =>
            version.id === selectedCredentialVersion &&
            version.status === "active",
        )
      ) {
        await json(
          route,
          {
            detail: {
              code: "asset_validation_failed",
              request_id: "request-invalid-credential",
            },
          },
          422,
        );
        return;
      }
      mcpVersions = mcpVersions.map((version) => ({
        ...version,
        workflow_status: "published",
        reviewed_at: now,
        reviewed_by_user_id: "user-1",
        credential_grants: version.credential_slots.map((slot, index) => ({
          id: `50000000-0000-4000-8000-00000000000${index + 1}`,
          mcp_server_version_id: version.id,
          credential_slot_id: slot.id,
          credential_version_id: selectedCredentialVersion,
          status: "active" as const,
          version: 1,
          created_by_user_id: "user-1",
          created_at: now,
        })),
      }));
      projectMcp = {
        ...projectMcp,
        version: 4,
        current_published_version_id: mcpVersions[0]!.id,
      };
      await json(route, { data: mcpVersions[0], request_id: "mcp-approve" });
      return;
    }

    if (
      path.endsWith(`/api/projects/${PROJECT_ID}/credentials`) &&
      method === "POST"
    ) {
      credential = {
        id: CREDENTIAL_ID,
        scope: "project",
        project_id: PROJECT_ID,
        name: "github",
        display_name: "GitHub",
        credential_type: "token",
        status: "active",
        current_version_id: CREDENTIAL_VERSION_ID,
        version: 1,
        created_by_user_id: "user-1",
        created_at: now,
        updated_at: now,
        capabilities: ["shared_assets.read", "mcp.credentials.approve"],
      };
      credentialVersions = [
        {
          id: CREDENTIAL_VERSION_ID,
          credential_id: CREDENTIAL_ID,
          version_number: 1,
          status: "active",
          payload_schema_version: 1,
          payload_schema: { env: ["TOKEN"] },
          supersedes_version_id: null,
          created_by_user_id: "user-1",
          created_at: now,
        },
      ];
      const { capabilities: _capabilities, ...responseItem } = credential;
      void _capabilities;
      await json(
        route,
        { item: responseItem, request_id: "credential-create" },
        201,
      );
      return;
    }
    if (
      path.endsWith(`/${CREDENTIAL_ID}/replace`) &&
      method === "POST" &&
      credential
    ) {
      const body = request.postDataJSON() as {
        expected_credential_version: number;
      };
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_credential_version,
          credential.version,
        ))
      ) {
        return;
      }
      credential = {
        ...credential,
        current_version_id: "30000000-0000-4000-8000-000000000006",
        version: 2,
        updated_at: now,
      };
      const version: CredentialVersion = {
        ...credentialVersions[0]!,
        id: "30000000-0000-4000-8000-000000000006",
        version_number: 2,
        supersedes_version_id: CREDENTIAL_VERSION_ID,
      };
      credentialVersions = [
        version,
        { ...credentialVersions[0]!, status: "retired" },
      ];
      await json(route, { data: version, request_id: "credential-replace" });
      return;
    }
    if (
      path.endsWith(`/${CREDENTIAL_ID}/migrate-grants`) &&
      method === "POST" &&
      credential
    ) {
      const body = request.postDataJSON() as {
        expected_credential_version: number;
      };
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_credential_version,
          credential.version,
        ))
      ) {
        return;
      }
      const currentVersionId = credential.current_version_id!;
      let migratedCount = 0;
      mcpVersions = mcpVersions.map((version) => ({
        ...version,
        credential_grants: version.credential_grants.flatMap((grant) => {
          if (
            grant.status !== "active" ||
            grant.credential_version_id === currentVersionId
          ) {
            return [grant];
          }
          migratedCount += 1;
          return [
            {
              ...grant,
              status: "revoked" as const,
              version: grant.version + 1,
            },
            {
              ...grant,
              id: "50000000-0000-4000-8000-000000000099",
              credential_version_id: currentVersionId,
              version: 1,
            },
          ];
        }),
      }));
      await json(route, {
        credential_id: credential.id,
        credential_version_id: currentVersionId,
        migrated_count: migratedCount,
        request_id: "credential-migration",
      });
      return;
    }
    if (
      path.endsWith(`/${CREDENTIAL_ID}/revoke`) &&
      method === "POST" &&
      credential
    ) {
      const body = request.postDataJSON() as {
        expected_credential_version: number;
      };
      if (
        !(await requireExpectedVersion(
          route,
          body.expected_credential_version,
          credential.version,
        ))
      ) {
        return;
      }
      credential = { ...credential, status: "revoked", version: 3 };
      credentialVersions = credentialVersions.map((version) => ({
        ...version,
        status: "revoked",
      }));
      mcpVersions = mcpVersions.map((version) => ({
        ...version,
        credential_grants: version.credential_grants.map((grant) => ({
          ...grant,
          status: "revoked" as const,
          version:
            grant.status === "active" ? grant.version + 1 : grant.version,
        })),
      }));
      const { capabilities: _capabilities, ...responseItem } = credential;
      void _capabilities;
      await json(route, {
        item: responseItem,
        request_id: "credential-revoke",
      });
      return;
    }

    await json(
      route,
      { detail: { code: "asset_not_found", request_id: "request-404" } },
      404,
    );
  });

  return {
    skillDeleteExpectedVersions: () => [...skillDeleteExpectedVersions],
    staleConflicts: () => staleConflicts,
    validatedMutations: () => validatedMutations,
    skillFileRequests: () => [...skillFileRequests],
    skillForkRequests: () => [...skillForkRequests],
  };
}

test("Agent menu hides system-provided Agents without deleting project Agents", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await page.route("**/api/models", async (route) => {
    await json(route, {
      models: [
        {
          id: "model-default",
          name: "default",
          model: "provider-model-id",
          display_name: "Default logical model",
          supports_thinking: false,
          supports_reasoning_effort: false,
        },
      ],
      token_usage: { enabled: false },
    });
  });
  const mock = await mockProjectAssets(page);
  await page.goto("/projects/research-lab/agents");

  await expect(page.getByText("Analyst", { exact: true })).toHaveCount(1);
  await expect(page.getByRole("tab", { name: /系统提供/u })).toHaveCount(0);
  await expect(page.getByRole("switch", { name: /Analyst/u })).toHaveCount(0);
  await expect(
    page.getByText("系统默认 Main 不在此列表中展示。"),
  ).toBeVisible();
  expect(mock.validatedMutations()).toBe(0);
  expect(mock.staleConflicts()).toBe(0);
});

test("Skill list shows descriptions and keeps quick binding separate from details", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const mock = await mockProjectAssets(page);

  await page.goto("/projects/research-lab/skills");
  await page.getByRole("tab", { name: /系统提供/u }).click();

  await expect(
    page.getByText(
      "Review, analyze, critique, and summarize academic papers with structured methodology assessment and constructive feedback.",
    ),
  ).toBeVisible();

  const toggle = page.getByRole("switch", {
    name: "启用 academic-paper-review",
  });
  await expect(toggle).not.toBeChecked();
  await toggle.click();
  await expect(
    page.getByRole("switch", {
      name: "停用 academic-paper-review",
    }),
  ).toBeChecked();

  await page
    .getByRole("switch", { name: "停用 academic-paper-review" })
    .click();
  await expect(
    page.getByRole("switch", { name: "启用 academic-paper-review" }),
  ).not.toBeChecked();

  await page
    .getByRole("button", { name: "查看 academic-paper-review 详情" })
    .click();
  await expect(
    page.getByRole("dialog", { name: "academic-paper-review", exact: true }),
  ).toBeVisible();

  expect(mock.validatedMutations()).toBe(2);
  expect(mock.staleConflicts()).toBe(0);
});

test("project Skill delete waits five seconds and removes the whole package", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const mock = await mockProjectAssets(page, { skillFileWorkflow: true });

  await page.goto("/projects/research-lab/skills");
  await page.getByRole("button", { name: "查看 Review Skill 详情" }).click();

  const detail = page.getByRole("dialog", {
    name: "Review Skill",
    exact: true,
  });
  await expect(detail.getByRole("button", { name: "归档" })).toHaveCount(0);
  await detail.getByRole("button", { name: "删除 Skill" }).click();

  const confirmation = page.getByRole("dialog", {
    name: "永久删除 Skill？",
  });
  await expect(confirmation).toContainText("永久删除整个 Skill 包");
  await expect(confirmation).toContainText("所有版本");
  await expect(confirmation).toContainText("不可恢复");
  const confirm = confirmation.getByRole("button", {
    name: /确认(?:永久)?删除/u,
  });
  await expect(confirm).toBeDisabled();
  await expect(confirm).toBeEnabled({ timeout: 6_500 });
  await confirm.click();

  await expect(confirmation).toHaveCount(0);
  await expect(detail).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "查看 Review Skill 详情" }),
  ).toHaveCount(0);
  expect(mock.validatedMutations()).toBe(1);
  expect(mock.staleConflicts()).toBe(0);
});

test("project Skill delete keeps the opening revision when the catalog refreshes during confirmation", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const mock = await mockProjectAssets(page, { skillFileWorkflow: true });

  await page.goto("/projects/research-lab/skills");
  await page.getByRole("button", { name: "查看 Review Skill 详情" }).click();
  const detail = page.getByRole("dialog", {
    name: "Review Skill",
    exact: true,
  });
  const concurrentSuspend = page
    .locator("button")
    .filter({ hasText: /^暂停$/u });
  await expect(concurrentSuspend).toBeVisible();
  await detail.getByRole("button", { name: "删除 Skill" }).click();

  const confirmation = page.getByRole("dialog", {
    name: "永久删除 Skill？",
  });
  const confirm = confirmation.getByRole("button", {
    name: /确认(?:永久)?删除/u,
  });
  await expect(confirm).toBeDisabled();

  const refreshedCatalog = page.waitForResponse((response) => {
    const request = response.request();
    return (
      request.method() === "GET" &&
      new URL(request.url()).pathname.endsWith(
        `/api/projects/${PROJECT_ID}/skills`,
      )
    );
  });
  await concurrentSuspend.evaluate((button) => {
    (button as HTMLButtonElement).click();
  });
  await refreshedCatalog;

  await expect(confirm).toBeEnabled({ timeout: 6_500 });
  await confirm.click();

  await expect(confirmation).toContainText("资产状态已变化，请刷新后重试。");
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "取消" }).click();
  await expect(confirmation).toHaveCount(0);
  await expect(detail).toBeVisible();
  expect(mock.skillDeleteExpectedVersions()).toEqual([1]);
  expect(mock.validatedMutations()).toBe(1);
  expect(mock.staleConflicts()).toBe(1);
});

test("Skill file preview and fork preserve immutable source version", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const mock = await mockProjectAssets(page, { skillFileWorkflow: true });

  await page.goto("/projects/research-lab/skills");
  await page.getByRole("button", { name: "查看 Review Skill 详情" }).click();

  const detail = page.getByRole("dialog");
  const sourceView = detail.locator("pre").filter({
    hasText: "Immutable source version remains unchanged.",
  });
  await expect(sourceView).toContainText(SKILL_SOURCE_CONTENT);
  await expect(detail.getByRole("button", { name: "源码" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  await detail.getByRole("button", { name: "预览" }).click();
  await expect(detail.getByRole("button", { name: "预览" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(
    detail.getByText("Immutable source version remains unchanged.", {
      exact: true,
    }),
  ).toBeVisible();
  await detail.getByRole("button", { name: "源码" }).click();
  await expect(detail.getByRole("button", { name: "源码" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  await detail.getByRole("button", { name: "编辑为新版本" }).click();
  const editor = detail.getByLabel("编辑 SKILL.md");
  await expect(editor).toHaveValue(SKILL_SOURCE_CONTENT);
  await editor.fill(SKILL_FORK_CONTENT);
  await expect(detail.getByText("已有 1 项未保存修改")).toBeVisible();
  await detail.getByRole("button", { name: "保存为新版本" }).click();

  const versionSelector = detail.getByLabel("查看版本");
  await expect(versionSelector).toHaveValue(SKILL_FORK_VERSION_ID);
  await expect(versionSelector.locator("option:checked")).toHaveText(
    "版本 2 · 草稿",
  );
  await expect(
    detail.locator("pre").filter({
      hasText: "Forked draft content is isolated from its source.",
    }),
  ).toContainText(SKILL_FORK_CONTENT);

  expect(mock.skillForkRequests()).toEqual([
    {
      sourceVersionId: SKILL_SOURCE_VERSION_ID,
      body: {
        expected_asset_version: 1,
        expected_source_payload_checksum: SKILL_SOURCE_CHECKSUM,
        changes: [
          {
            op: "replace",
            path: "SKILL.md",
            content: SKILL_FORK_CONTENT,
            media_type: "text/markdown",
          },
        ],
      },
    },
  ]);

  await versionSelector.selectOption(SKILL_SOURCE_VERSION_ID);
  const immutableSource = detail.locator("pre").filter({
    hasText: "Immutable source version remains unchanged.",
  });
  await expect(immutableSource).toContainText(SKILL_SOURCE_CONTENT);
  await expect(immutableSource).not.toContainText(SKILL_FORK_CONTENT);
  expect(mock.skillFileRequests()).toEqual(
    expect.arrayContaining([
      { path: "SKILL.md", versionId: SKILL_SOURCE_VERSION_ID },
      { path: "SKILL.md", versionId: SKILL_FORK_VERSION_ID },
    ]),
  );
  expect(mock.validatedMutations()).toBe(1);
  expect(mock.staleConflicts()).toBe(0);
});

test("blank Skill creation uses the real contract, selects the new draft, and blocks dirty publish", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  const mock = await mockProjectAssets(page, { skillFileWorkflow: true });

  await page.goto("/projects/research-lab/skills");
  await page.getByRole("button", { name: "查看 Review Skill 详情" }).click();
  const detail = page.getByRole("dialog", {
    name: "Review Skill",
    exact: true,
  });
  await expect(detail.getByLabel("查看版本")).toHaveValue(
    SKILL_SOURCE_VERSION_ID,
  );

  await detail.getByRole("button", { name: "从空白创建" }).click();
  const create = page.getByRole("dialog", { name: "创建 Skill 版本" });
  const content = create.getByLabel("文件内容");
  await expect(content).toHaveValue(
    /^---\nname: review-skill\ndescription: .+\n---\n/u,
  );
  await expect(create.getByText("SKILL.md", { exact: true })).toBeVisible();
  await expect(
    create.getByText("text/markdown", { exact: true }),
  ).toBeVisible();
  await expect(create.locator('input[name="path"]')).toHaveCount(0);
  await expect(create.locator('input[name="media_type"]')).toHaveCount(0);
  await create.getByRole("button", { name: "创建版本" }).click();
  await expect(create).toHaveCount(0);

  const selector = detail.getByLabel("查看版本");
  await expect(selector).toHaveValue(SKILL_BLANK_VERSION_ID);
  await expect(selector.locator("option:checked")).toHaveText("版本 2 · 草稿");

  await detail.getByRole("button", { name: "编辑为新版本" }).click();
  const editor = detail.getByLabel("编辑 SKILL.md");
  await editor.fill(`${await editor.inputValue()}\n\nKeep this edit local.`);
  await expect(detail.getByRole("button", { name: "发布版本" })).toBeDisabled();
  expect(mock.validatedMutations()).toBe(1);
  expect(mock.staleConflicts()).toBe(0);
});

test("project Credential replace requires explicit grant migration and confirmed revoke", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await mockProjectAssets(page);

  await page.goto("/projects/research-lab/credentials");
  await page.getByRole("tab", { name: /项目自建/u }).click();
  await page.getByRole("button", { name: "创建 Credential" }).click();
  const createDialog = page.getByRole("dialog", { name: "创建 Credential" });
  await createDialog.getByLabel("名称").fill("GitHub");
  await createDialog.getByLabel("Credential 标识").fill("github");
  await createDialog.getByLabel("类型").fill("token");
  await createDialog.getByLabel("字段名").fill("TOKEN");
  await createDialog.getByLabel("凭据值").fill("project-secret-sentinel");
  await createDialog.getByRole("button", { name: "加密写入" }).click();
  await expect(page.locator("body")).not.toContainText(
    "project-secret-sentinel",
  );

  await page.getByRole("button", { name: "替换凭据" }).click();
  const replaceDialog = page.getByRole("dialog", { name: "替换凭据" });
  await replaceDialog.getByLabel("凭据值").fill("rotated-project-secret");
  await replaceDialog.getByRole("button", { name: "替换凭据" }).click();
  await expect(page.locator("body")).not.toContainText(
    "rotated-project-secret",
  );
  await expect(page.getByRole("note")).toContainText(
    "既有 Grant 仍固定到 retired version",
  );

  await page.getByRole("button", { name: "迁移兼容 Grant" }).click();
  const migrationDialog = page.getByRole("dialog", {
    name: "迁移 Credential Grant",
  });
  await expect(migrationDialog).toContainText("字段结构完全兼容");
  await migrationDialog.getByRole("button", { name: "确认迁移 Grant" }).click();
  await expect(page.getByRole("status")).toContainText("已完成兼容 Grant 迁移");

  await page.getByRole("button", { name: "撤销凭据" }).click();
  const revokeDialog = page.getByRole("dialog", {
    name: "确认撤销 Credential",
  });
  await expect(revokeDialog).toContainText("此操作不可恢复");
  await revokeDialog.getByRole("button", { name: "取消" }).click();
  await expect(page.getByText("已撤销", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "撤销凭据" }).click();
  await page
    .getByRole("dialog", { name: "确认撤销 Credential" })
    .getByRole("button", { name: "确认永久撤销" })
    .click();
  await expect(page.getByText("已撤销", { exact: true })).toHaveCount(3);
});

test("project asset authoring, approval, Credential safety, and scope switch", async ({
  page,
}) => {
  mockLangGraphAPI(page);
  await page.route("**/api/models", (route) =>
    json(route, {
      models: [
        {
          id: "model-default",
          name: "default",
          model: "provider-model-id",
          display_name: "Default logical model",
          supports_thinking: false,
          supports_reasoning_effort: false,
        },
      ],
      token_usage: { enabled: false },
    }),
  );
  const mock = await mockProjectAssets(page);

  await page.goto("/projects/research-lab/agents");
  await page.getByRole("button", { name: "查看 Analyst 详情" }).click();
  const agentSheet = page.getByRole("dialog", {
    name: "Analyst",
    exact: true,
  });
  await agentSheet.getByRole("button", { name: "创建新版本" }).click();
  const agentVersionDialog = page.getByRole("dialog", {
    name: "创建 Agent 版本",
  });
  await agentVersionDialog.getByLabel("描述").fill("Project agent v1");
  await agentVersionDialog.getByLabel("逻辑模型").selectOption("default");
  await agentVersionDialog.getByRole("button", { name: "创建版本" }).click();
  await expect(agentVersionDialog).toHaveCount(0);
  await agentSheet.getByRole("button", { name: "发布版本" }).click();
  await expect(agentSheet.getByText("已发布", { exact: true })).toBeVisible();
  await agentSheet.getByRole("button", { name: "Close" }).click();

  await page.getByRole("link", { name: "Skill", exact: true }).first().click();
  await page.getByRole("button", { name: "查看 Review Skill 详情" }).click();
  const skillSheet = page.getByRole("dialog", {
    name: "Review Skill",
    exact: true,
  });
  await skillSheet.getByRole("button", { name: "从空白创建" }).click();
  const skillVersionDialog = page.getByRole("dialog", {
    name: "创建 Skill 版本",
  });
  const skillContent = skillVersionDialog.getByLabel("文件内容");
  await expect(skillContent).toHaveValue(
    /^---\nname: review-skill\ndescription: .+\n---\n/u,
  );
  await skillVersionDialog.getByRole("button", { name: "创建版本" }).click();
  await expect(skillVersionDialog).toHaveCount(0);
  await skillSheet.getByRole("button", { name: "发布版本" }).click();
  await expect(skillSheet.getByText("已发布", { exact: true })).toBeVisible();
  await skillSheet.getByRole("button", { name: "Close" }).click();

  await page.getByRole("link", { name: "MCP", exact: true }).first().click();
  await page.getByRole("button", { name: "查看 GitHub MCP 详情" }).click();
  let mcpSheet = page.getByRole("dialog", {
    name: "GitHub MCP",
    exact: true,
  });
  await mcpSheet.getByRole("button", { name: "创建新版本" }).click();
  const mcpVersionDialog = page.getByRole("dialog", {
    name: "创建 MCP 版本",
  });
  await mcpVersionDialog.getByLabel("描述").fill("GitHub MCP");
  await mcpVersionDialog.getByLabel("槽位名称").fill("github-token");
  await mcpVersionDialog.getByLabel("必需字段（逗号或换行分隔）").fill("TOKEN");
  await mcpVersionDialog.getByRole("button", { name: "创建版本" }).click();
  await expect(mcpVersionDialog).toHaveCount(0);
  await expect(mcpSheet.getByRole("button", { name: "发布版本" })).toHaveCount(
    0,
  );
  await mcpSheet.getByRole("button", { name: "提交审批" }).click();
  await expect(mcpSheet.getByText("待审批", { exact: true })).toBeVisible();
  await mcpSheet.getByRole("button", { name: "Close" }).click();

  await page
    .getByRole("link", { name: "Credential", exact: true })
    .first()
    .click();
  await page.getByRole("tab", { name: /项目自建/u }).click();
  await page.getByRole("button", { name: "创建 Credential" }).click();
  await page.getByLabel("名称").fill("GitHub");
  await page.getByLabel("Credential 标识").fill("github");
  await page.getByLabel("类型").fill("token");
  await page.getByLabel("字段名").fill("TOKEN");
  await page.getByLabel("凭据值").fill("never-render-this-secret");
  await page.getByRole("button", { name: "加密写入" }).click();
  await expect(page.locator("body")).not.toContainText(
    "never-render-this-secret",
  );

  await page.getByRole("link", { name: "MCP", exact: true }).first().click();
  await page.getByRole("button", { name: "查看 GitHub MCP 详情" }).click();
  mcpSheet = page.getByRole("dialog", {
    name: "GitHub MCP",
    exact: true,
  });
  await mcpSheet.getByRole("button", { name: "批准并发布" }).click();
  const approvalDialog = page.getByRole("dialog", { name: "批准 MCP 版本" });
  await approvalDialog
    .getByLabel("github-token Credential")
    .selectOption(CREDENTIAL_VERSION_ID);
  await expect(
    approvalDialog.getByLabel("github-token Credential"),
  ).toContainText("GitHub");
  await expect(
    approvalDialog.getByLabel("github-token Credential"),
  ).not.toContainText("System GitHub");
  await approvalDialog.getByRole("button", { name: "批准并发布" }).click();
  await expect(approvalDialog).toHaveCount(0);
  await expect(mcpSheet.getByText("已发布", { exact: true })).toBeVisible();
  await mcpSheet.getByRole("button", { name: "Close" }).click();

  await page
    .getByRole("link", { name: "Credential", exact: true })
    .first()
    .click();
  const credentialCard = page
    .locator('[data-slot="card"]')
    .filter({ has: page.getByText("GitHub", { exact: true }) })
    .filter({ hasText: "github" })
    .first();
  await page.getByRole("button", { name: "替换凭据" }).click();
  await page.getByLabel("字段名").fill("TOKEN");
  await page.getByLabel("凭据值").fill("rotated-secret");
  await page.getByRole("button", { name: "替换凭据" }).last().click();
  await expect(page.locator("body")).not.toContainText("rotated-secret");
  await expect(credentialCard).toContainText(
    "既有 Grant 仍固定到 retired version",
  );
  await page.getByRole("button", { name: "迁移兼容 Grant" }).click();
  const migrationDialog = page.getByRole("dialog", {
    name: "迁移 Credential Grant",
  });
  await expect(migrationDialog).toContainText("字段结构完全兼容");
  await migrationDialog.getByRole("button", { name: "确认迁移 Grant" }).click();
  await expect(page.getByRole("status")).toContainText("已完成兼容 Grant 迁移");

  await page.getByRole("button", { name: "撤销凭据" }).click();
  const revokeDialog = page.getByRole("dialog", {
    name: "确认撤销 Credential",
  });
  await expect(revokeDialog).toContainText("此操作不可恢复");
  await expect(revokeDialog).toContainText("相关 active Grant");
  await revokeDialog.getByRole("button", { name: "取消" }).click();
  await expect(credentialCard.getByText("已撤销", { exact: true })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "撤销凭据" }).click();
  await page
    .getByRole("dialog", { name: "确认撤销 Credential" })
    .getByRole("button", { name: "确认永久撤销" })
    .click();
  await expect(credentialCard.getByText("已撤销", { exact: true })).toHaveCount(
    3,
  );

  const staleStatus = await page.evaluate(
    async ({ projectId, assetId }) =>
      fetch(`/api/projects/${projectId}/agents/${assetId}/versions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_asset_version: 2 }),
      }).then((response) => response.status),
    { projectId: PROJECT_ID, assetId: PROJECT_AGENT_ID },
  );
  expect(staleStatus).toBe(409);

  await page.goto("/projects/second-lab/agents");
  await expect(
    page.getByText("Second Lab", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText("Analyst", { exact: true })).toHaveCount(0);
  expect(mock.staleConflicts()).toBe(1);
  expect(mock.validatedMutations()).toBe(10);
  for (const forbidden of ["运行 Agent", "开始对话", "立即运行"]) {
    await expect(page.getByText(forbidden, { exact: true })).toHaveCount(0);
  }
});
