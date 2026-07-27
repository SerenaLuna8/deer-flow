import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test, rs } from "@rstest/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { headers } from "next/headers";
import {
  createElement,
  type ComponentProps,
  type ComponentType,
  type PropsWithChildren,
} from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("next/navigation", () => ({
  notFound: rs.fn(() => {
    throw Object.assign(new Error("Not found"), { code: "NEXT_NOT_FOUND" });
  }),
  redirect: rs.fn((destination: string) => {
    throw Object.assign(new Error("Redirect"), {
      code: "NEXT_REDIRECT",
      destination,
    });
  }),
  usePathname: () => "/admin/assets/agents",
  useRouter: () => ({ push: rs.fn() }),
}));
rs.mock("next/headers", () => ({ headers: rs.fn() }));
rs.mock("@/core/auth/server", () => ({ getServerSideUser: rs.fn() }));
rs.mock("@/core/static-mode", () => ({ isStaticWebsiteOnly: () => false }));

import AdminLayout from "@/app/admin/layout";
import {
  skillMarkdownTemplate,
  McpVersionFields,
  SkillVersionFields,
  versionInput,
} from "@/components/admin/assets/admin-asset-dialogs";
import {
  AdminAssetPage,
  CredentialWriteError,
  CredentialMetadataCard,
  adminAssetErrorMessage,
  assetLifecycleActions,
  versionWorkflowActions,
} from "@/components/admin/assets/admin-asset-page";
import {
  AdminAssetsNavigation,
  AdminAssetsShell,
} from "@/components/admin/assets/admin-assets-shell";
import { AdminProjectAssetsShell } from "@/components/admin/assets/admin-project-assets-shell";
import { AssetVersionDiff } from "@/components/assets/asset-version-diff";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { sharedAssetKeys, SharedAssetApiError } from "@/core/shared-assets";

describe("admin asset access and credential safety", () => {
  test("server layout returns 404 for an authenticated ordinary user", async () => {
    rs.mocked(getServerSideUser).mockResolvedValue({
      tag: "authenticated",
      user: {
        id: "ordinary-user",
        email: "member@example.com",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      },
    });

    await expect(
      AdminLayout({ children: createElement("p", null, "restricted") }),
    ).rejects.toMatchObject({ code: "NEXT_NOT_FOUND" });
  });

  test("server layout preserves the admin target for unauthenticated users", async () => {
    rs.mocked(getServerSideUser).mockResolvedValue({ tag: "unauthenticated" });
    rs.mocked(headers).mockResolvedValue(
      new Headers({
        "x-deerflow-admin-return-path": "/admin/assets/agents?scope=system",
      }) as never,
    );

    await expect(
      AdminLayout({ children: createElement("p", null, "restricted") }),
    ).rejects.toMatchObject({
      code: "NEXT_REDIRECT",
      destination: "/login?next=%2Fadmin%2Fassets%2Fagents%3Fscope%3Dsystem",
    });
  });

  test("navigation exposes exactly the four platform asset areas", () => {
    const html = renderToStaticMarkup(
      createElement(AdminAssetsNavigation, {
        pathname: "/admin/assets/agents",
      }),
    );

    for (const [href, label] of [
      ["/admin/assets/agents", "Agent"],
      ["/admin/assets/skills", "Skill"],
      ["/admin/assets/mcp", "MCP"],
      ["/admin/assets/credentials", "Credential"],
    ]) {
      expect(html).toContain(`href="${href}"`);
      expect(html).toContain(label);
    }
  });

  test("asset routes add only a wrapping sub-navigation without a second app shell", () => {
    const html = renderToStaticMarkup(
      createElement(
        AdminAssetsShell,
        null,
        createElement("main", null, "assets"),
      ),
    );

    expect(html).toContain('data-testid="admin-assets-shell"');
    expect(html).toContain("平台资产导航");
    expect(html).not.toContain("退出登录");
    expect(html).not.toContain("overflow-x-auto");
    expect(html.match(/<header/g) ?? []).toHaveLength(0);
  });

  test("admin project override shell keeps project selection explicit and responsive", () => {
    const projectId = "33333333-3333-4333-8333-333333333333";
    const html = renderToStaticMarkup(
      createElement(
        AdminProjectAssetsShell,
        { projectId } as ComponentProps<typeof AdminProjectAssetsShell>,
        createElement("main", null, "override"),
      ),
    );

    expect(html).toContain('data-testid="admin-project-assets-shell"');
    expect(html).toContain("返回项目选择");
    expect(html).toContain(projectId);
    expect(html).toContain("不会读取成员、聊天、运行、记忆、文件");
    expect(html).toContain("grid-cols-2");
    for (const [segment, label] of [
      ["agents", "Agent"],
      ["skills", "Skill"],
      ["mcp", "MCP"],
      ["credentials", "Credential"],
    ]) {
      expect(html).toContain(
        `href="/admin/projects/${projectId}/assets/${segment}"`,
      );
      expect(html).toContain(label);
    }
  });

  test("admin project override never falls back to member routes or global system authoring", () => {
    const pageSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-project-asset-page.tsx",
      ),
      "utf8",
    );
    const apiSource = readFileSync(
      resolve(process.cwd(), "src/core/shared-assets/api.ts"),
      "utf8",
    );

    expect(pageSource).toContain("useAdminProjectAssets");
    expect(pageSource).toContain("createAdminProjectCredential");
    expect(pageSource).toContain("AdminProjectSystemBindingDialog");
    expect(pageSource).not.toContain("useCurrentProject");
    expect(pageSource).not.toContain("createProjectCredential(");
    expect(pageSource).not.toContain("useMutation(");
    expect(pageSource).toContain("approve.mutateAsync");
    expect(pageSource).toContain("settleMcpApproval");
    expect(pageSource).toContain("approvalError={approve.error}");
    expect(apiSource).toContain(
      "/api/admin/projects/${parsedProjectId}/assets/${parsedKind}",
    );
    expect(apiSource).not.toContain("createAdminAsset(projectId");
  });

  test("does not construct an asset query while the auth user is null", () => {
    const queryClient = new QueryClient();
    const accountKey = rs.spyOn(sharedAssetKeys, "account");
    const TestAuthProvider = AuthProvider as ComponentType<
      PropsWithChildren<{ initialUser: null }>
    >;

    expect(() =>
      renderToStaticMarkup(
        createElement(
          QueryClientProvider,
          { client: queryClient },
          createElement(
            TestAuthProvider,
            { initialUser: null },
            createElement(AdminAssetPage, { kind: "agents" }),
          ),
        ),
      ),
    ).not.toThrow();
    expect(accountKey).not.toHaveBeenCalled();

    accountKey.mockRestore();
  });

  test("authoring fields use Chinese labels for ordinary UI terms", () => {
    const html = [
      renderToStaticMarkup(
        createElement(SkillVersionFields, { assetSlug: "review-skill" }),
      ),
      renderToStaticMarkup(createElement(McpVersionFields)),
    ].join("\n");

    for (const label of [
      "媒体类型",
      "传输方式",
      "URL",
      "Credential 槽位",
      "凭据字段分组",
    ]) {
      expect(html).toContain(label);
    }
    for (const english of [
      "Model reference",
      "Tool groups",
      "Skill version IDs",
      "Media type",
      "Transport",
      "Command",
      "Credential slot",
      "Payload 分组",
    ]) {
      expect(html).not.toContain(english);
    }
  });

  test("project MCP authoring only offers supported remote transports", () => {
    const html = renderToStaticMarkup(createElement(McpVersionFields));

    expect(html).toContain('<option value="sse"');
    expect(html).toContain('<option value="http"');
    expect(html).toContain('name="url"');
    expect(html).toContain('required=""');
    expect(html).toContain("Worker 访问");
    expect(html).toContain("平台批准的精确 HTTPS 地址");
    expect(html).toContain("实际超时由平台控制");
    expect(html).not.toContain('<option value="stdio"');
    expect(html).not.toContain('<option value="streamable_http"');
    expect(html).not.toContain('<option value="env"');
    expect(html).not.toContain('<option value="oauth"');
    expect(html).not.toContain('name="command"');
    expect(html).not.toContain('name="args"');
    expect(html).not.toContain('name="timeout_seconds"');
  });

  test("project MCP version input clears stale local-process fields", () => {
    const form = new FormData();
    form.set("description", "Remote MCP");
    form.set("transport", "sse");
    form.set("url", " https://mcp.example.test/sse ");
    form.set("command", "npx");
    form.set("args", "--yes,server");
    form.set("timeout_seconds", "999");
    form.set("slot_name", "api-token");
    form.set("slot_group", "oauth");
    form.set("slot_fields", "Authorization");

    expect(versionInput("mcp-servers", form, 4)).toMatchObject({
      description: "Remote MCP",
      transport: "sse",
      url: "https://mcp.example.test/sse",
      command: null,
      args: [],
      timeout_seconds: 30,
      credential_slots: [
        {
          name: "api-token",
          payload_schema: { headers: ["Authorization"] },
        },
      ],
      expected_asset_version: 4,
    });
  });

  test("blank Skill authoring provides a valid immutable SKILL.md envelope", () => {
    const template = skillMarkdownTemplate("review-skill");
    const html = renderToStaticMarkup(
      createElement(SkillVersionFields, { assetSlug: "review-skill" }),
    );

    expect(template).toContain("---\nname: review-skill\n");
    expect(template).toMatch(/\ndescription: .+\n---\n/u);
    expect(html).toContain("SKILL.md");
    expect(html).toContain("text/markdown");
    expect(html).toContain("name: review-skill");
    expect(html).not.toContain('name="path"');
    expect(html).not.toContain('name="media_type"');
  });

  test("project Agent authoring has no manual version surface", () => {
    const pageSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-project-asset-page.tsx",
      ),
      "utf8",
    );
    const dialogSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-asset-dialogs.tsx",
      ),
      "utf8",
    );

    expect(dialogSource).not.toContain("AgentVersionFields");
    expect(dialogSource).not.toContain('kind === "agents"');
    expect(pageSource).toContain('kind !== "agents"');
  });

  test("credential card exposes metadata actions without secret reveal or copy", () => {
    const html = renderToStaticMarkup(
      createElement(CredentialMetadataCard, {
        credential: {
          id: "11111111-1111-4111-8111-111111111111",
          scope: "system",
          project_id: null,
          name: "github-token",
          display_name: "GitHub Token",
          credential_type: "token",
          status: "active",
          current_version_id: "22222222-2222-4222-8222-222222222222",
          version: 3,
          created_by_user_id: "admin-user",
          created_at: "2026-07-13T08:00:00+00:00",
          updated_at: "2026-07-13T09:00:00+00:00",
        },
        onReplace: () => undefined,
        onRevoke: () => undefined,
        onMigrate: () => undefined,
        onDelete: () => undefined,
      }),
    );

    expect(html).toContain("GitHub Token");
    expect(html).toContain("替换凭据");
    expect(html).toContain("撤销凭据");
    expect(html).toContain("迁移兼容引用");
    expect(html).toContain(">删除<");
    expect(html).toContain(
      "既有 MCP Grant 与 Skill 环境变量绑定仍固定到旧版本",
    );
    expect(html).not.toContain("显示明文");
    expect(html).not.toContain("复制密钥");
    expect(html).not.toContain("plaintext");
  });

  test("credential write failures remain visible outside secret dialogs", () => {
    const html = renderToStaticMarkup(
      createElement(CredentialWriteError, {
        message: "操作失败，请稍后重试。",
      }),
    );

    expect(html).toContain('role="alert"');
    expect(html).toContain("操作失败，请稍后重试。");
    expect(html).not.toContain("private backend detail");
  });

  test("credential writes bypass TanStack mutation cache and clear the secret form", () => {
    const pageSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-asset-page.tsx",
      ),
      "utf8",
    );
    const dialogSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-asset-dialogs.tsx",
      ),
      "utf8",
    );

    expect(pageSource).toContain("createAdminCredential(input)");
    expect(pageSource).toContain(
      "replaceAdminCredential(credential.id, input)",
    );
    expect(pageSource).not.toContain("useCreateAdminCredential");
    expect(pageSource).not.toContain("useReplaceAdminCredential");
    expect(pageSource).not.toContain("useMutation(");
    expect(dialogSource).toContain('type="password"');
    expect(dialogSource).toContain("event.currentTarget.reset()");
    expect(dialogSource).toContain("CredentialRevokeDialog");
    expect(pageSource).toContain("setRevokeOpen(true)");
    expect(pageSource).toContain("migrateAdminCredentialGrants");
  });

  test("system Agent Skill and MCP pages are packaged-catalog read only", () => {
    const pageSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-asset-page.tsx",
      ),
      "utf8",
    );
    const apiSource = readFileSync(
      resolve(process.cwd(), "src/core/shared-assets/api.ts"),
      "utf8",
    );
    const hooksSource = readFileSync(
      resolve(process.cwd(), "src/core/shared-assets/hooks.ts"),
      "utf8",
    );

    expect(pageSource).toContain("packaged catalog");
    expect(pageSource).toContain("运行期只读");
    for (const name of [
      "createAdminAsset",
      "createAdminAssetVersion",
      "changeAdminAssetStatus",
      "publishAdminAssetVersion",
      "submitAdminMcpVersion",
      "approveAdminMcpVersion",
    ]) {
      expect(pageSource).not.toContain(name);
      expect(apiSource).not.toContain(`function ${name}`);
      expect(hooksSource).not.toContain(name);
    }
    expect(pageSource).toContain("createAdminCredential(input)");
    expect(apiSource).toContain("function createAdminCredential");
  });

  test("lifecycle and workflow controls follow server state", () => {
    expect(assetLifecycleActions("active")).toEqual(["archive", "suspend"]);
    expect(assetLifecycleActions("archived")).toEqual(["suspend"]);
    expect(assetLifecycleActions("suspended")).toEqual(["archive"]);

    expect(versionWorkflowActions("agents", "draft", false)).toEqual([
      "publish",
    ]);
    expect(versionWorkflowActions("skills", "published", false)).toEqual([]);
    expect(versionWorkflowActions("mcp-servers", "draft", false)).toEqual([
      "publish",
    ]);
  });

  test("MCP versions with credential slots can only use approval workflow", () => {
    expect(versionWorkflowActions("mcp-servers", "draft", true)).toEqual([
      "submit",
    ]);
    expect(
      versionWorkflowActions("mcp-servers", "pending_approval", true),
    ).toEqual(["approve"]);
    expect(
      versionWorkflowActions("mcp-servers", "pending_approval", true),
    ).not.toContain("publish");
  });

  test("diff presents checksums and file metadata without secret fields", () => {
    const html = renderToStaticMarkup(
      createElement(AssetVersionDiff, {
        previous: {
          id: "11111111-1111-4111-8111-111111111111",
          skill_id: "22222222-2222-4222-8222-222222222222",
          version_number: 1,
          workflow_status: "published",
          description: "Old",
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
              size_bytes: 10,
              sha256: "old-checksum",
            },
          ],
          supersedes_version_id: null,
          payload_checksum: "old-payload-checksum",
          created_by_user_id: "admin",
          created_at: "2026-07-13T08:00:00+00:00",
        },
        current: {
          id: "33333333-3333-4333-8333-333333333333",
          skill_id: "22222222-2222-4222-8222-222222222222",
          version_number: 2,
          workflow_status: "draft",
          description: "New",
          frontmatter: {},
          compatibility: null,
          secret_requirements: [{ name: "TOKEN", optional: false }],
          scan_decision: "warn",
          scan_rule_ids: ["rule-1"],
          scan_summary: {},
          file_views: [
            {
              path: "SKILL.md",
              media_type: "text/markdown",
              size_bytes: 12,
              sha256: "new-checksum",
            },
          ],
          supersedes_version_id: "11111111-1111-4111-8111-111111111111",
          payload_checksum: "new-payload-checksum",
          created_by_user_id: "admin",
          created_at: "2026-07-13T09:00:00+00:00",
        },
      }),
    );

    expect(html).toContain("new-payload-checksum");
    expect(html).toContain("载荷校验和");
    expect(html).toContain("SKILL.md");
    expect(html).toContain("new-checksum");
    expect(html).not.toContain("plaintext");
    expect(html).not.toContain("ciphertext");
    expect(html).not.toContain("复制密钥");
    expect(html).not.toContain("Payload checksum");
  });

  test("Agent runtime diff does not expose independently edited profile documents", () => {
    const html = renderToStaticMarkup(
      createElement(AssetVersionDiff, {
        current: {
          id: "11111111-1111-4111-8111-111111111111",
          agent_id: "22222222-2222-4222-8222-222222222222",
          version_number: 2,
          workflow_status: "published",
          description: "Runtime config",
          agents_instructions: "agents-profile-sentinel",
          soul: "soul-profile-sentinel",
          identity: "identity-profile-sentinel",
          user_context: "user-profile-sentinel",
          payload_schema_version: 2,
          model_ref: "default",
          tool_groups: ["web"],
          skill_version_ids: [],
          mcp_version_ids: [],
          supersedes_version_id: null,
          payload_checksum: "agent-payload-checksum",
          created_by_user_id: "admin",
          created_at: "2026-07-13T09:00:00+00:00",
        },
      }),
    );

    expect(html).toContain("Runtime config");
    expect(html).toContain("default");
    expect(html).not.toContain("agents-profile-sentinel");
    expect(html).not.toContain("soul-profile-sentinel");
    expect(html).not.toContain("identity-profile-sentinel");
    expect(html).not.toContain("user-profile-sentinel");
    expect(html).not.toContain("角色设定（Soul）");
  });

  test("credential diff translates payload metadata labels", () => {
    const html = renderToStaticMarkup(
      createElement(AssetVersionDiff, {
        current: {
          id: "11111111-1111-4111-8111-111111111111",
          credential_id: "22222222-2222-4222-8222-222222222222",
          version_number: 1,
          status: "active",
          payload_schema_version: 1,
          payload_schema: { env: ["TOKEN"] },
          supersedes_version_id: null,
          created_by_user_id: "admin",
          created_at: "2026-07-13T08:00:00+00:00",
        },
      }),
    );

    expect(html).toContain("载荷结构版本");
    expect(html).toContain("载荷字段");
    expect(html).not.toContain("Payload schema version");
    expect(html).not.toContain("Payload fields");
  });

  test("maps API failures to safe Chinese public messages", () => {
    expect(
      adminAssetErrorMessage(
        new SharedAssetApiError(409, "ASSET_CONFLICT", "Asset state conflict"),
      ),
    ).toBe("资产状态已变化，请刷新后重试。");
    expect(
      adminAssetErrorMessage(
        new SharedAssetApiError(
          429,
          "ASSET_STORAGE_QUOTA_EXCEEDED",
          "Project Skill storage quota exceeded",
        ),
      ),
    ).toBe("项目 Skill 存储配额已用尽，请清理不再需要的 Skill 后重试。");
    expect(adminAssetErrorMessage(new Error("private backend detail"))).toBe(
      "操作失败，请稍后重试。",
    );
  });
});
