"use client";

import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales";
import {
  SharedAssetApiError,
  type AssetScope,
  type AssetVersion,
  type McpToolInventory,
} from "@/core/shared-assets";
import { mcpVersionRuntimeBlockReason } from "@/core/shared-assets/mcp-runtime";

export type McpAssetVersion = Extract<AssetVersion, { mcp_server_id: string }>;
type McpToolInventoryCopy = Translations["adminAssets"]["mcpToolInventory"];

function StringMap({ value }: { value: Record<string, string> }) {
  const entries = Object.entries(value);
  if (entries.length === 0) {
    return <p className="text-muted-foreground text-sm">未配置</p>;
  }
  return (
    <dl className="divide-border/70 overflow-hidden rounded-lg border text-xs">
      {entries.map(([key, item]) => (
        <div
          key={key}
          className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] gap-3 px-3 py-2"
        >
          <dt className="font-mono break-all">{key}</dt>
          <dd className="text-muted-foreground font-mono break-all">{item}</dd>
        </div>
      ))}
    </dl>
  );
}

function JsonConfig({ value }: { value: Record<string, unknown> }) {
  return Object.keys(value).length === 0 ? (
    <p className="text-muted-foreground text-sm">未配置</p>
  ) : (
    <pre className="bg-muted/45 overflow-x-auto rounded-lg p-3 text-xs">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function InventoryNotice({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "warning" | "danger";
}) {
  const toneClass =
    tone === "danger"
      ? "border-destructive/30 bg-destructive/5 text-destructive"
      : tone === "warning"
        ? "border-amber-300/70 bg-amber-50 text-amber-950 dark:border-amber-900/70 dark:bg-amber-950/25 dark:text-amber-100"
        : "border-border/70 bg-muted/35 text-muted-foreground";
  return (
    <p className={`rounded-xl border px-4 py-3 text-sm leading-6 ${toneClass}`}>
      {children}
    </p>
  );
}

function inventoryFailureMessage(
  errorCode: McpToolInventory["error_code"],
  copy: McpToolInventoryCopy,
): string {
  return errorCode === "mcp_catalog_invalid"
    ? copy.catalogInvalid
    : copy.discoveryUnavailable;
}

function inventoryRequestFailureMessage(
  error: unknown,
  copy: McpToolInventoryCopy,
): string {
  if (!(error instanceof SharedAssetApiError)) {
    return copy.loadErrors.generic;
  }
  switch (error.code) {
    case "ASSET_NOT_FOUND":
      return copy.loadErrors.notFound;
    case "ASSET_FORBIDDEN":
      return copy.loadErrors.forbidden;
    case "AUTH_REQUIRED":
      return copy.loadErrors.authRequired;
    case "ASSET_RESPONSE_INVALID":
      return copy.loadErrors.responseInvalid;
    case "ASSET_NETWORK_ERROR":
    case "ASSET_STORAGE_UNAVAILABLE":
      return copy.loadErrors.network;
    default:
      return copy.loadErrors.generic;
  }
}

function toolDiscoveryRequestFailureMessage(
  error: unknown,
  copy: McpToolInventoryCopy,
): string {
  if (!(error instanceof SharedAssetApiError)) {
    return copy.testErrors.generic;
  }
  switch (error.code) {
    case "ASSET_NOT_FOUND":
      return copy.testErrors.notFound;
    case "ASSET_FORBIDDEN":
      return copy.testErrors.forbidden;
    case "AUTH_REQUIRED":
      return copy.testErrors.authRequired;
    case "ASSET_CONFLICT":
      return copy.testErrors.conflict;
    case "ASSET_NETWORK_ERROR":
    case "ASSET_STORAGE_UNAVAILABLE":
      return copy.testErrors.network;
    default:
      return copy.testErrors.generic;
  }
}

function McpToolList({
  inventory,
  copy,
  showEmpty = true,
}: {
  inventory: McpToolInventory;
  copy: McpToolInventoryCopy;
  showEmpty?: boolean;
}) {
  return (
    <>
      {inventory.tools.length === 0 ? (
        showEmpty ? (
          <div
            role="status"
            className="border-border/70 text-muted-foreground rounded-xl border border-dashed px-4 py-6 text-center text-sm"
          >
            {copy.empty}
          </div>
        ) : null
      ) : (
        <dl className="border-border/70 bg-muted/30 overflow-hidden rounded-xl border px-4">
          {inventory.tools.map((tool) => (
            <div
              key={tool.name}
              className="border-border/70 grid min-w-0 gap-1 border-b py-3.5 last:border-b-0 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] sm:items-center sm:gap-6"
            >
              <dt className="min-w-0 font-mono text-sm font-medium [overflow-wrap:anywhere]">
                {tool.name}
              </dt>
              <dd className="text-muted-foreground min-w-0 text-sm leading-6 [overflow-wrap:anywhere] sm:text-right">
                {tool.description || copy.noDescription}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {inventory.last_success_at ? (
        <p className="text-muted-foreground text-right text-xs">
          {copy.lastSuccess}
          <time dateTime={inventory.last_success_at}>
            {new Date(inventory.last_success_at).toLocaleString()}
          </time>
        </p>
      ) : null}
    </>
  );
}

export function McpToolInventorySection({
  workflowStatus,
  inventory,
  isLoading = false,
  error = null,
  isRefreshing = false,
  toolDiscoveryPending = false,
  toolDiscoveryError = null,
  onTest,
}: {
  workflowStatus: McpAssetVersion["workflow_status"];
  inventory?: McpToolInventory | null;
  isLoading?: boolean;
  error?: unknown;
  isRefreshing?: boolean;
  toolDiscoveryPending?: boolean;
  toolDiscoveryError?: unknown;
  onTest?: () => void;
}) {
  const { t } = useI18n();
  const copy = t.adminAssets.mcpToolInventory;
  const effectiveInventory =
    inventory ??
    (workflowStatus === "published"
      ? {
          status: "never_discovered" as const,
          tools: [],
          last_attempt_at: null,
          last_success_at: null,
          error_code: null,
        }
      : null);
  const hasRequestError = error !== null && error !== undefined;
  const testing =
    workflowStatus === "published" &&
    (toolDiscoveryPending || effectiveInventory?.status === "testing");
  const testLabel = testing
    ? copy.testingAction
    : !inventory || inventory.status === "never_discovered"
      ? copy.testService
      : copy.retestService;
  const showCount =
    effectiveInventory &&
    (effectiveInventory.status === "ready" ||
      effectiveInventory.status === "degraded" ||
      (effectiveInventory.status === "testing" &&
        effectiveInventory.tools.length > 0));

  return (
    <section
      className="space-y-3"
      aria-busy={isLoading || isRefreshing || testing || undefined}
    >
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="space-y-1">
          <h3 className="text-sm font-semibold">{copy.title}</h3>
          <p className="text-muted-foreground text-xs">{copy.description}</p>
        </div>
        <div className="flex items-center gap-3">
          {showCount ? (
            <span className="text-muted-foreground text-xs tabular-nums">
              {copy.toolCount(effectiveInventory.tools.length)}
            </span>
          ) : null}
          {workflowStatus === "published" && onTest ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label={testLabel}
              disabled={testing}
              onClick={onTest}
            >
              {testLabel}
            </Button>
          ) : null}
        </div>
      </div>

      {toolDiscoveryError ? (
        <div role="alert">
          <InventoryNotice tone="danger">
            {copy.testFailurePrefix}{" "}
            {toolDiscoveryRequestFailureMessage(toolDiscoveryError, copy)}
          </InventoryNotice>
        </div>
      ) : null}

      {isLoading ? (
        <div
          role="status"
          aria-label={copy.loading}
          className="border-border/70 space-y-3 rounded-xl border p-4"
        >
          <span className="sr-only">{copy.loading}</span>
          {["first", "second", "third"].map((key) => (
            <div
              key={key}
              className="grid gap-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] sm:gap-6"
            >
              <Skeleton className="h-5 w-48 max-w-full" />
              <Skeleton className="h-5 w-full" />
            </div>
          ))}
        </div>
      ) : workflowStatus !== "published" ? (
        <InventoryNotice>{copy.unpublished}</InventoryNotice>
      ) : testing ? (
        <>
          <InventoryNotice>{copy.testing}</InventoryNotice>
          {hasRequestError ? (
            <div role="alert">
              <InventoryNotice tone={inventory ? "warning" : "danger"}>
                {inventory
                  ? copy.refreshFailed
                  : inventoryRequestFailureMessage(error, copy)}
              </InventoryNotice>
            </div>
          ) : null}
          {effectiveInventory ? (
            <McpToolList
              inventory={effectiveInventory}
              copy={copy}
              showEmpty={false}
            />
          ) : null}
        </>
      ) : hasRequestError && !inventory ? (
        <div
          role="alert"
          className="border-destructive/30 bg-destructive/5 rounded-xl border px-4 py-3"
        >
          <p className="text-destructive text-sm">
            {inventoryRequestFailureMessage(error, copy)}
          </p>
        </div>
      ) : effectiveInventory?.status === "never_discovered" ? (
        <InventoryNotice>{copy.neverDiscovered}</InventoryNotice>
      ) : effectiveInventory?.status === "failed" ? (
        <div role="alert">
          <InventoryNotice tone="danger">
            {inventoryFailureMessage(effectiveInventory.error_code, copy)}
          </InventoryNotice>
        </div>
      ) : effectiveInventory?.status === "stale" ? (
        <InventoryNotice tone="warning">{copy.stale}</InventoryNotice>
      ) : effectiveInventory ? (
        <>
          {hasRequestError && inventory ? (
            <div role="alert">
              <InventoryNotice tone="warning">
                {copy.refreshFailed}
              </InventoryNotice>
            </div>
          ) : null}
          {effectiveInventory.status === "degraded" ? (
            <div role="alert">
              <InventoryNotice tone="warning">
                {inventoryFailureMessage(effectiveInventory.error_code, copy)}{" "}
                {copy.degradedSuffix}
              </InventoryNotice>
            </div>
          ) : null}
          <McpToolList inventory={effectiveInventory} copy={copy} />
        </>
      ) : null}
    </section>
  );
}

export function McpAssetDetail({
  version,
  scope,
  toolInventory,
  toolInventoryLoading = false,
  toolInventoryError = null,
  toolInventoryRefreshing = false,
  toolDiscoveryPending = false,
  toolDiscoveryError = null,
  onTestToolDiscovery,
}: {
  version: McpAssetVersion;
  scope: AssetScope;
  toolInventory?: McpToolInventory | null;
  toolInventoryLoading?: boolean;
  toolInventoryError?: unknown;
  toolInventoryRefreshing?: boolean;
  toolDiscoveryPending?: boolean;
  toolDiscoveryError?: unknown;
  onTestToolDiscovery?: () => void;
}) {
  const definition = version.definition;
  const runtimeBlockReason = mcpVersionRuntimeBlockReason(version, scope);
  return (
    <div className="space-y-6">
      {runtimeBlockReason ? (
        <p
          role="alert"
          className="border-destructive/30 bg-destructive/5 text-destructive rounded-xl border px-4 py-3 text-sm"
        >
          {runtimeBlockReason}
        </p>
      ) : null}
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">MCP 说明</h3>
        <p className="text-muted-foreground text-sm leading-6 whitespace-pre-wrap">
          {definition.description || "未填写说明。"}
        </p>
      </section>

      <section className="grid gap-4 sm:grid-cols-[minmax(0,0.75fr)_minmax(0,0.75fr)_minmax(0,1.5fr)]">
        <div className="border-border/70 rounded-xl border p-4">
          <p className="text-muted-foreground text-xs">传输方式</p>
          <p className="mt-2 font-mono text-sm">{definition.transport}</p>
        </div>
        <div className="border-border/70 rounded-xl border p-4">
          <p className="text-muted-foreground text-xs">超时</p>
          <p className="mt-2 text-sm">{definition.timeout_seconds} 秒</p>
        </div>
        {definition.command && (
          <div className="border-border/70 rounded-xl border p-4 sm:col-span-3">
            <p className="text-muted-foreground text-xs">命令与参数</p>
            <p className="mt-2 font-mono text-sm break-all">
              {[definition.command, ...definition.args].join(" ")}
            </p>
          </div>
        )}
        {definition.url && (
          <div className="border-border/70 min-w-0 rounded-xl border p-4">
            <p className="text-muted-foreground text-xs">URL</p>
            <p className="mt-2 font-mono text-sm break-all">{definition.url}</p>
          </div>
        )}
      </section>

      <McpToolInventorySection
        workflowStatus={version.workflow_status}
        inventory={toolInventory}
        isLoading={toolInventoryLoading}
        error={toolInventoryError}
        isRefreshing={toolInventoryRefreshing}
        toolDiscoveryPending={toolDiscoveryPending}
        toolDiscoveryError={toolDiscoveryError}
        onTest={onTestToolDiscovery}
      />

      <details className="border-border/70 rounded-xl border px-4 py-3">
        <summary className="cursor-pointer text-sm font-medium">
          非敏感配置
        </summary>
        <div className="mt-4 space-y-4">
          <div className="space-y-2">
            <p className="text-xs font-medium">环境变量</p>
            <StringMap value={definition.env} />
          </div>
          <div className="space-y-2">
            <p className="text-xs font-medium">请求头</p>
            <StringMap value={definition.headers} />
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-2">
              <p className="text-xs font-medium">OAuth</p>
              <JsonConfig value={definition.oauth} />
            </div>
            <div className="space-y-2">
              <p className="text-xs font-medium">路由</p>
              <JsonConfig value={definition.routing} />
            </div>
            <div className="space-y-2">
              <p className="text-xs font-medium">工具覆盖</p>
              <JsonConfig value={definition.tool_overrides} />
            </div>
          </div>
        </div>
      </details>
    </div>
  );
}
