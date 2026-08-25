"use client";

import { BotIcon, NetworkIcon, SearchIcon, SparklesIcon } from "lucide-react";
import { useState } from "react";

import { AdminPage, AdminSection } from "@/components/admin/ui/admin-page";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import { AssetVersionHistory } from "@/components/assets/asset-version-history";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import {
  useAdminAssets,
  useAdminAgentDefinition,
  useAdminAssetVersions,
  type AgentDefinition,
  type AssetListKind,
  type AssetSummary,
  type AssetVersion,
} from "@/core/shared-assets";

export {
  adminAssetErrorMessage,
  assetLifecycleActions,
  versionWorkflowActions,
} from "./admin-asset-view-model";

const PAGE_META = {
  agents: { label: "Agent", icon: BotIcon },
  skills: { label: "Skill", icon: SparklesIcon },
  "mcp-servers": { label: "MCP", icon: NetworkIcon },
} as const;

export function adminAssetVersionStatus(version: AssetVersion) {
  if (
    "governance_status" in version &&
    version.governance_status === "revoked"
  ) {
    return "revoked" as const;
  }
  return "workflow_status" in version
    ? version.workflow_status
    : version.relation;
}

export function VersionTimeline({
  kind,
  versions,
}: {
  kind: Exclude<AssetListKind, "agents">;
  versions: AssetVersion[];
}) {
  return <AssetVersionHistory kind={kind} scope="system" versions={versions} />;
}

export function filterAdminCatalogItems<T extends AssetSummary>(
  items: T[],
  query: string,
  status: "all" | AssetSummary["status"],
): T[] {
  const normalized = query.trim().toLocaleLowerCase();
  return items.filter(
    (item) =>
      (status === "all" || item.status === status) &&
      (!normalized ||
        item.display_name.toLocaleLowerCase().includes(normalized) ||
        item.slug.toLocaleLowerCase().includes(normalized)),
  );
}

export function AgentDefinitionSummary({
  definition,
  scope,
}: {
  definition: AgentDefinition;
  scope: "project" | "system";
}) {
  const { t } = useI18n();
  const rows = [
    [t.adminAssets.diff.description, definition.description],
    [t.adminAssets.diff.model, definition.model_ref],
    [t.adminAssets.diff.toolGroups, definition.tool_groups.join(", ")],
    [
      t.adminAssets.diff.skillAssets,
      definition.skill_refs
        .map((ref) => `${ref.scope}:${ref.asset_id}`)
        .join(", "),
    ],
    [t.adminAssets.diff.mcpVersions, definition.mcp_version_ids.join(", ")],
  ] as const;
  const documents = [
    ["AGENTS.md", definition.agents_instructions],
    ["SOUL.md", definition.soul],
    ["IDENTITY.md", definition.identity],
    ["USER.md", definition.user_context],
  ] as const;
  return (
    <div data-testid="admin-agent-definition" className="space-y-4">
      <p className="text-muted-foreground text-sm">
        {scope === "system"
          ? t.agents.instructions.readOnlyDescription
          : t.agents.instructions.adminProjectReadOnlyDescription}
      </p>
      <dl className="grid gap-3 text-sm">
        {rows.map(([label, content]) => (
          <div key={label}>
            <dt className="text-muted-foreground text-xs">{label}</dt>
            <dd className="mt-1 whitespace-pre-wrap">{content || "—"}</dd>
          </div>
        ))}
      </dl>
      {documents.map(([name, content]) => (
        <section key={name} className="rounded-lg border p-3">
          <h3 className="font-mono text-xs font-semibold">{name}</h3>
          <pre className="mt-2 overflow-x-auto text-xs whitespace-pre-wrap">
            {content || "—"}
          </pre>
        </section>
      ))}
    </div>
  );
}

function AdminAgentDetail({
  accountId,
  item,
  onClose,
}: {
  accountId: string;
  item: AssetSummary;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const definition = useAdminAgentDefinition(accountId, item.id);
  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle>{item.display_name}</SheetTitle>
          <SheetDescription>
            {item.slug} · {t.adminAssets.catalog.system} Agent
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-5 px-4 pb-6">
          <div className="flex items-center gap-3">
            <AssetStatusBadge status={item.status} />
            <span className="text-muted-foreground text-xs">
              {t.adminAssets.common.assetVersion} {item.revision}
            </span>
          </div>
          {definition.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : definition.error ? (
            <div className="space-y-3">
              <p role="alert" className="text-destructive text-sm">
                {definition.error instanceof Error
                  ? definition.error.message
                  : t.adminAssets.catalog.catalogUnavailable}
              </p>
              <Button
                type="button"
                variant="outline"
                onClick={() => void definition.refetch()}
              >
                {t.adminAssets.common.retry}
              </Button>
            </div>
          ) : definition.data ? (
            <AgentDefinitionSummary
              definition={definition.data.definition}
              scope="system"
            />
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function VersionedAssetDetail({
  accountId,
  kind,
  item,
  onClose,
}: {
  accountId: string;
  kind: Exclude<AssetListKind, "agents">;
  item: AssetSummary;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const history = useAdminAssetVersions(accountId, kind, item.id);
  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle>{item.display_name}</SheetTitle>
          <SheetDescription>
            {item.slug} · {t.adminAssets.catalog.system} {PAGE_META[kind].label}
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-5 px-4 pb-6">
          <div className="flex items-center gap-3">
            <AssetStatusBadge status={item.status} />
            <span className="text-muted-foreground text-xs">
              {t.adminAssets.common.assetVersion} {item.revision}
            </span>
          </div>
          {history.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : history.error ? (
            <div className="space-y-3">
              <p role="alert" className="text-destructive text-sm">
                {history.error instanceof Error
                  ? history.error.message
                  : t.adminAssets.catalog.versionHistoryUnavailable}
              </p>
              <Button
                type="button"
                variant="outline"
                onClick={() => void history.refetch()}
              >
                {t.adminAssets.common.retry}
              </Button>
            </div>
          ) : (
            <AssetVersionHistory
              kind={kind}
              scope="system"
              versions={history.data?.data ?? []}
              currentVersionId={item.current_version_id}
            />
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function AssetDetail(props: {
  accountId: string;
  kind: AssetListKind;
  item: AssetSummary;
  onClose: () => void;
}) {
  if (props.kind === "agents") {
    return (
      <AdminAgentDetail
        accountId={props.accountId}
        item={props.item}
        onClose={props.onClose}
      />
    );
  }
  return <VersionedAssetDetail {...props} kind={props.kind} />;
}

function AuthenticatedAdminAssetPage({
  accountId,
  kind,
}: {
  accountId: string;
  kind: AssetListKind;
}) {
  const { t } = useI18n();
  const query = useAdminAssets(accountId, kind);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<"all" | AssetSummary["status"]>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const items = query.data
    ? filterAdminCatalogItems(query.data.items, search, status)
    : [];
  const selected =
    query.data?.items.find((item) => item.id === selectedId) ?? null;
  const Icon = PAGE_META[kind].icon;

  return (
    <AdminPage className="max-w-[96rem]">
      <header className="flex items-center gap-2">
        <Icon aria-hidden className="text-muted-foreground size-4" />
        <h1 className="font-semibold">System {PAGE_META[kind].label}</h1>
      </header>
      <AdminSection title={t.adminAssets.catalog.assetCatalog}>
        <div className="mb-4 grid gap-3 sm:grid-cols-[1fr_12rem]">
          <label className="relative">
            <SearchIcon
              aria-hidden
              className="text-muted-foreground absolute top-2.5 left-3 size-4"
            />
            <Input
              className="pl-9"
              value={search}
              placeholder={t.adminAssets.catalog.searchPlaceholder}
              aria-label={t.adminAssets.catalog.searchPlaceholder}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <select
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
            value={status}
            aria-label={t.adminAssets.catalog.lifecycleStatus}
            onChange={(event) => setStatus(event.target.value as typeof status)}
          >
            <option value="all">{t.adminAssets.catalog.filterAll}</option>
            <option value="active">{t.adminAssets.status.active}</option>
            <option value="suspended">{t.adminAssets.status.suspended}</option>
            <option value="archived">{t.adminAssets.status.archived}</option>
          </select>
        </div>
        {query.isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-24" />
            <Skeleton className="h-24" />
          </div>
        ) : query.error ? (
          <div className="space-y-3">
            <p role="alert" className="text-destructive text-sm">
              {query.error instanceof Error
                ? query.error.message
                : t.adminAssets.catalog.catalogUnavailable}
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={() => void query.refetch()}
            >
              {t.adminAssets.common.retry}
            </Button>
          </div>
        ) : items.length === 0 ? (
          <p className="text-muted-foreground rounded-xl border border-dashed p-8 text-center text-sm">
            {t.adminAssets.catalog.noResults}
          </p>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {items.map((item) => (
              <Card key={item.id} className="py-4">
                <CardContent className="flex items-center gap-4 px-4">
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-medium">{item.display_name}</p>
                    <p className="text-muted-foreground truncate text-xs">
                      {item.slug}
                    </p>
                  </div>
                  <AssetStatusBadge status={item.status} />
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setSelectedId(item.id)}
                  >
                    {t.adminAssets.catalog.viewDetails}
                  </Button>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </AdminSection>
      {selected ? (
        <AssetDetail
          accountId={accountId}
          kind={kind}
          item={selected}
          onClose={() => setSelectedId(null)}
        />
      ) : null}
    </AdminPage>
  );
}

export function AdminAssetPage({ kind }: { kind: AssetListKind }) {
  const { user } = useAuth();
  if (user === null) return null;
  return <AuthenticatedAdminAssetPage accountId={user.id} kind={kind} />;
}
