import fs from "node:fs";
import path from "node:path";

import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  McpApprovalForm,
  McpCredentialSelectors,
} from "@/components/projects/assets/mcp-approval-dialog";
import {
  ProjectAssetCatalogView,
  ProjectAssetHistoryView,
  ProjectCredentialCatalogView,
  projectAssetCanAuthor,
  projectAssetLifecycleActions,
  projectCredentialShowsHistory,
} from "@/components/projects/assets/project-assets-page";
import { canManageSystemBinding } from "@/components/projects/assets/system-asset-section";
import { canMoveSystemBinding } from "@/components/projects/assets/system-binding-dialog";
import type {
  AssetVersion,
  ProjectAssetList,
  ProjectCredentialList,
} from "@/core/shared-assets";

const PROJECT_ID = "33333333-3333-4333-8333-333333333333";
const SYSTEM_ID = "11111111-1111-4111-8111-111111111111";
const PROJECT_ASSET_ID = "22222222-2222-4222-8222-222222222222";
const VERSION_ID = "44444444-4444-4444-8444-444444444444";
const MCP_VERSION_ID = "55555555-5555-4555-8555-555555555555";
const SLOT_ID = "66666666-6666-4666-8666-666666666666";

const base = {
  slug: "analyst",
  display_name: "Analyst",
  status: "active" as const,
  current_published_version_id: VERSION_ID,
  version: 2,
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
  updated_at: "2026-07-14T00:00:00Z",
};

const adminData: ProjectAssetList = {
  system_items: [
    {
      ...base,
      id: SYSTEM_ID,
      scope: "system",
      project_id: null,
      capabilities: [
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.manage_bindings",
      ],
      binding: {
        project_id: PROJECT_ID,
        kind: "agent",
        asset_id: SYSTEM_ID,
        version_id: VERSION_ID,
        enabled: true,
        version: 3,
        created_by_user_id: "user-1",
        updated_by_user_id: "user-1",
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:00Z",
      },
    },
  ],
  project_items: [
    {
      ...base,
      id: PROJECT_ASSET_ID,
      scope: "project",
      project_id: PROJECT_ID,
      capabilities: [
        "shared_assets.read",
        "shared_assets.execute",
        "shared_assets.edit",
      ],
      binding: null,
    },
  ],
  request_id: "req-assets",
};

const pendingMcpVersion: AssetVersion = {
  id: MCP_VERSION_ID,
  mcp_server_id: PROJECT_ASSET_ID,
  version_number: 1,
  workflow_status: "pending_approval",
  definition: {
    description: "GitHub MCP",
    transport: "http",
    command: null,
    args: [],
    url: "https://mcp.example.test",
    env: {},
    headers: {},
    oauth: {},
    routing: {},
    tool_overrides: {},
    timeout_seconds: 30,
    credential_slots: [
      {
        name: "github-token",
        purpose: "GitHub token",
        payload_schema: { env: ["TOKEN"] },
        required: true,
      },
    ],
  },
  credential_slots: [
    {
      id: SLOT_ID,
      name: "github-token",
      purpose: "GitHub token",
      payload_schema: { env: ["TOKEN"] },
      required: true,
    },
  ],
  credential_grants: [],
  supersedes_version_id: null,
  payload_checksum: "a".repeat(64),
  submitted_at: "2026-07-14T00:00:00Z",
  reviewed_at: null,
  reviewed_by_user_id: null,
  created_by_user_id: "user-1",
  created_at: "2026-07-14T00:00:00Z",
};

describe("project shared asset pages", () => {
  test("keeps same-name system and project assets separate with source badges", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetCatalogView kind="agents" data={adminData} />,
    );

    expect(html.match(/Analyst/g)).toHaveLength(2);
    expect(html).toContain("系统");
    expect(html).toContain("项目");
    expect(html).toContain("固定版本");
  });

  test("uses item capabilities for binding and editing actions without run CTA", () => {
    const html = renderToStaticMarkup(
      <ProjectAssetCatalogView kind="agents" data={adminData} />,
    );
    const readOnly = renderToStaticMarkup(
      <ProjectAssetCatalogView
        kind="agents"
        data={{
          ...adminData,
          system_items: adminData.system_items.map((item) => ({
            ...item,
            capabilities: ["shared_assets.read"],
          })),
          project_items: adminData.project_items.map((item) => ({
            ...item,
            capabilities: ["shared_assets.read"],
          })),
        }}
      />,
    );

    expect(html).toContain("管理绑定");
    expect(html).toContain("创建新版本");
    expect(readOnly).not.toContain("管理绑定");
    expect(readOnly).not.toContain("创建新版本");
    for (const forbidden of ["运行 Agent", "开始对话", "立即运行"]) {
      expect(html).not.toContain(forbidden);
    }
  });

  test("shows lifecycle state and blocks authoring or new bindings for inactive assets", () => {
    const archivedSystem = {
      ...adminData.system_items[0]!,
      status: "archived" as const,
      binding: null,
    };
    const suspendedProject = {
      ...adminData.project_items[0]!,
      status: "suspended" as const,
    };
    const html = renderToStaticMarkup(
      <ProjectAssetCatalogView
        kind="agents"
        data={{
          ...adminData,
          system_items: [archivedSystem],
          project_items: [suspendedProject],
        }}
      />,
    );

    expect(html).toContain("已归档");
    expect(html).toContain("已暂停");
    expect(html).not.toContain("创建新版本");
    expect(html).not.toContain("管理绑定");
    expect(projectAssetCanAuthor(suspendedProject)).toBe(false);
    expect(projectAssetLifecycleActions(suspendedProject)).toEqual(["archive"]);
    expect(
      projectAssetLifecycleActions({
        ...suspendedProject,
        capabilities: ["shared_assets.read"],
      }),
    ).toEqual([]);
    expect(canManageSystemBinding(archivedSystem)).toBe(false);
    expect(
      canManageSystemBinding({
        ...archivedSystem,
        binding: adminData.system_items[0]!.binding,
      }),
    ).toBe(true);
    expect(canMoveSystemBinding(archivedSystem)).toBe(false);
    expect(canMoveSystemBinding(adminData.system_items[0]!)).toBe(true);
  });

  test("renders Viewer Runner Editor and Admin actions through the production history path", () => {
    const item = adminData.project_items[0]!;
    const callbacks = {
      onPublish: () => undefined,
      onSubmit: () => undefined,
      onApprove: () => undefined,
      onChangeStatus: () => undefined,
    };
    const viewer = renderToStaticMarkup(
      <ProjectAssetHistoryView
        kind="mcp-servers"
        item={{ ...item, capabilities: ["shared_assets.read"] }}
        versions={[pendingMcpVersion]}
        {...callbacks}
      />,
    );
    const runner = renderToStaticMarkup(
      <ProjectAssetHistoryView
        kind="mcp-servers"
        item={{
          ...item,
          capabilities: ["shared_assets.read", "shared_assets.execute"],
        }}
        versions={[pendingMcpVersion]}
        {...callbacks}
      />,
    );
    const editorPending = renderToStaticMarkup(
      <ProjectAssetHistoryView
        kind="mcp-servers"
        item={{
          ...item,
          capabilities: ["shared_assets.read", "shared_assets.edit"],
        }}
        versions={[pendingMcpVersion]}
        {...callbacks}
      />,
    );
    const editorDraft = renderToStaticMarkup(
      <ProjectAssetHistoryView
        kind="mcp-servers"
        item={{
          ...item,
          capabilities: ["shared_assets.read", "shared_assets.edit"],
        }}
        versions={[{ ...pendingMcpVersion, workflow_status: "draft" }]}
        {...callbacks}
      />,
    );
    const admin = renderToStaticMarkup(
      <ProjectAssetHistoryView
        kind="mcp-servers"
        item={{
          ...item,
          capabilities: [
            "shared_assets.read",
            "shared_assets.edit",
            "shared_assets.manage_bindings",
            "mcp.credentials.approve",
          ],
        }}
        versions={[pendingMcpVersion]}
        {...callbacks}
      />,
    );

    for (const readOnly of [viewer, runner]) {
      expect(readOnly).not.toContain("发布版本");
      expect(readOnly).not.toContain("提交审批");
      expect(readOnly).not.toContain("批准并发布");
      expect(readOnly).not.toContain(">归档<");
      expect(readOnly).not.toContain(">暂停<");
    }
    expect(editorPending).toContain("等待 Admin 审批");
    expect(editorPending).not.toContain("批准并发布");
    expect(editorPending).not.toContain("发布版本");
    expect(editorDraft).toContain("提交审批");
    expect(editorDraft).not.toContain("发布版本");
    expect(admin).toContain("批准并发布");
    expect(admin).toContain(">归档<");
    expect(admin).toContain(">暂停<");
  });

  test("Credential view exposes only metadata and secure capability actions", () => {
    const data: ProjectCredentialList = {
      system_items: [],
      project_items: [
        {
          id: PROJECT_ASSET_ID,
          scope: "project",
          project_id: PROJECT_ID,
          name: "github",
          display_name: "GitHub",
          credential_type: "token",
          status: "active",
          current_version_id: VERSION_ID,
          version: 1,
          created_by_user_id: "user-1",
          created_at: "2026-07-14T00:00:00Z",
          updated_at: "2026-07-14T00:00:00Z",
          capabilities: ["shared_assets.read", "mcp.credentials.approve"],
        },
      ],
      request_id: "req-credentials",
    };
    const html = renderToStaticMarkup(
      <ProjectCredentialCatalogView data={data} />,
    );

    expect(html).toContain("替换凭据");
    expect(html).toContain("撤销凭据");
    expect(html).not.toContain("显示明文");
    expect(html).not.toContain("复制密钥");
    expect(html).not.toContain("ciphertext");
  });

  test("MCP approval selector keeps project and system Credential scopes exact", () => {
    const credentials = [
      {
        id: "77777777-7777-4777-8777-777777777777",
        scope: "system" as const,
        name: "system-github",
        display_name: "System GitHub",
        credential_type: "token",
        status: "active",
        current_version_id: "88888888-8888-4888-8888-888888888888",
      },
      {
        id: "99999999-9999-4999-8999-999999999999",
        scope: "project" as const,
        name: "project-github",
        display_name: "Project GitHub",
        credential_type: "token",
        status: "active",
        current_version_id: VERSION_ID,
      },
    ];
    const projectHtml = renderToStaticMarkup(
      <McpCredentialSelectors
        version={pendingMcpVersion}
        credentialScope="project"
        credentials={credentials}
      />,
    );
    const systemHtml = renderToStaticMarkup(
      <McpCredentialSelectors
        version={pendingMcpVersion}
        credentialScope="system"
        credentials={credentials}
      />,
    );

    expect(projectHtml).toContain("Project GitHub");
    expect(projectHtml).toContain(VERSION_ID);
    expect(projectHtml).not.toContain("System GitHub");
    expect(systemHtml).toContain("System GitHub");
    expect(systemHtml).toContain("88888888-8888-4888-8888-888888888888");
    expect(systemHtml).not.toContain("Project GitHub");
  });

  test("MCP approval form permits empty optional slots but blocks empty required slots", () => {
    const optionalVersion = {
      ...pendingMcpVersion,
      definition: {
        ...pendingMcpVersion.definition,
        credential_slots: pendingMcpVersion.definition.credential_slots.map(
          (slot) => ({ ...slot, required: false }),
        ),
      },
      credential_slots: pendingMcpVersion.credential_slots.map((slot) => ({
        ...slot,
        required: false,
      })),
    };
    const optionalHtml = renderToStaticMarkup(
      <McpApprovalForm
        version={optionalVersion}
        pending={false}
        credentials={[]}
        credentialScope="project"
        onApprove={() => undefined}
      />,
    );
    const requiredHtml = renderToStaticMarkup(
      <McpApprovalForm
        version={pendingMcpVersion}
        pending={false}
        credentials={[]}
        credentialScope="project"
        onApprove={() => undefined}
      />,
    );

    expect(optionalHtml).toContain("可选槽位可留空并直接批准");
    expect(optionalHtml).not.toContain('disabled=""');
    expect(requiredHtml).toContain("必填槽位没有可用 Credential");
    expect(requiredHtml).toContain('required=""');
    expect(requiredHtml).toContain('disabled=""');
  });

  test("MCP approval form distinguishes Credential loading and safe retryable errors", () => {
    const loadingHtml = renderToStaticMarkup(
      <McpApprovalForm
        version={pendingMcpVersion}
        pending={false}
        credentials={[]}
        credentialScope="project"
        credentialsLoading
        onApprove={() => undefined}
      />,
    );
    const errorHtml = renderToStaticMarkup(
      <McpApprovalForm
        version={pendingMcpVersion}
        pending={false}
        credentials={[]}
        credentialScope="project"
        credentialsError={new Error("secret-token-must-not-render")}
        onRetryCredentials={() => undefined}
        onApprove={() => undefined}
      />,
    );

    expect(loadingHtml).toContain("正在加载 Credential");
    expect(loadingHtml).not.toContain("没有可用 Credential");
    expect(loadingHtml).toContain('disabled=""');
    expect(errorHtml).toContain("Credential 列表加载失败，请重试");
    expect(errorHtml).toContain("重试");
    expect(errorHtml).not.toContain("secret-token-must-not-render");
    expect(errorHtml).not.toContain("没有可用 Credential");
    expect(errorHtml).toContain('disabled=""');
  });

  test("system Credential stays metadata-only while project Credential can show safe history", () => {
    expect(projectCredentialShowsHistory({ scope: "system" })).toBe(false);
    expect(projectCredentialShowsHistory({ scope: "project" })).toBe(true);
  });

  test("project Credential writes bypass TanStack mutation variables", () => {
    const source = fs.readFileSync(
      path.join(
        process.cwd(),
        "src/components/projects/assets/project-assets-page.tsx",
      ),
      "utf8",
    );

    expect(source).toContain("createProjectCredential(project.id, input)");
    expect(source).toContain(
      "replaceProjectCredential(project.id, credential.id, input)",
    );
    expect(source).not.toContain("useCreateProjectCredential");
    expect(source).not.toContain("useReplaceProjectCredential");
    expect(source).not.toContain("useMutation(");
  });

});
