import { describe, expect, rs, test } from "@rstest/core";
import type {
  ButtonHTMLAttributes,
  HTMLAttributes,
  PropsWithChildren,
} from "react";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/components/ui/button", () => ({
  Button: ({
    children,
    onClick,
    ...props
  }: ButtonHTMLAttributes<HTMLButtonElement>) => {
    if (
      (props as Record<string, unknown>)["data-testid"] ===
      "system-binding-history-retry"
    ) {
      (
        globalThis as typeof globalThis & {
          __systemBindingHistoryRetry?: () => unknown;
        }
      ).__systemBindingHistoryRetry = onClick as (() => unknown) | undefined;
    }
    if (
      (props as Record<string, unknown>)["data-testid"] ===
      "system-mcp-binding-submit"
    ) {
      (
        globalThis as typeof globalThis & {
          __systemMcpBindingSubmit?: () => unknown;
        }
      ).__systemMcpBindingSubmit = onClick as (() => unknown) | undefined;
    }
    return <button {...props}>{children}</button>;
  },
}));

rs.mock("@/components/ui/dialog", () => {
  function Container({ children }: PropsWithChildren) {
    return <div>{children}</div>;
  }

  return {
    Dialog: ({ children, open }: PropsWithChildren<{ open?: boolean }>) =>
      open ? <div>{children}</div> : null,
    DialogContent: Container,
    DialogDescription: ({ children }: PropsWithChildren) => <p>{children}</p>,
    DialogFooter: Container,
    DialogHeader: Container,
    DialogTitle: ({ children }: PropsWithChildren) => <h2>{children}</h2>,
  };
});

rs.mock("@/components/ui/skeleton", () => ({
  Skeleton: (props: HTMLAttributes<HTMLDivElement>) => <div {...props} />,
}));

rs.mock("@/core/shared-assets", () => ({
  SharedAssetApiError: class SharedAssetApiError extends Error {},
  useDisableProjectSystemBinding: rs.fn(),
  useEnableProjectSystemBinding: rs.fn(),
  useProjectAssetVersions: rs.fn(),
  useRollbackProjectSystemBinding: rs.fn(),
  useSyncCurrentProjectSystemMcpBinding: rs.fn(),
  useUpgradeProjectSystemBinding: rs.fn(),
}));

import * as bindingDialog from "@/components/projects/assets/system-binding-dialog";
import { SystemBindingDialog } from "@/components/projects/assets/system-binding-dialog";
import {
  useDisableProjectSystemBinding,
  useEnableProjectSystemBinding,
  useProjectAssetVersions,
  useRollbackProjectSystemBinding,
  useSyncCurrentProjectSystemMcpBinding,
  useUpgradeProjectSystemBinding,
  type ProjectAssetItem,
} from "@/core/shared-assets";

type BindingAvailabilityInput = {
  historyLoading: boolean;
  historyError: boolean;
  historyRetryPending: boolean;
  mutationPending: boolean;
  selectedVersionId: string;
  publishedVersionIds: readonly string[];
  boundVersionId?: string | null;
};

type BindingAvailability = {
  canSubmit: boolean;
  canRetryHistory: boolean;
  hasSelectedPublishedTarget: boolean;
};

const systemBindingDialogAvailability = (
  bindingDialog as typeof bindingDialog & {
    systemBindingDialogAvailability: (
      input: BindingAvailabilityInput,
    ) => BindingAvailability;
  }
).systemBindingDialogAvailability;

const SYSTEM_ASSET_ID = "11111111-1111-4111-8111-111111111111";
const PUBLISHED_VERSION_ID = "22222222-2222-4222-8222-222222222222";

const SYSTEM_ASSET: ProjectAssetItem = {
  id: SYSTEM_ASSET_ID,
  scope: "system",
  project_id: null,
  slug: "github-mcp",
  display_name: "GitHub MCP",
  status: "active",
  current_published_version_id: PUBLISHED_VERSION_ID,
  version: 1,
  created_by_user_id: "user-1",
  created_at: "2026-07-21T00:00:00Z",
  updated_at: "2026-07-21T00:00:00Z",
  capabilities: ["shared_assets.read", "shared_assets.manage_bindings"],
  binding: null,
};

function availabilityInput(
  overrides: Partial<BindingAvailabilityInput> = {},
): BindingAvailabilityInput {
  return {
    historyLoading: false,
    historyError: false,
    historyRetryPending: false,
    mutationPending: false,
    selectedVersionId: PUBLISHED_VERSION_ID,
    publishedVersionIds: [PUBLISHED_VERSION_ID],
    boundVersionId: null,
    ...overrides,
  };
}

function mutation() {
  return {
    error: null,
    isPending: false,
    mutate: rs.fn(),
  };
}

function prepareMutations(syncMutation = mutation()) {
  rs.mocked(useDisableProjectSystemBinding).mockReturnValue(
    mutation() as never,
  );
  rs.mocked(useEnableProjectSystemBinding).mockReturnValue(mutation() as never);
  rs.mocked(useRollbackProjectSystemBinding).mockReturnValue(
    mutation() as never,
  );
  rs.mocked(useUpgradeProjectSystemBinding).mockReturnValue(
    mutation() as never,
  );
  rs.mocked(useSyncCurrentProjectSystemMcpBinding).mockReturnValue(
    syncMutation as never,
  );
  return syncMutation;
}

describe("SystemBindingDialog submission availability", () => {
  test("fails closed while version history is loading", () => {
    expect(
      systemBindingDialogAvailability(
        availabilityInput({ historyLoading: true }),
      ),
    ).toEqual({
      canSubmit: false,
      canRetryHistory: false,
      hasSelectedPublishedTarget: true,
    });
  });

  test("fails closed on history error and exposes one safe retry action", () => {
    expect(
      systemBindingDialogAvailability(
        availabilityInput({ historyError: true }),
      ),
    ).toEqual({
      canSubmit: false,
      canRetryHistory: true,
      hasSelectedPublishedTarget: true,
    });

    expect(
      systemBindingDialogAvailability(
        availabilityInput({
          historyError: true,
          historyRetryPending: true,
        }),
      ),
    ).toEqual({
      canSubmit: false,
      canRetryHistory: false,
      hasSelectedPublishedTarget: true,
    });
  });

  test("rejects an empty, draft, or otherwise unavailable selected target", () => {
    expect(
      systemBindingDialogAvailability(
        availabilityInput({
          selectedVersionId: "",
          publishedVersionIds: [],
        }),
      ),
    ).toEqual({
      canSubmit: false,
      canRetryHistory: false,
      hasSelectedPublishedTarget: false,
    });

    expect(
      systemBindingDialogAvailability(
        availabilityInput({ publishedVersionIds: [] }),
      ).canSubmit,
    ).toBe(false);
  });

  test("allows submission only after the selected published target loads", () => {
    expect(systemBindingDialogAvailability(availabilityInput())).toEqual({
      canSubmit: true,
      canRetryHistory: false,
      hasSelectedPublishedTarget: true,
    });

    expect(
      systemBindingDialogAvailability(
        availabilityInput({ boundVersionId: PUBLISHED_VERSION_ID }),
      ).canSubmit,
    ).toBe(false);
    expect(
      systemBindingDialogAvailability(
        availabilityInput({ mutationPending: true }),
      ).canSubmit,
    ).toBe(false);
  });
});

describe("SystemBindingDialog history retry", () => {
  test("renders a safe error and retries the scoped history query", async () => {
    const rawError = "cannot pickle mappingproxy: postgres-password";
    const refetch = rs.fn(async () => undefined);
    delete (
      globalThis as typeof globalThis & {
        __systemBindingHistoryRetry?: () => unknown;
      }
    ).__systemBindingHistoryRetry;
    prepareMutations();
    rs.mocked(useProjectAssetVersions).mockReturnValue({
      data: undefined,
      error: new Error(rawError),
      isFetching: false,
      isLoading: false,
      refetch,
    } as never);

    const html = renderToStaticMarkup(
      <SystemBindingDialog
        accountId="account-1"
        projectId="project-1"
        kind="mcp-servers"
        item={SYSTEM_ASSET}
        open
        onOpenChange={rs.fn()}
      />,
    );

    expect(html).toContain("操作失败，请稍后重试。");
    expect(html).not.toContain(rawError);
    expect(html).toContain('data-testid="system-binding-history-retry"');

    const retry = (
      globalThis as typeof globalThis & {
        __systemBindingHistoryRetry?: () => unknown;
      }
    ).__systemBindingHistoryRetry;
    expect(retry).toBeDefined();
    await retry?.();
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  test("shows only the current MCP configuration and fails closed when it is unavailable", () => {
    prepareMutations();
    rs.mocked(useProjectAssetVersions).mockReturnValue({
      data: {
        data: [
          {
            id: PUBLISHED_VERSION_ID,
            mcp_server_id: SYSTEM_ASSET_ID,
            version_number: 1,
            workflow_status: "published",
            definition: {
              description: "Unsupported legacy MCP",
              transport: "streamable_http",
              command: null,
              args: [],
              url: null,
              env: {},
              headers: {},
              oauth: {},
              routing: {},
              tool_overrides: {},
              timeout_seconds: 30,
              credential_slots: [],
            },
            credential_slots: [],
            credential_grants: [],
            supersedes_version_id: null,
            payload_checksum: "a".repeat(64),
            submitted_at: null,
            reviewed_at: null,
            reviewed_by_user_id: null,
            created_by_user_id: "user-1",
            created_at: "2026-07-21T00:00:00Z",
          },
        ],
        request_id: "request-1",
      },
      error: null,
      isFetching: false,
      isLoading: false,
      refetch: rs.fn(),
    } as never);

    const html = renderToStaticMarkup(
      <SystemBindingDialog
        accountId="account-1"
        projectId="project-1"
        kind="mcp-servers"
        item={SYSTEM_ASSET}
        open
        onOpenChange={rs.fn()}
      />,
    );

    expect(html).not.toContain("配置 #1");
    expect(html).not.toContain("<select");
    expect(html).toContain("Private runtime 仅支持 stdio、SSE 或 HTTP");
    expect(html).toContain("当前没有可启用的已发布配置");
    expect(html).not.toContain("版本");
  });

  test("offers one update action for the authoritative current MCP configuration", () => {
    prepareMutations();
    const previousVersionId = "33333333-3333-4333-8333-333333333333";
    const definition = {
      description: "Current MCP",
      transport: "http" as const,
      command: null,
      args: [],
      url: "https://mcp.example.test/mcp",
      env: {},
      headers: {},
      oauth: {},
      routing: {},
      tool_overrides: {},
      timeout_seconds: 30,
      credential_slots: [],
    };
    rs.mocked(useProjectAssetVersions).mockReturnValue({
      data: {
        data: [
          {
            id: PUBLISHED_VERSION_ID,
            mcp_server_id: SYSTEM_ASSET_ID,
            version_number: 2,
            workflow_status: "published",
            definition,
            credential_slots: [],
            credential_grants: [],
            supersedes_version_id: previousVersionId,
            payload_checksum: "b".repeat(64),
            submitted_at: null,
            reviewed_at: null,
            reviewed_by_user_id: null,
            created_by_user_id: "user-1",
            created_at: "2026-07-22T00:00:00Z",
          },
          {
            id: previousVersionId,
            mcp_server_id: SYSTEM_ASSET_ID,
            version_number: 1,
            workflow_status: "published",
            definition: { ...definition, description: "Historical MCP" },
            credential_slots: [],
            credential_grants: [],
            supersedes_version_id: null,
            payload_checksum: "a".repeat(64),
            submitted_at: null,
            reviewed_at: null,
            reviewed_by_user_id: null,
            created_by_user_id: "user-1",
            created_at: "2026-07-21T00:00:00Z",
          },
        ],
        request_id: "request-1",
      },
      error: null,
      isFetching: false,
      isLoading: false,
      refetch: rs.fn(),
    } as never);

    const html = renderToStaticMarkup(
      <SystemBindingDialog
        accountId="account-1"
        projectId="project-1"
        kind="mcp-servers"
        item={{
          ...SYSTEM_ASSET,
          binding: {
            project_id: "project-1",
            kind: "mcp",
            asset_id: SYSTEM_ASSET_ID,
            version_id: previousVersionId,
            enabled: true,
            version: 1,
            created_by_user_id: "user-1",
            updated_by_user_id: "user-1",
            created_at: "2026-07-21T00:00:00Z",
            updated_at: "2026-07-21T00:00:00Z",
          },
        }}
        open
        onOpenChange={rs.fn()}
      />,
    );

    expect(html).toContain("有配置更新");
    expect(html).toContain("更新到当前配置");
    expect(html).not.toContain("<select");
    expect(html).not.toContain("配置 #");
    expect(html).not.toContain("Historical MCP");
  });

  test("clicking sync-current omits a missing binding revision and preserves an existing one", () => {
    const syncMutation = prepareMutations();
    rs.mocked(useProjectAssetVersions).mockReturnValue({
      data: {
        data: [
          {
            id: PUBLISHED_VERSION_ID,
            mcp_server_id: SYSTEM_ASSET_ID,
            version_number: 2,
            workflow_status: "published",
            definition: {
              description: "Current MCP",
              transport: "http",
              command: null,
              args: [],
              url: "https://mcp.example.test/mcp",
              env: {},
              headers: {},
              oauth: {},
              routing: {},
              tool_overrides: {},
              timeout_seconds: 30,
              credential_slots: [],
            },
            credential_slots: [],
            credential_grants: [],
            supersedes_version_id: null,
            payload_checksum: "b".repeat(64),
            submitted_at: null,
            reviewed_at: null,
            reviewed_by_user_id: null,
            created_by_user_id: "user-1",
            created_at: "2026-07-22T00:00:00Z",
          },
        ],
        request_id: "request-1",
      },
      error: null,
      isFetching: false,
      isLoading: false,
      refetch: rs.fn(),
    } as never);

    delete (
      globalThis as typeof globalThis & {
        __systemMcpBindingSubmit?: () => unknown;
      }
    ).__systemMcpBindingSubmit;
    renderToStaticMarkup(
      <SystemBindingDialog
        accountId="account-1"
        projectId="project-1"
        kind="mcp-servers"
        item={SYSTEM_ASSET}
        open
        onOpenChange={rs.fn()}
      />,
    );
    const submitWithoutBinding = (
      globalThis as typeof globalThis & {
        __systemMcpBindingSubmit?: () => unknown;
      }
    ).__systemMcpBindingSubmit;
    expect(submitWithoutBinding).toBeDefined();
    submitWithoutBinding?.();
    expect(syncMutation.mutate).toHaveBeenLastCalledWith({
      assetId: SYSTEM_ASSET_ID,
      input: {},
    });

    renderToStaticMarkup(
      <SystemBindingDialog
        accountId="account-1"
        projectId="project-1"
        kind="mcp-servers"
        item={{
          ...SYSTEM_ASSET,
          binding: {
            project_id: "project-1",
            kind: "mcp",
            asset_id: SYSTEM_ASSET_ID,
            version_id: "33333333-3333-4333-8333-333333333333",
            enabled: false,
            version: 7,
            created_by_user_id: "user-1",
            updated_by_user_id: "user-1",
            created_at: "2026-07-21T00:00:00Z",
            updated_at: "2026-07-21T00:00:00Z",
          },
        }}
        open
        onOpenChange={rs.fn()}
      />,
    );
    const submitWithBinding = (
      globalThis as typeof globalThis & {
        __systemMcpBindingSubmit?: () => unknown;
      }
    ).__systemMcpBindingSubmit;
    expect(submitWithBinding).toBeDefined();
    submitWithBinding?.();
    expect(syncMutation.mutate).toHaveBeenLastCalledWith({
      assetId: SYSTEM_ASSET_ID,
      input: { expected_binding_version: 7 },
    });
  });
});
