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
  type ReactNode,
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
  AdminTechnicalValue,
  AdminAssetDesktopInspector,
  AdminAssetPage,
  CredentialWriteError,
  CredentialMetadataCard,
  VersionTimeline,
  adminAssetErrorMessage,
  assetLifecycleActions,
  buildMcpCredentialGrantInput,
  filterAdminCatalogItems,
  initialMcpCredentialSelections,
  versionWorkflowActions,
} from "@/components/admin/assets/admin-asset-page";
import {
  AdminAssetsNavigation,
  AdminAssetsShell,
} from "@/components/admin/assets/admin-assets-shell";
import { filterAdminProjectDirectoryItems } from "@/components/admin/assets/admin-project-asset-page";
import { AdminProjectAssetsShell } from "@/components/admin/assets/admin-project-assets-shell";
import { CredentialRotationStatusCard } from "@/components/admin/assets/credential-rotation-status";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { AssetVersionDiff } from "@/components/assets/asset-version-diff";
import { AssetVersionHistory } from "@/components/assets/asset-version-history";
import { CredentialDeleteConfirmation } from "@/components/projects/assets/credential-delete-dialog";
import { ProjectAssetCatalogView } from "@/components/projects/assets/project-assets-page";
import { Dialog } from "@/components/ui/dialog";
import { ADMIN_RETURN_PATH_HEADER } from "@/core/auth/admin-return-path";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { I18nProvider } from "@/core/i18n/context";
import type { Locale } from "@/core/i18n/locale";
import {
  sharedAssetKeys,
  SharedAssetApiError,
  type AssetVersion,
} from "@/core/shared-assets";

function renderLocalized(
  children: ReactNode,
  locale: Locale = "zh-CN",
): string {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale}>{children}</I18nProvider>,
  );
}

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
        [ADMIN_RETURN_PATH_HEADER]: "/admin/assets/agents?scope=system",
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
    const html = renderLocalized(
      createElement(AdminAssetsNavigation, {
        pathname: "/admin/assets/agents",
      }),
    );

    for (const [href, label] of [
      ["/admin/assets/agents", "Agent"],
      ["/admin/assets/skills", "Skill"],
      ["/admin/assets/mcp", "MCP"],
      ["/admin/assets/credentials", "凭据"],
    ]) {
      expect(html).toContain(`href="${href}"`);
      expect(html).toContain(label);
    }
    expect(html).toContain('data-variant="line"');
    expect(html).toContain("border-primary");
    expect(html).not.toContain("bg-primary text-primary-foreground");
  });

  test("asset routes keep one compact line navigation without a duplicate catalog masthead", () => {
    const html = renderLocalized(
      createElement(
        AdminAssetsShell,
        null,
        createElement("main", null, "assets"),
      ),
    );

    expect(html).toContain('data-testid="admin-assets-shell"');
    expect(html).toContain("平台资产导航");
    expect(html).not.toContain('data-testid="admin-assets-context"');
    expect(html).not.toContain("系统定义运行期只读");
    expect(html).not.toContain("凭据仅支持受控写入");
    expect(html).not.toContain("退出登录");
    expect(html).not.toContain("overflow-x-auto");
    expect(html.match(/<header/g) ?? []).toHaveLength(0);
  });

  test("platform asset tabs and content share one responsive page frame", () => {
    const shellSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-assets-shell.tsx",
      ),
      "utf8",
    );
    const pageSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-asset-page.tsx",
      ),
      "utf8",
    );

    expect(shellSource).toContain(
      'className="mx-auto max-w-[96rem] px-4 sm:px-5 lg:px-6"',
    );
    expect(pageSource).toContain('<AdminPage className="max-w-[96rem]">');
    expect(pageSource).not.toContain(
      '<AdminPage className="mr-0 ml-auto max-w-[120rem]">',
    );
  });

  test("system asset catalog implements truthful summary, dense table, filters, and pagination", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-asset-page.tsx",
      ),
      "utf8",
    );

    expect(source).toContain('data-testid="admin-asset-summary"');
    expect(source).toContain('data-testid="admin-asset-table"');
    expect(source).toContain('data-testid="admin-asset-publication-filter"');
    expect(source).toContain('data-testid="admin-asset-pagination"');
    expect(source).toContain("adminAssetCatalogSummary");
    expect(source).toContain("filterAndSortAdminAssets");
    expect(source).toContain("adminAssetCatalogPage");
    expect(source).toContain('"divide-border divide-y @min-[52rem]:hidden"');
    expect(source).toContain(
      '"hidden min-w-0 overflow-x-auto @min-[52rem]:block"',
    );
    expect(source).toContain('"divide-border divide-y @min-[48rem]:hidden"');
    expect(source).toContain(
      '"hidden min-w-0 overflow-x-auto @min-[48rem]:block"',
    );
    expect(source.match(/@container/g) ?? []).toHaveLength(2);
    expect(source).not.toContain('data-testid="runtime-readonly-note"');
  });

  test("admin project override shell keeps project selection explicit and responsive", () => {
    const projectId = "33333333-3333-4333-8333-333333333333";
    const html = renderLocalized(
      createElement(
        AdminProjectAssetsShell,
        { projectId } as ComponentProps<typeof AdminProjectAssetsShell>,
        createElement("main", null, "override"),
      ),
    );

    expect(html).toContain('data-testid="admin-project-assets-shell"');
    expect(html).toContain('data-testid="admin-project-assets-context"');
    expect(html).toContain("返回项目选择");
    expect(html).toContain(projectId);
    expect(html).toContain("不会读取成员、聊天、运行、记忆、文件");
    expect(html).toContain('data-variant="line"');
    expect(html).toContain("border-b-2");
    expect(html).toContain("max-w-[90rem]");
    expect(html).toContain("sm:grid-cols-4");
    expect(html).toContain("sm:whitespace-nowrap");
    expect(html).not.toContain("bg-primary text-primary-foreground");
    for (const [segment, label] of [
      ["agents", "Agent"],
      ["skills", "Skill"],
      ["mcp", "MCP"],
      ["credentials", "凭据"],
    ]) {
      expect(html).toContain(
        `href="/admin/projects/${projectId}/assets/${segment}"`,
      );
      expect(html).toContain(label);
    }
  });

  test("admin asset surfaces follow the active locale without mixed ordinary-language copy", () => {
    const projectId = "33333333-3333-4333-8333-333333333333";
    const credential = {
      id: "11111111-1111-4111-8111-111111111111",
      scope: "system" as const,
      project_id: null,
      name: "github-token",
      display_name: "GitHub Token",
      credential_type: "token",
      status: "active" as const,
      current_version_id: "22222222-2222-4222-8222-222222222222",
      version: 2,
      created_by_user_id: "admin-user",
      created_at: "2026-07-13T08:00:00+00:00",
      updated_at: "2026-07-13T09:00:00+00:00",
    };
    const content = createElement(
      "div",
      null,
      createElement(
        AdminAssetsShell,
        null,
        createElement("main", null, "content"),
      ),
      createElement(
        AdminProjectAssetsShell,
        { projectId } as ComponentProps<typeof AdminProjectAssetsShell>,
        createElement("main", null, "content"),
      ),
      createElement(CredentialMetadataCard, {
        credential,
        onReplace: () => undefined,
        onRevoke: () => undefined,
        onMigrate: () => undefined,
        onDelete: () => undefined,
      }),
      createElement(CredentialRotationStatusCard, {
        status: {
          status: "current",
          eligible_total: 2,
          current: 2,
          pending: 0,
        },
      }),
      createElement(SkillVersionFields, { assetSlug: "review-skill" }),
      createElement(McpVersionFields),
      createElement(AssetStatusBadge, { status: "published" }),
      createElement(ProjectAssetCatalogView, {
        kind: "skills",
        data: {
          system_items: [],
          project_items: [],
          request_id: "localized-catalog",
        },
      }),
      createElement(AssetVersionHistory, {
        kind: "credentials",
        scope: "system",
        versions: [
          {
            id: "22222222-2222-4222-8222-222222222222",
            credential_id: credential.id,
            version_number: 1,
            status: "active",
            payload_schema_version: 1,
            payload_schema: { env: ["TOKEN"] },
            supersedes_version_id: null,
            created_by_user_id: "admin-user",
            created_at: "2026-07-13T09:00:00+00:00",
          },
        ],
      }),
      createElement(
        Dialog,
        { open: true },
        createElement(CredentialDeleteConfirmation, {
          credentialName: credential.display_name,
          remainingSeconds: 5,
          pending: false,
          errorMessage: null,
          onCancel: () => undefined,
          onConfirm: () => undefined,
        }),
      ),
    );

    const english = renderLocalized(content, "en-US");
    expect(english).toContain("Platform asset navigation");
    expect(english).toContain("Project shared-asset governance");
    expect(english).toContain("Credential metadata");
    expect(english).toContain("Credential envelope rotation");
    expect(english).toContain("File path");
    expect(english).toContain("Transport");
    expect(english).toContain("Published");
    expect(english).toContain("System assets");
    expect(english).toContain("Version 1");
    expect(english).toContain("Confirm delete (5s)");
    expect(english).not.toMatch(/[\u3400-\u9fff]/u);

    const chinese = renderLocalized(content, "zh-CN");
    expect(chinese).toContain("平台资产导航");
    expect(chinese).toContain("项目共享资产代管");
    expect(chinese).toContain("凭据元数据");
    expect(chinese).toContain("文件路径");
    expect(chinese).toContain("确认删除（5 秒）");
    expect(chinese).not.toContain("Platform asset navigation");
    expect(chinese).not.toContain("Project shared-asset governance");
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

  test("admin project override uses dense source directories and selected-only history", () => {
    const pageSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-project-asset-page.tsx",
      ),
      "utf8",
    );
    const bindingSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-project-system-binding-dialog.tsx",
      ),
      "utf8",
    );

    expect(pageSource).toContain("AdminProjectAssetDirectory");
    expect(pageSource).toContain("AdminProjectCredentialDirectory");
    expect(pageSource).toContain('data-testid="admin-project-asset-directory"');
    expect(pageSource).toContain(
      'data-testid="admin-project-credential-directory"',
    );
    expect(pageSource).toContain("selectedProjectAssetId");
    expect(pageSource).toContain("selectedCredentialId");
    expect(pageSource).toContain("projectCredentialCanDelete");
    expect(pageSource).not.toContain("<ProjectAssetCatalogView");
    expect(pageSource).not.toContain("<ProjectCredentialCatalogView");
    expect(bindingSource).toContain("sm:max-w-2xl");
    expect(bindingSource).toContain(
      'data-testid="admin-project-binding-summary"',
    );
    expect(pageSource).toContain(
      "xl:grid-cols-[minmax(13rem,1.7fr)_7rem_minmax(10rem,1fr)_8rem_auto]",
    );
    expect(pageSource).toContain(
      "xl:grid-cols-[minmax(14rem,1.7fr)_7rem_9rem_7rem_12rem_auto]",
    );
    expect(pageSource).not.toContain(
      "md:grid-cols-[minmax(13rem,1.7fr)_7rem_minmax(10rem,1fr)_8rem_auto]",
    );
    expect(pageSource).not.toContain(
      "md:grid-cols-[minmax(14rem,1.7fr)_7rem_9rem_7rem_12rem_auto]",
    );
    expect(pageSource).toContain(
      'const ADMIN_PROJECT_ASSET_DETAIL_ID = "admin-project-asset-detail"',
    );
    expect(pageSource).toContain(
      'const ADMIN_PROJECT_CREDENTIAL_DETAIL_ID = "admin-project-credential-detail"',
    );
    expect(pageSource).toContain(
      "aria-controls={ADMIN_PROJECT_ASSET_DETAIL_ID}",
    );
    expect(pageSource).toContain(
      "aria-controls={ADMIN_PROJECT_CREDENTIAL_DETAIL_ID}",
    );
    expect(pageSource).toContain("aria-expanded={selected}");
    expect(pageSource).toContain("detailRef.current?.scrollIntoView");
    expect(pageSource).toContain("tabIndex={-1}");
  });

  test("admin project directory search matches names and stable identifiers", () => {
    const rows = [
      {
        display_name: "Academic Paper Review",
        slug: "academic-paper-review",
      },
      {
        display_name: "GitHub Token",
        name: "github-token",
      },
    ];

    expect(filterAdminProjectDirectoryItems(rows, "PAPER")).toEqual([rows[0]]);
    expect(filterAdminProjectDirectoryItems(rows, "github-token")).toEqual([
      rows[1],
    ]);
    expect(filterAdminProjectDirectoryItems(rows, "missing")).toEqual([]);
  });

  test("does not construct an asset query while the auth user is null", () => {
    const queryClient = new QueryClient();
    const accountKey = rs.spyOn(sharedAssetKeys, "account");
    const TestAuthProvider = AuthProvider as ComponentType<
      PropsWithChildren<{ initialUser: null }>
    >;

    expect(() =>
      renderLocalized(
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

  test("filters the compact catalog by name, slug, type, and status", () => {
    const assets = [
      {
        id: "11111111-1111-4111-8111-111111111111",
        scope: "system" as const,
        project_id: null,
        slug: "review-agent",
        display_name: "Review Agent",
        status: "active" as const,
        current_published_version_id: "21111111-1111-4111-8111-111111111111",
        version: 1,
        created_by_user_id: "admin",
        created_at: "2026-07-13T08:00:00+00:00",
        updated_at: "2026-07-13T09:00:00+00:00",
      },
      {
        id: "31111111-1111-4111-8111-111111111111",
        scope: "system" as const,
        project_id: null,
        name: "github-token",
        display_name: "GitHub Token",
        credential_type: "token",
        status: "revoked" as const,
        current_version_id: "41111111-1111-4111-8111-111111111111",
        version: 2,
        created_by_user_id: "admin",
        created_at: "2026-07-13T08:00:00+00:00",
        updated_at: "2026-07-13T09:00:00+00:00",
      },
    ];

    expect(filterAdminCatalogItems(assets, "review", "all")).toEqual([
      assets[0],
    ]);
    expect(filterAdminCatalogItems(assets, "TOKEN", "all")).toEqual([
      assets[1],
    ]);
    expect(filterAdminCatalogItems(assets, "", "active")).toEqual([assets[0]]);
    expect(filterAdminCatalogItems(assets, "", "revoked")).toEqual([assets[1]]);
  });

  test("loads version history only inside the selected asset detail", () => {
    const pageSource = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-asset-page.tsx",
      ),
      "utf8",
    );

    expect(pageSource.match(/useAdminAssetVersions\(/g) ?? []).toHaveLength(1);
    expect(pageSource).toContain("SelectedAssetDetail");
    expect(pageSource).not.toContain("<AssetVersionHistory");
    expect(pageSource).not.toContain("open={index === 0}");
  });

  test("version history selects the current version by default and never shows an empty prompt", () => {
    const firstVersion: AssetVersion = {
      id: "11111111-1111-4111-8111-111111111111",
      credential_id: "21111111-1111-4111-8111-111111111111",
      version_number: 1,
      status: "retired",
      payload_schema_version: 1,
      payload_schema: { env: ["TOKEN"] },
      supersedes_version_id: null,
      created_by_user_id: "admin-user",
      created_at: "2026-07-13T08:00:00+00:00",
    };
    const currentVersion: AssetVersion = {
      ...firstVersion,
      id: "31111111-1111-4111-8111-111111111111",
      version_number: 2,
      status: "active",
      supersedes_version_id: firstVersion.id,
      created_at: "2026-07-13T09:00:00+00:00",
    };

    const currentHtml = renderLocalized(
      createElement(VersionTimeline, {
        versions: [firstVersion, currentVersion],
        currentVersionId: currentVersion.id,
      }),
    );
    expect(currentHtml).toContain(
      `data-testid="admin-version-detail-${currentVersion.id}"`,
    );
    expect(currentHtml).toContain(
      `data-testid="admin-version-row-${currentVersion.id}" aria-pressed="true"`,
    );
    expect(currentHtml).not.toContain("选择左侧版本");

    const onlyHtml = renderLocalized(
      createElement(VersionTimeline, {
        versions: [firstVersion],
      }),
    );
    expect(onlyHtml).toContain(
      `data-testid="admin-version-detail-${firstVersion.id}"`,
    );
    expect(onlyHtml).not.toContain("aria-pressed");
    expect(onlyHtml).not.toContain("选择左侧版本");
  });

  test("desktop asset details use a fixed non-modal inspector without a backdrop", () => {
    const html = renderLocalized(
      createElement(
        AdminAssetDesktopInspector,
        {
          item: {
            id: "11111111-1111-4111-8111-111111111111",
            scope: "system",
            project_id: null,
            slug: "deerflow-docs",
            display_name: "DeerFlow Docs",
            status: "active",
            current_published_version_id:
              "21111111-1111-4111-8111-111111111111",
            version: 1,
            created_by_user_id: "admin-user",
            created_at: "2026-07-13T08:00:00+00:00",
            updated_at: "2026-07-13T09:00:00+00:00",
          },
          kind: "skills",
          onClose: () => undefined,
        },
        createElement("p", null, "version detail"),
      ),
    );

    expect(html).toContain('data-testid="admin-asset-inspector"');
    expect(html).toContain('data-mode="desktop"');
    expect(html).toContain('id="admin-asset-inspector"');
    expect(html).toContain('tabindex="-1"');
    expect(html).toContain("top-14");
    expect(html).toContain("w-[clamp(32rem,34vw,48rem)]");
    expect(html).toContain("Skill 详情");
    expect(html).toContain("version detail");
    expect(html).not.toContain('data-slot="sheet-overlay"');
    expect(html).not.toContain("bg-black/50");
  });

  test("asset inspectors overlay the catalog without resizing it", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/admin/assets/admin-asset-page.tsx",
      ),
      "utf8",
    );

    expect(source).toContain('<aside\n      id="admin-asset-inspector"');
    expect(source).toContain(
      '<SheetContent\n        id="admin-asset-inspector"',
    );
    expect(source).toContain('event.key === "Escape"');
    expect(source).toContain("previouslyFocusedElementRef");
    expect(source).not.toContain("xl:pr-[clamp(33rem,calc(34vw+1rem),49rem)]");
  });

  test("technical identifiers wrap instead of overflowing the detail panel", () => {
    const html = renderLocalized(
      createElement(AdminTechnicalValue, {
        className: "sm:col-span-2",
        label: "URL",
        value:
          "https://mcp.example.test/a/very/long/path/without/any/short/segments?checksum=0123456789abcdef0123456789abcdef",
        valueClassName: "font-sans leading-relaxed",
      }),
    );

    expect(html).toContain("min-w-0");
    expect(html).toContain("sm:col-span-2");
    expect(html).toContain("font-sans");
    expect(html).toContain("leading-relaxed");
    expect(html).toContain("[overflow-wrap:anywhere]");
    expect(html).toContain("0123456789abcdef");
  });

  test("MCP grant editing starts from active bindings and preserves untouched slots", () => {
    const version: Extract<AssetVersion, { mcp_server_id: string }> = {
      id: "11111111-1111-4111-8111-111111111111",
      mcp_server_id: "21111111-1111-4111-8111-111111111111",
      version_number: 3,
      workflow_status: "published",
      definition: {
        description: "Remote MCP",
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
        credential_slots: [],
      },
      credential_slots: [
        {
          id: "31111111-1111-4111-8111-111111111111",
          name: "primary-token",
          purpose: "Primary",
          payload_schema: { headers: ["Authorization"] },
          required: true,
        },
        {
          id: "41111111-1111-4111-8111-111111111111",
          name: "audit-token",
          purpose: "Audit",
          payload_schema: { env: ["AUDIT_TOKEN"] },
          required: false,
        },
      ],
      credential_grants: [
        {
          id: "51111111-1111-4111-8111-111111111111",
          mcp_server_version_id: "11111111-1111-4111-8111-111111111111",
          credential_slot_id: "31111111-1111-4111-8111-111111111111",
          credential_version_id: "61111111-1111-4111-8111-111111111111",
          status: "active",
          version: 4,
          created_by_user_id: "admin",
          created_at: "2026-07-13T08:00:00+00:00",
        },
        {
          id: "71111111-1111-4111-8111-111111111111",
          mcp_server_version_id: "11111111-1111-4111-8111-111111111111",
          credential_slot_id: "41111111-1111-4111-8111-111111111111",
          credential_version_id: "81111111-1111-4111-8111-111111111111",
          status: "active",
          version: 2,
          created_by_user_id: "admin",
          created_at: "2026-07-13T08:00:00+00:00",
        },
      ],
      supersedes_version_id: null,
      payload_checksum: "mcp-checksum",
      submitted_at: null,
      reviewed_at: null,
      reviewed_by_user_id: null,
      created_by_user_id: "admin",
      created_at: "2026-07-13T08:00:00+00:00",
    };

    const selections = initialMcpCredentialSelections(version);
    expect(selections).toEqual({
      "primary-token": "61111111-1111-4111-8111-111111111111",
      "audit-token": "81111111-1111-4111-8111-111111111111",
    });
    expect(buildMcpCredentialGrantInput(version, selections)).toEqual({
      credential_versions: selections,
      expected_active_grant_versions: {
        "primary-token": 4,
        "audit-token": 2,
      },
    });
  });

  test("authoring fields use Chinese labels for ordinary UI terms", () => {
    const html = [
      renderLocalized(
        createElement(SkillVersionFields, { assetSlug: "review-skill" }),
      ),
      renderLocalized(createElement(McpVersionFields)),
    ].join("\n");

    for (const label of [
      "媒体类型",
      "传输方式",
      "URL",
      "凭据槽位",
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
    const html = renderLocalized(createElement(McpVersionFields));

    expect(html).toContain('<option value="sse"');
    expect(html).toContain('<option value="http"');
    expect(html).toContain('name="url"');
    expect(html).toContain('required=""');
    expect(html).toContain("执行器访问");
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
    const html = renderLocalized(
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
    const html = renderLocalized(
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

    expect(html).not.toContain("GitHub Token");
    expect(html).toContain('data-density="compact"');
    expect(html).toContain("凭据元数据");
    expect(html).toContain("访问令牌");
    expect(html).toContain("替换凭据");
    expect(html).toContain("撤销凭据");
    expect(html).toContain("迁移兼容引用");
    expect(html).toContain(">删除<");
    expect(html).toContain('data-testid="credential-danger-zone"');
    expect(html).toContain("既有 MCP 授权与 Skill 环境变量绑定仍固定到旧版本");
    expect(html).not.toContain("显示明文");
    expect(html).not.toContain("复制密钥");
    expect(html).not.toContain("plaintext");
  });

  test("credential write failures remain visible outside secret dialogs", () => {
    const html = renderLocalized(
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
    const zhLocaleSource = readFileSync(
      resolve(process.cwd(), "src/core/i18n/locales/zh-CN.ts"),
      "utf8",
    );

    expect(zhLocaleSource).toContain("内置目录");
    expect(zhLocaleSource).toContain("运行期只读");
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
    const html = renderLocalized(
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
    const html = renderLocalized(
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
    const html = renderLocalized(
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
