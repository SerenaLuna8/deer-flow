"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  ArrowRightIcon,
  ArchiveIcon,
  BotIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  Clock3Icon,
  KeyRoundIcon,
  NetworkIcon,
  PlusIcon,
  RefreshCwIcon,
  SearchIcon,
  SparklesIcon,
  XIcon,
} from "lucide-react";
import {
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { AdminPage, AdminSection } from "@/components/admin/ui/admin-page";
import { AssetStatusBadge } from "@/components/assets/asset-status-badge";
import {
  CredentialDeleteDialog,
  createCredentialDeleteSnapshot,
  type CredentialDeleteSnapshot,
} from "@/components/projects/assets/credential-delete-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import type { Translations } from "@/core/i18n/locales/types";
import {
  adminAssetKey,
  adminAssetVersionsKey,
  createAdminCredential,
  deleteAdminCredential,
  migrateAdminCredentialGrants,
  replaceAdminCredential,
  revokeAdminCredential,
  useAdminAssets,
  useAdminAssetVersions,
  useAdminCredentialRotationStatus,
  useConfigureAdminMcpCredentialGrants,
  type AdminAssetList,
  type AdminCredentialList,
  type AssetListKind,
  type AssetSummary,
  type AssetVersion,
  type ConfigureSystemMcpCredentialGrantsInput,
  type CreateCredentialInput,
  type CredentialMetadata,
  type ReplaceCredentialInput,
} from "@/core/shared-assets";
import { cn } from "@/lib/utils";

import {
  CredentialGrantMigrationDialog,
  CredentialRevokeDialog,
  CredentialSecretDialog,
} from "./admin-asset-dialogs";
import {
  adminAssetCatalogPage,
  adminAssetCatalogSummary,
  adminCredentialPayloadGroupLabel,
  adminCredentialTypeLabel,
  adminMcpTransportLabel,
  adminAssetErrorMessage,
  filterAndSortAdminAssets,
  type AdminAssetCatalogFilters,
  type AdminAssetPublicationFilter,
  type AdminAssetUpdatedSort,
} from "./admin-asset-view-model";
import { CredentialRotationStatusCard } from "./credential-rotation-status";

export {
  adminAssetErrorMessage,
  assetLifecycleActions,
  versionWorkflowActions,
} from "./admin-asset-view-model";

type SystemCatalogKind = Exclude<AssetListKind, "credentials">;
type AdminCatalogItem = AssetSummary | CredentialMetadata;
type AdminCatalogStatus =
  | "all"
  | AssetSummary["status"]
  | CredentialMetadata["status"];
type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;

const PAGE_META = {
  agents: {
    label: "Agent",
    icon: BotIcon,
  },
  skills: {
    label: "Skill",
    icon: SparklesIcon,
  },
  "mcp-servers": {
    label: "MCP",
    icon: NetworkIcon,
  },
  credentials: {
    label: "Credential",
    icon: KeyRoundIcon,
  },
} as const;

function isCredentialMetadata(
  item: AdminCatalogItem,
): item is CredentialMetadata {
  return "credential_type" in item;
}

export function filterAdminCatalogItems<T extends AdminCatalogItem>(
  items: T[],
  query: string,
  status: AdminCatalogStatus,
): T[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  return items.filter((item) => {
    if (status !== "all" && item.status !== status) return false;
    if (!normalizedQuery) return true;
    const searchable = isCredentialMetadata(item)
      ? [item.display_name, item.name, item.credential_type]
      : [item.display_name, item.slug];
    return searchable.some((value) =>
      value.toLocaleLowerCase().includes(normalizedQuery),
    );
  });
}

export function initialMcpCredentialSelections(
  version: McpVersion,
): Record<string, string> {
  const activeGrantBySlotId = new Map(
    version.credential_grants
      .filter((grant) => grant.status === "active")
      .map((grant) => [grant.credential_slot_id, grant] as const),
  );
  return Object.fromEntries(
    version.credential_slots.flatMap((slot) => {
      const grant = activeGrantBySlotId.get(slot.id);
      return grant ? [[slot.name, grant.credential_version_id]] : [];
    }),
  );
}

export function buildMcpCredentialGrantInput(
  version: McpVersion,
  selections: Record<string, string>,
): ConfigureSystemMcpCredentialGrantsInput {
  const slotNames = new Map(
    version.credential_slots.map((slot) => [slot.id, slot.name]),
  );
  const expectedActiveGrantVersions = Object.fromEntries(
    version.credential_grants
      .filter((grant) => grant.status === "active")
      .flatMap((grant) => {
        const slotName = slotNames.get(grant.credential_slot_id);
        return slotName ? [[slotName, grant.version]] : [];
      }),
  );
  return {
    credential_versions: Object.fromEntries(
      Object.entries(selections).filter(([, value]) => value !== ""),
    ),
    expected_active_grant_versions: expectedActiveGrantVersions,
  };
}

export function AdminTechnicalValue({
  className,
  label,
  value,
  valueClassName,
}: {
  className?: string;
  label: string;
  value: string | number | null | undefined;
  valueClassName?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="text-muted-foreground text-xs">{label}</dt>
      <dd
        className={cn(
          "mt-1 min-w-0 font-mono text-xs [overflow-wrap:anywhere]",
          valueClassName,
        )}
        title={
          value === null || value === undefined ? undefined : String(value)
        }
      >
        {value === null || value === undefined || value === "" ? "—" : value}
      </dd>
    </div>
  );
}

function systemPageCopy(
  kind: AssetListKind,
  pages: Translations["adminAssets"]["pages"],
) {
  switch (kind) {
    case "agents":
      return {
        title: pages.system.agentsTitle,
        description: pages.system.agentsDescription,
      };
    case "skills":
      return {
        title: pages.system.skillsTitle,
        description: pages.system.skillsDescription,
      };
    case "mcp-servers":
      return {
        title: pages.system.mcpTitle,
        description: pages.system.mcpDescription,
      };
    case "credentials":
      return {
        title: pages.system.credentialsTitle,
        description: pages.system.credentialsDescription,
      };
  }
}

function localizedAssetKind(
  kind: AssetListKind,
  navigation: Translations["adminAssets"]["navigation"],
) {
  switch (kind) {
    case "agents":
      return navigation.agent;
    case "skills":
      return navigation.skill;
    case "mcp-servers":
      return navigation.mcp;
    case "credentials":
      return navigation.credential;
  }
}

function ErrorNotice({ error }: { error: unknown }) {
  const { t } = useI18n();
  if (!error) return null;
  return (
    <p role="alert" className="text-destructive text-sm">
      {adminAssetErrorMessage(error, t.adminAssets.errors)}
    </p>
  );
}

function versionStatus(version: AssetVersion) {
  return "workflow_status" in version
    ? version.workflow_status
    : version.status;
}

function formatByteCount(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function VersionDetail({ version }: { version: AssetVersion }) {
  const { t } = useI18n();

  return (
    <div
      data-testid={`admin-version-detail-${version.id}`}
      className="border-border/70 bg-muted/15 min-w-0 space-y-5 rounded-xl border p-4"
    >
      <dl className="grid min-w-0 gap-4">
        <AdminTechnicalValue
          label={t.adminAssets.common.versionId}
          value={version.id}
        />
        {"payload_checksum" in version ? (
          <AdminTechnicalValue
            label={t.adminAssets.diff.payloadChecksum}
            value={version.payload_checksum}
          />
        ) : null}
      </dl>

      {"agent_id" in version ? (
        <dl className="grid min-w-0 gap-4 sm:grid-cols-2">
          <AdminTechnicalValue
            className="sm:col-span-2"
            label={t.adminAssets.diff.description}
            value={version.description}
            valueClassName="font-sans leading-relaxed"
          />
          <AdminTechnicalValue
            label={t.adminAssets.diff.model}
            value={version.model_ref}
          />
          <AdminTechnicalValue
            label={t.adminAssets.diff.toolGroups}
            value={version.tool_groups.join(", ")}
          />
          <AdminTechnicalValue
            label={t.adminAssets.diff.skillVersions}
            value={version.skill_version_ids.join(", ")}
          />
          <AdminTechnicalValue
            label={t.adminAssets.diff.mcpVersions}
            value={version.mcp_version_ids.join(", ")}
          />
        </dl>
      ) : null}

      {"skill_id" in version ? (
        <div className="min-w-0 space-y-4">
          <dl className="grid min-w-0 gap-4 sm:grid-cols-2">
            <AdminTechnicalValue
              className="sm:col-span-2"
              label={t.adminAssets.diff.description}
              value={version.description}
              valueClassName="font-sans leading-relaxed"
            />
            <AdminTechnicalValue
              label={t.adminAssets.diff.compatibility}
              value={version.compatibility}
            />
            <AdminTechnicalValue
              label={t.adminAssets.diff.scanDecision}
              value={
                version.scan_decision === "allow"
                  ? t.adminAssets.diff.scanAllow
                  : version.scan_decision === "warn"
                    ? t.adminAssets.diff.scanWarn
                    : t.adminAssets.diff.scanBlock
              }
            />
          </dl>
          <div className="space-y-2">
            <p className="text-muted-foreground text-xs">
              {t.adminAssets.diff.files}
            </p>
            {version.file_views.map((file) => (
              <div
                key={file.path}
                className="border-border/70 min-w-0 rounded-lg border px-3 py-2"
              >
                <p className="min-w-0 text-sm font-medium [overflow-wrap:anywhere]">
                  {file.path}
                </p>
                <p className="text-muted-foreground mt-1 text-xs">
                  {file.media_type} · {formatByteCount(file.size_bytes)}
                </p>
                <p className="text-muted-foreground mt-1 min-w-0 font-mono text-[0.6875rem] [overflow-wrap:anywhere]">
                  SHA-256 · {file.sha256}
                </p>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {"mcp_server_id" in version ? (
        <div className="min-w-0 space-y-4">
          <dl className="grid min-w-0 gap-4 sm:grid-cols-2">
            <AdminTechnicalValue
              className="sm:col-span-2"
              label={t.adminAssets.diff.description}
              value={version.definition.description}
              valueClassName="font-sans leading-relaxed"
            />
            <AdminTechnicalValue
              label={t.adminAssets.diff.transport}
              value={adminMcpTransportLabel(
                version.definition.transport,
                t.adminAssets.common.transportTypes,
              )}
            />
            <AdminTechnicalValue
              label={t.adminAssets.diff.url}
              value={version.definition.url}
            />
            <AdminTechnicalValue
              label={t.adminAssets.diff.command}
              value={version.definition.command}
            />
            <AdminTechnicalValue
              label={t.adminAssets.diff.arguments}
              value={version.definition.args.join(" ")}
            />
            <AdminTechnicalValue
              label={t.adminAssets.diff.timeout}
              value={t.adminAssets.diff.seconds(
                version.definition.timeout_seconds,
              )}
            />
          </dl>
          <div className="space-y-2">
            <p className="text-muted-foreground text-xs">
              {t.adminAssets.diff.credentialSlots}
            </p>
            {version.credential_slots.map((slot) => (
              <div
                key={slot.id}
                className="border-border/70 min-w-0 rounded-lg border px-3 py-2"
              >
                <div className="flex items-center justify-between gap-3">
                  <p className="min-w-0 font-mono text-xs [overflow-wrap:anywhere]">
                    {slot.name}
                  </p>
                  <span className="text-muted-foreground text-xs">
                    {slot.required
                      ? t.adminAssets.diff.required
                      : t.adminAssets.diff.optional}
                  </span>
                </div>
                {slot.purpose ? (
                  <p className="text-muted-foreground mt-1 text-xs">
                    {slot.purpose}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {"credential_id" in version ? (
        <div className="min-w-0 space-y-3">
          <dl>
            <AdminTechnicalValue
              label={t.adminAssets.diff.payloadSchemaVersion}
              value={version.payload_schema_version}
            />
          </dl>
          <div>
            <p className="text-muted-foreground text-xs">
              {t.adminAssets.diff.payloadFields}
            </p>
            <div className="mt-2 grid min-w-0 gap-2">
              {Object.entries(version.payload_schema).map(([group, fields]) => (
                <p
                  key={group}
                  className="border-border/70 min-w-0 rounded-lg border px-3 py-2 font-mono text-xs [overflow-wrap:anywhere]"
                >
                  {adminCredentialPayloadGroupLabel(
                    group,
                    t.adminAssets.common.credentialPayloadGroups,
                  )}
                  : {fields.join(", ")}
                </p>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function VersionTimeline({
  versions,
  currentVersionId,
  configureCredentialGrantsVersionId,
  onConfigureCredentialGrants,
}: {
  versions: AssetVersion[];
  currentVersionId?: string | null;
  configureCredentialGrantsVersionId?: string | null;
  onConfigureCredentialGrants?: (version: McpVersion) => void;
}) {
  const { locale, t } = useI18n();
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(
    () =>
      versions.find((version) => version.id === currentVersionId)?.id ??
      versions[0]?.id ??
      null,
  );
  const selectedVersion =
    versions.find((version) => version.id === selectedVersionId) ??
    versions[0] ??
    null;
  const effectiveSelectedVersionId = selectedVersion?.id ?? null;
  const canSwitchVersions = versions.length > 1;

  if (versions.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        {t.adminAssets.version.none}
      </p>
    );
  }

  return (
    <div className="grid min-w-0 gap-4">
      <div className="border-border/70 min-w-0 overflow-hidden rounded-xl border">
        {versions.map((version) => {
          const selected = effectiveSelectedVersionId === version.id;
          const content = (
            <>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">
                  {t.adminAssets.version.number(version.version_number)}
                </p>
                <time className="text-muted-foreground mt-1 block text-xs">
                  {new Date(version.created_at).toLocaleString(locale)}
                </time>
              </div>
              <AssetStatusBadge status={versionStatus(version)} />
              {canSwitchVersions ? (
                <ChevronRightIcon
                  aria-hidden
                  className={`size-4 shrink-0 transition-transform ${
                    selected ? "rotate-90" : ""
                  }`}
                />
              ) : null}
            </>
          );
          const rowClassName =
            "border-border/70 flex w-full min-w-0 items-center gap-3 border-b px-3 py-3 text-left last:border-b-0";
          return canSwitchVersions ? (
            <button
              key={version.id}
              type="button"
              data-testid={`admin-version-row-${version.id}`}
              aria-pressed={selected}
              className={`${rowClassName} hover:bg-muted/40 focus-visible:ring-ring transition-colors focus-visible:ring-2 focus-visible:outline-none`}
              onClick={() => setSelectedVersionId(version.id)}
            >
              {content}
            </button>
          ) : (
            <div
              key={version.id}
              data-testid={`admin-version-row-${version.id}`}
              className={rowClassName}
            >
              {content}
            </div>
          );
        })}
      </div>
      <div className="min-w-0">
        {selectedVersion ? (
          <div className="min-w-0 space-y-3">
            {"mcp_server_id" in selectedVersion &&
            selectedVersion.id === configureCredentialGrantsVersionId &&
            selectedVersion.credential_slots.length > 0 &&
            onConfigureCredentialGrants ? (
              <Button
                type="button"
                size="sm"
                onClick={() => onConfigureCredentialGrants(selectedVersion)}
              >
                {t.adminAssets.version.configureGrants}
              </Button>
            ) : null}
            <VersionDetail version={selectedVersion} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function AdminMcpCredentialGrantDialog({
  version,
  credentials,
  credentialsLoading,
  credentialsError,
  pending,
  error,
  onRetryCredentials,
  onOpenChange,
  onSave,
}: {
  version: McpVersion;
  credentials: CredentialMetadata[];
  credentialsLoading: boolean;
  credentialsError: unknown;
  pending: boolean;
  error: unknown;
  onRetryCredentials: () => void;
  onOpenChange: (open: boolean) => void;
  onSave: (input: ConfigureSystemMcpCredentialGrantsInput) => Promise<boolean>;
}) {
  const { t } = useI18n();
  const initialSelections = initialMcpCredentialSelections(version);
  const activeCredentials = credentials.filter(
    (credential) =>
      credential.scope === "system" &&
      credential.status === "active" &&
      credential.current_version_id !== null,
  );
  const requiredUnavailable = version.credential_slots.some(
    (slot) =>
      slot.required &&
      !initialSelections[slot.name] &&
      activeCredentials.length === 0,
  );

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent
        closeLabel={t.adminOperations.ui.close}
        className="max-h-[min(42rem,calc(100vh-2rem))] overflow-y-auto sm:max-w-xl"
      >
        <DialogHeader>
          <DialogTitle>
            {t.adminAssets.dialogs.approval.configureTitle}
          </DialogTitle>
          <DialogDescription>
            {t.adminAssets.dialogs.approval.configureDescription}
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            const selections = Object.fromEntries(
              version.credential_slots.map((slot) => {
                const value = form.get(`slot:${slot.name}`);
                return [
                  slot.name,
                  typeof value === "string" ? value.trim() : "",
                ];
              }),
            );
            const missingRequired = version.credential_slots.some(
              (slot) => slot.required && !selections[slot.name],
            );
            if (missingRequired) return;
            void onSave(buildMcpCredentialGrantInput(version, selections)).then(
              (saved) => {
                if (saved) onOpenChange(false);
              },
            );
          }}
        >
          {credentialsLoading ? (
            <p role="status" className="text-muted-foreground text-sm">
              {t.adminAssets.dialogs.approval.loadingCredentials}
            </p>
          ) : credentialsError ? (
            <div className="space-y-2">
              <p role="alert" className="text-destructive text-sm">
                {t.adminAssets.dialogs.approval.credentialsFailed}
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={onRetryCredentials}
              >
                {t.adminAssets.common.retry}
              </Button>
            </div>
          ) : (
            <div className="space-y-4">
              {version.credential_slots.map((slot) => {
                const existingVersionId = initialSelections[slot.name] ?? "";
                const existingIsCurrent = activeCredentials.some(
                  (credential) =>
                    credential.current_version_id === existingVersionId,
                );
                return (
                  <label key={slot.id} className="grid min-w-0 gap-2 text-sm">
                    <span className="flex items-center justify-between gap-3">
                      <span className="min-w-0 font-mono text-xs [overflow-wrap:anywhere]">
                        {slot.name}
                      </span>
                      <span className="text-muted-foreground text-xs">
                        {slot.required
                          ? t.adminAssets.diff.required
                          : t.adminAssets.diff.optional}
                      </span>
                    </span>
                    <select
                      name={`slot:${slot.name}`}
                      required={slot.required}
                      defaultValue={existingVersionId}
                      className="border-input bg-background h-9 min-w-0 rounded-md border px-3 text-sm"
                    >
                      <option value="">
                        {slot.required
                          ? t.adminAssets.dialogs.approval.selectCredential
                          : t.adminAssets.dialogs.approval.clearOptionalGrant}
                      </option>
                      {existingVersionId && !existingIsCurrent ? (
                        <option value={existingVersionId}>
                          {slot.name} · {t.adminAssets.common.active} ·{" "}
                          {existingVersionId}
                        </option>
                      ) : null}
                      {activeCredentials.map((credential) => (
                        <option
                          key={credential.id}
                          value={credential.current_version_id ?? ""}
                        >
                          {credential.display_name} · {credential.name} ·{" "}
                          {t.adminAssets.dialogs.approval.currentVersion}
                        </option>
                      ))}
                    </select>
                    {slot.purpose ? (
                      <span className="text-muted-foreground text-xs">
                        {slot.purpose}
                      </span>
                    ) : null}
                  </label>
                );
              })}
              {version.credential_slots.some((slot) => !slot.required) ? (
                <p className="text-muted-foreground text-xs">
                  {t.adminAssets.dialogs.approval.configureEmptyOptional}
                </p>
              ) : null}
            </div>
          )}
          {requiredUnavailable ? (
            <p role="alert" className="text-destructive text-sm">
              {t.adminAssets.dialogs.approval.requiredUnavailable}
            </p>
          ) : null}
          {error ? <ErrorNotice error={error} /> : null}
          <DialogFooter>
            <Button
              type="submit"
              disabled={
                pending ||
                credentialsLoading ||
                Boolean(credentialsError) ||
                requiredUnavailable
              }
            >
              {t.adminAssets.dialogs.approval.saveGrants}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function CredentialMetadataCard({
  credential,
  pending = false,
  onReplace,
  onMigrate,
  onRevoke,
  onDelete,
}: {
  credential: CredentialMetadata;
  pending?: boolean;
  onReplace: () => void;
  onMigrate: () => void;
  onRevoke: () => void;
  onDelete: () => void;
}) {
  const { locale, t } = useI18n();
  return (
    <Card data-density="compact" className="gap-0 py-0 shadow-none">
      <CardContent className="space-y-5 px-5 py-4">
        <p className="text-muted-foreground text-[0.6875rem] font-semibold tracking-[0.1em] uppercase">
          {t.adminAssets.common.credentialMetadata}
        </p>
        <dl className="grid min-w-0 gap-3 text-sm sm:grid-cols-2">
          <AdminTechnicalValue
            label={t.adminAssets.common.type}
            value={adminCredentialTypeLabel(
              credential.credential_type,
              t.adminAssets.common.credentialTypes,
            )}
          />
          <AdminTechnicalValue
            label={t.adminAssets.common.metadataVersion}
            value={credential.version}
          />
          <div className="sm:col-span-2">
            <dt className="text-muted-foreground text-xs">
              {t.adminAssets.common.updatedAt}
            </dt>
            <dd className="mt-1 text-sm">
              {new Date(credential.updated_at).toLocaleString(locale)}
            </dd>
          </div>
        </dl>

        {credential.status === "active" ? (
          <section className="border-border/70 space-y-3 border-t pt-4">
            {credential.version > 1 ? (
              <div
                role="note"
                className="border-border bg-muted/30 text-muted-foreground rounded-lg border px-3 py-2 text-xs"
              >
                {t.adminAssets.common.credentialRotationNote}
              </div>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={pending}
                onClick={onReplace}
              >
                {t.adminAssets.common.replaceCredential}
              </Button>
              {credential.version > 1 ? (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={pending}
                  onClick={onMigrate}
                >
                  {t.adminAssets.common.migrateReferences}
                </Button>
              ) : null}
            </div>
          </section>
        ) : null}

        <section
          data-testid="credential-danger-zone"
          className="border-destructive/30 bg-destructive/5 space-y-3 rounded-xl border p-4"
        >
          <div className="text-destructive flex items-center gap-2 text-sm font-semibold">
            <AlertTriangleIcon aria-hidden className="size-4" />
            {t.adminAssets.common.dangerZone}
          </div>
          <div className="flex flex-wrap gap-2">
            {credential.status === "active" ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={pending}
                onClick={onRevoke}
              >
                {t.adminAssets.common.revokeCredential}
              </Button>
            ) : null}
            <Button
              type="button"
              size="sm"
              variant="destructive"
              disabled={pending}
              onClick={onDelete}
            >
              {t.adminAssets.common.delete}
            </Button>
          </div>
        </section>
      </CardContent>
    </Card>
  );
}

function useSecureCredentialWrite(accountId: string) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [noticeMessage, setNoticeMessage] = useState<string | null>(null);

  async function run(
    operation: () => Promise<unknown>,
    successMessage?: string,
    onSuccess?: () => void,
  ): Promise<boolean> {
    setPending(true);
    setErrorMessage(null);
    setNoticeMessage(null);
    try {
      await operation();
      onSuccess?.();
      await queryClient.invalidateQueries({
        queryKey: adminAssetKey(accountId, "credentials"),
      });
      setNoticeMessage(successMessage ?? null);
      return true;
    } catch (error) {
      setErrorMessage(adminAssetErrorMessage(error, t.adminAssets.errors));
      return false;
    } finally {
      setPending(false);
    }
  }

  return {
    pending,
    errorMessage,
    noticeMessage,
    run,
    clearError: () => {
      setErrorMessage(null);
      setNoticeMessage(null);
    },
  };
}

function SelectedAssetDetail({
  accountId,
  kind,
  item,
  secureWrite,
  onCredentialDeleted,
}: {
  accountId: string;
  kind: AssetListKind;
  item: AdminCatalogItem;
  secureWrite?: ReturnType<typeof useSecureCredentialWrite>;
  onCredentialDeleted: () => void;
}) {
  const { locale, t } = useI18n();
  const queryClient = useQueryClient();
  const history = useAdminAssetVersions(accountId, kind, item.id);
  const configureGrants = useConfigureAdminMcpCredentialGrants(accountId);
  const [grantVersion, setGrantVersion] = useState<McpVersion | null>(null);
  const credentialCatalog = useAdminAssets(
    accountId,
    "credentials",
    kind === "mcp-servers" && grantVersion !== null,
  );
  const [replaceOpen, setReplaceOpen] = useState(false);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [migrationOpen, setMigrationOpen] = useState(false);
  const [deleteSnapshot, setDeleteSnapshot] =
    useState<CredentialDeleteSnapshot | null>(null);

  const credential = isCredentialMetadata(item) ? item : null;
  const asset = isCredentialMetadata(item) ? null : item;

  return (
    <div className="min-w-0 flex-1 overflow-y-auto px-5 pb-8">
      <section className="min-w-0 space-y-4 py-5">
        {asset ? (
          <dl className="grid min-w-0 gap-4 rounded-xl border p-4 sm:grid-cols-2">
            <AdminTechnicalValue
              label={t.adminAssets.catalog.assetRevision}
              value={asset.version}
            />
            <div>
              <dt className="text-muted-foreground text-xs">
                {t.adminAssets.common.updatedAt}
              </dt>
              <dd className="mt-1 text-sm">
                {new Date(asset.updated_at).toLocaleString(locale)}
              </dd>
            </div>
            <AdminTechnicalValue
              className="sm:col-span-2"
              label={t.adminAssets.common.currentPublishedVersion}
              value={asset.current_published_version_id}
            />
            <AdminTechnicalValue
              className="sm:col-span-2"
              label="UUID"
              value={asset.id}
            />
          </dl>
        ) : credential && secureWrite ? (
          <CredentialMetadataCard
            credential={credential}
            pending={secureWrite.pending}
            onReplace={() => {
              secureWrite.clearError();
              setReplaceOpen(true);
            }}
            onMigrate={() => {
              secureWrite.clearError();
              setMigrationOpen(true);
            }}
            onRevoke={() => {
              secureWrite.clearError();
              setRevokeOpen(true);
            }}
            onDelete={() => {
              secureWrite.clearError();
              setDeleteSnapshot(
                createCredentialDeleteSnapshot(credential, Date.now()),
              );
            }}
          />
        ) : null}
      </section>

      <section
        aria-label={t.adminAssets.common.versionHistory}
        className="border-border/70 min-w-0 border-t pt-5"
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold">
            {t.adminAssets.common.versionHistory}
          </h3>
          {!history.isLoading && !history.error ? (
            <span className="text-muted-foreground text-xs tabular-nums">
              {t.adminAssets.common.versionCount(
                history.data?.data.length ?? 0,
              )}
            </span>
          ) : null}
        </div>
        {history.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : history.error ? (
          <div className="space-y-3">
            <ErrorNotice error={history.error} />
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void history.refetch()}
            >
              {t.adminAssets.common.retry}
            </Button>
          </div>
        ) : (
          <VersionTimeline
            versions={history.data?.data ?? []}
            currentVersionId={
              asset?.current_published_version_id ??
              credential?.current_version_id
            }
            configureCredentialGrantsVersionId={
              asset?.current_published_version_id
            }
            onConfigureCredentialGrants={
              kind === "mcp-servers" ? setGrantVersion : undefined
            }
          />
        )}
      </section>

      {grantVersion ? (
        <AdminMcpCredentialGrantDialog
          key={grantVersion.id}
          version={grantVersion}
          credentials={
            (credentialCatalog.data as AdminCredentialList | undefined)
              ?.items ?? []
          }
          credentialsLoading={credentialCatalog.isLoading}
          credentialsError={credentialCatalog.error}
          pending={configureGrants.isPending}
          error={configureGrants.error}
          onRetryCredentials={() => void credentialCatalog.refetch()}
          onOpenChange={(open) => {
            if (!open) setGrantVersion(null);
          }}
          onSave={async (input) => {
            try {
              await configureGrants.mutateAsync({
                assetId: item.id,
                versionId: grantVersion.id,
                input,
              });
              await history.refetch();
              return true;
            } catch {
              return false;
            }
          }}
        />
      ) : null}

      {credential && secureWrite ? (
        <>
          <CredentialSecretDialog
            mode="replace"
            open={replaceOpen}
            expectedVersion={credential.version}
            pending={secureWrite.pending}
            errorMessage={secureWrite.errorMessage}
            onOpenChange={setReplaceOpen}
            onReplace={(input: ReplaceCredentialInput) => {
              void secureWrite
                .run(() => replaceAdminCredential(credential.id, input))
                .then((success) => {
                  if (!success) return;
                  setReplaceOpen(false);
                  void history.refetch();
                });
            }}
          />
          <CredentialGrantMigrationDialog
            open={migrationOpen}
            credentialName={credential.display_name}
            pending={secureWrite.pending}
            onOpenChange={setMigrationOpen}
            onConfirm={() => {
              void secureWrite
                .run(
                  () =>
                    migrateAdminCredentialGrants(credential.id, {
                      expected_credential_version: credential.version,
                    }),
                  t.adminAssets.common.migrationSuccess,
                )
                .then((success) => success && setMigrationOpen(false));
            }}
          />
          <CredentialRevokeDialog
            open={revokeOpen}
            credentialName={credential.display_name}
            pending={secureWrite.pending}
            onOpenChange={setRevokeOpen}
            onConfirm={() => {
              void secureWrite
                .run(() =>
                  revokeAdminCredential(credential.id, {
                    expected_credential_version: credential.version,
                  }),
                )
                .then((success) => {
                  if (!success) return;
                  setRevokeOpen(false);
                  void history.refetch();
                });
            }}
          />
          {deleteSnapshot ? (
            <CredentialDeleteDialog
              key={`${deleteSnapshot.credentialId}:${deleteSnapshot.startedAt}`}
              snapshot={deleteSnapshot}
              pending={secureWrite.pending}
              errorMessage={secureWrite.errorMessage}
              onOpenChange={(open) => !open && setDeleteSnapshot(null)}
              onConfirm={() => {
                const snapshot = deleteSnapshot;
                void secureWrite
                  .run(
                    () =>
                      deleteAdminCredential(snapshot.credentialId, {
                        expected_credential_version:
                          snapshot.expectedCredentialVersion,
                      }),
                    undefined,
                    () => {
                      queryClient.setQueryData<AdminCredentialList>(
                        adminAssetKey(accountId, "credentials"),
                        (current) =>
                          current
                            ? {
                                ...current,
                                items: current.items.filter(
                                  (candidate) =>
                                    candidate.id !== snapshot.credentialId,
                                ),
                              }
                            : current,
                      );
                      queryClient.removeQueries({
                        queryKey: adminAssetVersionsKey(
                          accountId,
                          "credentials",
                          snapshot.credentialId,
                        ),
                        exact: true,
                      });
                    },
                  )
                  .then((success) => {
                    if (!success) return;
                    onCredentialDeleted();
                  });
              }}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function CredentialCatalogDirectory({
  items,
  selectedId,
  onSelect,
}: {
  items: CredentialMetadata[];
  selectedId: string | null;
  onSelect: (item: CredentialMetadata) => void;
}) {
  const { locale, t } = useI18n();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<AdminCatalogStatus>("all");
  const [requestedPage, setRequestedPage] = useState(1);
  const filteredItems = useMemo(
    () =>
      filterAdminCatalogItems(items, query, status)
        .map((item, index) => ({ item, index }))
        .sort((left, right) => {
          const timeDifference =
            Date.parse(right.item.updated_at) -
            Date.parse(left.item.updated_at);
          return timeDifference === 0
            ? left.index - right.index
            : timeDifference;
        })
        .map(({ item }) => item),
    [items, query, status],
  );
  const page = adminAssetCatalogPage(filteredItems, requestedPage);
  const statuses = useMemo(
    () => Array.from(new Set(items.map((item) => item.status))),
    [items],
  );
  const visibleFrom = page.totalItems === 0 ? 0 : (page.page - 1) * 20 + 1;
  const visibleTo =
    page.totalItems === 0 ? 0 : visibleFrom + page.items.length - 1;

  return (
    <div className="min-w-0 space-y-3">
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_11rem]">
        <label className="relative min-w-0">
          <SearchIcon
            aria-hidden
            className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
          />
          <Input
            value={query}
            onChange={(event) => {
              setQuery(event.currentTarget.value);
              setRequestedPage(1);
            }}
            aria-label={t.adminAssets.catalog.searchPlaceholder}
            placeholder={t.adminAssets.catalog.searchPlaceholder}
            className="pl-9"
          />
        </label>
        <select
          value={status}
          onChange={(event) => {
            setStatus(event.currentTarget.value as AdminCatalogStatus);
            setRequestedPage(1);
          }}
          aria-label={t.adminAssets.diff.status}
          className="border-input bg-background h-9 min-w-0 rounded-md border px-3 text-sm"
        >
          <option value="all">{t.adminAssets.catalog.filterAll}</option>
          {statuses.map((itemStatus) => (
            <option key={itemStatus} value={itemStatus}>
              {t.adminAssets.status[itemStatus]}
            </option>
          ))}
        </select>
      </div>

      <div
        data-testid="admin-credential-table"
        className="border-border/80 bg-card @container min-w-0 overflow-hidden rounded-lg border"
      >
        {page.items.length === 0 ? (
          <div className="bg-muted/15 px-6 py-12 text-center">
            <ArchiveIcon
              aria-hidden
              className="text-muted-foreground mx-auto size-7"
            />
            <p className="text-muted-foreground mt-3 text-sm">
              {items.length === 0
                ? t.adminAssets.pages.emptySystem(
                    t.adminAssets.navigation.credential,
                  )
                : t.adminAssets.catalog.noResults}
            </p>
          </div>
        ) : (
          <>
            <div className="divide-border divide-y @min-[48rem]:hidden">
              {page.items.map((credential) => {
                const selected = credential.id === selectedId;
                return (
                  <button
                    key={credential.id}
                    type="button"
                    aria-pressed={selected}
                    aria-controls={
                      selected ? "admin-asset-inspector" : undefined
                    }
                    aria-expanded={selected}
                    className={cn(
                      "hover:bg-muted/35 focus-visible:ring-ring flex w-full min-w-0 items-start justify-between gap-4 px-4 py-3 text-left focus-visible:ring-2 focus-visible:outline-none",
                      selected && "bg-selection-subtle",
                    )}
                    onClick={() => onSelect(credential)}
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium">
                        {credential.display_name}
                      </span>
                      <span className="text-muted-foreground mt-1 block truncate font-mono text-xs">
                        {credential.name}
                      </span>
                      <span className="mt-2 flex flex-wrap items-center gap-2">
                        <AssetStatusBadge status={credential.status} />
                        <span className="bg-muted text-muted-foreground rounded-md px-2 py-1 text-xs">
                          {adminCredentialTypeLabel(
                            credential.credential_type,
                            t.adminAssets.common.credentialTypes,
                          )}
                        </span>
                      </span>
                    </span>
                    <ChevronRightIcon
                      aria-hidden
                      className="text-muted-foreground mt-1 size-4 shrink-0"
                    />
                  </button>
                );
              })}
            </div>
            <div className="hidden min-w-0 overflow-x-auto @min-[48rem]:block">
              <table className="w-full min-w-[48rem] table-fixed border-collapse text-left text-sm">
                <thead className="bg-muted/35 text-muted-foreground">
                  <tr className="border-border/70 border-b">
                    <th className="w-[23%] px-4 py-2.5 text-xs font-medium">
                      {t.adminAssets.pages.system.credentialsTitle}
                    </th>
                    <th className="w-[22%] px-3 py-2.5 text-xs font-medium">
                      {t.adminAssets.catalog.identifier}
                    </th>
                    <th className="w-[13%] px-3 py-2.5 text-xs font-medium">
                      {t.adminAssets.common.type}
                    </th>
                    <th className="w-[12%] px-3 py-2.5 text-xs font-medium">
                      {t.adminAssets.catalog.lifecycleStatus}
                    </th>
                    <th className="w-[10%] px-3 py-2.5 text-xs font-medium">
                      {t.adminAssets.common.metadataVersion}
                    </th>
                    <th className="w-[14%] px-3 py-2.5 text-xs font-medium">
                      {t.adminAssets.common.updatedAt}
                    </th>
                    <th className="w-[10%] px-3 py-2.5 text-right text-xs font-medium">
                      {t.adminAssets.catalog.actions}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-border divide-y">
                  {page.items.map((credential) => {
                    const selected = credential.id === selectedId;
                    return (
                      <tr
                        key={credential.id}
                        aria-selected={selected}
                        className={cn(
                          "transition-colors",
                          selected
                            ? "bg-selection-subtle shadow-[inset_3px_0_0_var(--selection)]"
                            : "hover:bg-muted/25",
                        )}
                      >
                        <td className="px-4 py-3">
                          <p className="truncate font-medium">
                            {credential.display_name}
                          </p>
                        </td>
                        <td className="px-3 py-3">
                          <p className="truncate font-mono text-xs">
                            {credential.name}
                          </p>
                          <p
                            className="text-muted-foreground mt-1 truncate font-mono text-[0.6875rem]"
                            title={credential.id}
                          >
                            {credential.id}
                          </p>
                        </td>
                        <td className="px-3 py-3">
                          <span className="bg-muted text-muted-foreground rounded-md px-2 py-1 text-xs">
                            {adminCredentialTypeLabel(
                              credential.credential_type,
                              t.adminAssets.common.credentialTypes,
                            )}
                          </span>
                        </td>
                        <td className="px-3 py-3">
                          <AssetStatusBadge status={credential.status} />
                        </td>
                        <td className="px-3 py-3 font-mono text-xs tabular-nums">
                          {credential.version}
                        </td>
                        <td className="px-3 py-3">
                          <time className="text-muted-foreground text-xs">
                            {new Date(credential.updated_at).toLocaleString(
                              locale,
                            )}
                          </time>
                        </td>
                        <td className="px-3 py-3 text-right">
                          <Button
                            type="button"
                            variant="link"
                            size="sm"
                            data-testid={`admin-asset-row-${credential.id}`}
                            aria-pressed={selected}
                            aria-controls={
                              selected ? "admin-asset-inspector" : undefined
                            }
                            aria-expanded={selected}
                            className="text-selection h-auto px-0 text-xs"
                            onClick={() => onSelect(credential)}
                          >
                            {t.adminAssets.catalog.viewDetails}
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
      <nav
        aria-label={t.adminAssets.catalog.page(page.page, page.totalPages)}
        className="flex flex-col gap-3 px-1 sm:flex-row sm:items-center sm:justify-between"
      >
        <p className="text-muted-foreground text-xs tabular-nums">
          {t.adminAssets.catalog.resultRange(
            visibleFrom,
            visibleTo,
            page.totalItems,
          )}
        </p>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="icon-sm"
            aria-label={t.adminAssets.catalog.previousPage}
            disabled={page.page <= 1}
            onClick={() => setRequestedPage(Math.max(1, page.page - 1))}
          >
            <ArrowLeftIcon aria-hidden className="size-3.5" />
          </Button>
          <span className="text-muted-foreground min-w-20 text-center text-xs font-medium tabular-nums">
            {t.adminAssets.catalog.page(page.page, page.totalPages)}
          </span>
          <Button
            type="button"
            variant="outline"
            size="icon-sm"
            aria-label={t.adminAssets.catalog.nextPage}
            disabled={page.page >= page.totalPages}
            onClick={() =>
              setRequestedPage(Math.min(page.totalPages, page.page + 1))
            }
          >
            <ArrowRightIcon aria-hidden className="size-3.5" />
          </Button>
        </div>
      </nav>
    </div>
  );
}

function SystemCatalogMetric({
  className,
  detail,
  icon,
  label,
  value,
}: {
  className?: string;
  detail?: ReactNode;
  icon?: ReactNode;
  label: string;
  value: ReactNode;
}) {
  return (
    <div className={cn("bg-card min-w-0 px-4 py-3.5", className)}>
      <p className="text-muted-foreground text-xs font-medium">{label}</p>
      <div className="mt-2 flex min-w-0 items-center gap-2">
        {icon}
        <p className="min-w-0 text-lg font-semibold tracking-tight tabular-nums">
          {value}
        </p>
      </div>
      {detail ? (
        <div className="text-muted-foreground mt-1 text-xs leading-4">
          {detail}
        </div>
      ) : null}
    </div>
  );
}

function PublicationBadge({ published }: { published: boolean }) {
  const { t } = useI18n();
  return (
    <span
      className={cn(
        "inline-flex w-fit items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
        published
          ? "border-success/25 bg-success/10 text-foreground"
          : "border-border bg-muted text-muted-foreground",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 rounded-full",
          published ? "bg-success" : "bg-muted-foreground/50",
        )}
      />
      {published
        ? t.adminAssets.catalog.published
        : t.adminAssets.catalog.unpublished}
    </span>
  );
}

function SystemCatalogDirectory({
  items,
  kind,
  onRefresh,
  onSelect,
  refreshing,
  selectedId,
}: {
  items: AssetSummary[];
  kind: SystemCatalogKind;
  onRefresh: () => void;
  onSelect: (item: AssetSummary) => void;
  refreshing: boolean;
  selectedId: string | null;
}) {
  const { locale, t } = useI18n();
  const [filters, setFilters] = useState<AdminAssetCatalogFilters>({
    query: "",
    status: "all",
    publication: "all",
    updatedSort: "newest",
  });
  const [requestedPage, setRequestedPage] = useState(1);
  const summary = useMemo(() => adminAssetCatalogSummary(items), [items]);
  const filteredItems = useMemo(
    () => filterAndSortAdminAssets(items, filters),
    [filters, items],
  );
  const catalogPage = useMemo(
    () => adminAssetCatalogPage(filteredItems, requestedPage),
    [filteredItems, requestedPage],
  );
  const statusOptions = useMemo(
    () => Array.from(new Set(items.map((item) => item.status))),
    [items],
  );
  const meta = systemPageCopy(kind, t.adminAssets.pages);
  const visibleFrom =
    catalogPage.totalItems === 0 ? 0 : (catalogPage.page - 1) * 20 + 1;
  const visibleTo =
    catalogPage.totalItems === 0
      ? 0
      : visibleFrom + catalogPage.items.length - 1;

  function updateFilters(patch: Partial<AdminAssetCatalogFilters>) {
    setFilters((current) => ({ ...current, ...patch }));
    setRequestedPage(1);
  }

  return (
    <div className="min-w-0 space-y-4">
      <section
        data-testid="admin-asset-summary"
        aria-label={t.adminAssets.catalog.catalogReady}
        className="bg-border grid min-w-0 grid-cols-2 gap-px overflow-hidden rounded-lg border xl:grid-cols-5"
      >
        <SystemCatalogMetric
          label={t.adminAssets.catalog.catalogReady}
          value={t.adminAssets.common.active}
          detail={t.adminAssets.catalog.catalogReadyDetail}
          icon={
            <span className="border-success/30 bg-success/10 text-success flex size-7 shrink-0 items-center justify-center rounded-full border">
              <CheckCircle2Icon aria-hidden className="size-4" />
            </span>
          }
        />
        <SystemCatalogMetric
          label={t.adminAssets.catalog.totalAssets}
          value={summary.total}
          detail={t.adminAssets.pages.runtimeReadOnly}
        />
        <SystemCatalogMetric
          label={t.adminAssets.catalog.activeAssets}
          value={summary.active}
          detail={
            summary.suspended + summary.archived > 0
              ? `${t.adminAssets.status.suspended} ${summary.suspended} · ${t.adminAssets.status.archived} ${summary.archived}`
              : undefined
          }
        />
        <SystemCatalogMetric
          label={t.adminAssets.catalog.unpublishedAssets}
          value={summary.unpublished}
        />
        <SystemCatalogMetric
          className="col-span-2 xl:col-span-1"
          label={t.adminAssets.catalog.latestUpdate}
          value={
            summary.latestUpdatedAt ? (
              <time className="text-sm leading-5">
                {new Date(summary.latestUpdatedAt).toLocaleString(locale)}
              </time>
            ) : (
              "—"
            )
          }
          detail={
            summary.latestUpdatedAt ? undefined : t.adminAssets.catalog.noUpdate
          }
          icon={<Clock3Icon aria-hidden className="size-4 shrink-0" />}
        />
      </section>

      <section className="min-w-0 space-y-3">
        <div className="min-w-0">
          <div className="min-w-0">
            <h2 className="text-base font-semibold">
              {t.adminAssets.catalog.systemAssets}
            </h2>
            <p className="text-muted-foreground mt-1 text-xs">
              {t.adminAssets.catalog.systemAssetsDescription}
            </p>
          </div>
        </div>

        <div className="grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2 lg:flex lg:flex-row lg:flex-wrap">
          <label className="relative min-w-0 sm:col-span-2 lg:w-60">
            <SearchIcon
              aria-hidden
              className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
            />
            <Input
              value={filters.query}
              onChange={(event) =>
                updateFilters({ query: event.currentTarget.value })
              }
              aria-label={t.adminAssets.catalog.searchPlaceholder}
              placeholder={t.adminAssets.catalog.searchPlaceholder}
              className="pl-9"
            />
          </label>
          <select
            value={filters.status}
            onChange={(event) =>
              updateFilters({
                status: event.currentTarget
                  .value as AdminAssetCatalogFilters["status"],
              })
            }
            aria-label={t.adminAssets.catalog.lifecycleStatus}
            className="border-input bg-background h-9 min-w-0 rounded-md border px-3 text-sm lg:w-32"
          >
            <option value="all">{t.adminAssets.catalog.filterAll}</option>
            {statusOptions.map((status) => (
              <option key={status} value={status}>
                {t.adminAssets.status[status]}
              </option>
            ))}
          </select>
          <select
            data-testid="admin-asset-publication-filter"
            value={filters.publication}
            onChange={(event) =>
              updateFilters({
                publication: event.currentTarget
                  .value as AdminAssetPublicationFilter,
              })
            }
            aria-label={t.adminAssets.catalog.publicationFilter}
            className="border-input bg-background h-9 min-w-0 rounded-md border px-3 text-sm lg:w-36"
          >
            <option value="all">{t.adminAssets.catalog.publicationAll}</option>
            <option value="published">
              {t.adminAssets.catalog.publishedOnly}
            </option>
            <option value="unpublished">
              {t.adminAssets.catalog.unpublishedOnly}
            </option>
          </select>
          <select
            value={filters.updatedSort}
            onChange={(event) =>
              updateFilters({
                updatedSort: event.currentTarget.value as AdminAssetUpdatedSort,
              })
            }
            aria-label={t.adminAssets.catalog.updatedSort}
            className="border-input bg-background h-9 min-w-0 rounded-md border px-3 text-sm lg:w-40"
          >
            <option value="newest">{t.adminAssets.catalog.newestFirst}</option>
            <option value="oldest">{t.adminAssets.catalog.oldestFirst}</option>
          </select>
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="shrink-0 self-end lg:ml-auto"
            aria-label={
              refreshing
                ? t.adminAssets.catalog.refreshing
                : t.adminAssets.catalog.refresh
            }
            title={
              refreshing
                ? t.adminAssets.catalog.refreshing
                : t.adminAssets.catalog.refresh
            }
            disabled={refreshing}
            onClick={onRefresh}
          >
            <RefreshCwIcon
              aria-hidden
              className={cn("size-4", refreshing && "animate-spin")}
            />
          </Button>
        </div>

        <div
          data-testid="admin-asset-table"
          className="border-border/80 bg-card @container min-w-0 overflow-hidden rounded-lg border"
        >
          {catalogPage.items.length === 0 ? (
            <div className="bg-muted/15 px-6 py-12 text-center">
              <ArchiveIcon
                aria-hidden
                className="text-muted-foreground mx-auto size-7"
              />
              <p className="text-muted-foreground mt-3 text-sm">
                {items.length === 0
                  ? t.adminAssets.pages.emptySystem(PAGE_META[kind].label)
                  : t.adminAssets.catalog.noResults}
              </p>
            </div>
          ) : (
            <>
              <div className="divide-border divide-y @min-[52rem]:hidden">
                {catalogPage.items.map((item) => {
                  const selected = selectedId === item.id;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      data-testid={`admin-asset-row-mobile-${item.id}`}
                      aria-pressed={selected}
                      aria-controls={
                        selected ? "admin-asset-inspector" : undefined
                      }
                      aria-expanded={selected}
                      className={cn(
                        "hover:bg-muted/35 focus-visible:ring-ring flex w-full min-w-0 items-start justify-between gap-4 px-4 py-3 text-left focus-visible:ring-2 focus-visible:outline-none",
                        selected && "bg-selection-subtle",
                      )}
                      onClick={() => onSelect(item)}
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-medium">
                          {item.display_name}
                        </span>
                        <span className="text-muted-foreground mt-1 block truncate font-mono text-xs">
                          {item.slug}
                        </span>
                        <span className="mt-2 flex flex-wrap items-center gap-2">
                          <AssetStatusBadge status={item.status} />
                          <PublicationBadge
                            published={
                              item.current_published_version_id !== null
                            }
                          />
                        </span>
                      </span>
                      <ChevronRightIcon
                        aria-hidden
                        className="text-muted-foreground mt-1 size-4 shrink-0"
                      />
                    </button>
                  );
                })}
              </div>
              <div className="hidden min-w-0 overflow-x-auto @min-[52rem]:block">
                <table className="w-full min-w-[52rem] table-fixed border-collapse text-left text-sm">
                  <thead className="bg-muted/35 text-muted-foreground">
                    <tr className="border-border/70 border-b">
                      <th className="w-[20%] px-4 py-2.5 text-xs font-medium">
                        {meta.title}
                      </th>
                      <th className="w-[19%] px-3 py-2.5 text-xs font-medium">
                        {t.adminAssets.catalog.identifier}
                      </th>
                      <th className="w-[11%] px-3 py-2.5 text-xs font-medium">
                        {t.adminAssets.catalog.source}
                      </th>
                      <th className="w-[11%] px-3 py-2.5 text-xs font-medium">
                        {t.adminAssets.catalog.lifecycleStatus}
                      </th>
                      <th className="w-[12%] px-3 py-2.5 text-xs font-medium">
                        {t.adminAssets.catalog.publicationStatus}
                      </th>
                      <th className="w-[9%] px-3 py-2.5 text-xs font-medium">
                        {t.adminAssets.catalog.assetRevision}
                      </th>
                      <th className="w-[12%] px-3 py-2.5 text-xs font-medium">
                        {t.adminAssets.common.updatedAt}
                      </th>
                      <th className="w-[10%] px-3 py-2.5 text-right text-xs font-medium">
                        {t.adminAssets.catalog.actions}
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-border divide-y">
                    {catalogPage.items.map((item) => {
                      const selected = selectedId === item.id;
                      return (
                        <tr
                          key={item.id}
                          aria-selected={selected}
                          className={cn(
                            "transition-colors",
                            selected
                              ? "bg-selection-subtle shadow-[inset_3px_0_0_var(--selection)]"
                              : "hover:bg-muted/25",
                          )}
                        >
                          <td className="min-w-0 px-4 py-3">
                            <p className="truncate font-medium">
                              {item.display_name}
                            </p>
                          </td>
                          <td className="min-w-0 px-3 py-3">
                            <p
                              className="truncate font-mono text-xs"
                              title={item.slug}
                            >
                              {item.slug}
                            </p>
                            <p
                              className="text-muted-foreground mt-1 truncate font-mono text-[0.6875rem]"
                              title={item.id}
                            >
                              {item.id}
                            </p>
                          </td>
                          <td className="px-3 py-3">
                            <span className="bg-selection-subtle text-selection inline-flex rounded-md px-2 py-1 text-xs font-medium">
                              {t.adminAssets.catalog.systemCatalogSource}
                            </span>
                          </td>
                          <td className="px-3 py-3">
                            <AssetStatusBadge status={item.status} />
                          </td>
                          <td className="px-3 py-3">
                            <PublicationBadge
                              published={
                                item.current_published_version_id !== null
                              }
                            />
                          </td>
                          <td className="px-3 py-3 font-mono text-xs tabular-nums">
                            {item.version}
                          </td>
                          <td className="px-3 py-3">
                            <time className="text-muted-foreground block text-xs leading-4">
                              {new Date(item.updated_at).toLocaleString(locale)}
                            </time>
                          </td>
                          <td className="px-3 py-3 text-right">
                            <Button
                              type="button"
                              variant="link"
                              size="sm"
                              data-testid={`admin-asset-row-${item.id}`}
                              aria-pressed={selected}
                              aria-controls={
                                selected ? "admin-asset-inspector" : undefined
                              }
                              aria-expanded={selected}
                              className="text-selection h-auto px-0 text-xs"
                              onClick={() => onSelect(item)}
                            >
                              {t.adminAssets.catalog.viewDetails}
                            </Button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>

        <nav
          data-testid="admin-asset-pagination"
          aria-label={t.adminAssets.catalog.page(
            catalogPage.page,
            catalogPage.totalPages,
          )}
          className="flex flex-col gap-3 px-1 sm:flex-row sm:items-center sm:justify-between"
        >
          <p className="text-muted-foreground text-xs tabular-nums">
            {t.adminAssets.catalog.resultRange(
              visibleFrom,
              visibleTo,
              catalogPage.totalItems,
            )}
          </p>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              aria-label={t.adminAssets.catalog.previousPage}
              disabled={catalogPage.page <= 1}
              onClick={() =>
                setRequestedPage(Math.max(1, catalogPage.page - 1))
              }
            >
              <ArrowLeftIcon aria-hidden className="size-3.5" />
            </Button>
            <span
              aria-current="page"
              className="text-muted-foreground min-w-20 text-center text-xs font-medium tabular-nums"
            >
              {t.adminAssets.catalog.page(
                catalogPage.page,
                catalogPage.totalPages,
              )}
            </span>
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              aria-label={t.adminAssets.catalog.nextPage}
              disabled={catalogPage.page >= catalogPage.totalPages}
              onClick={() =>
                setRequestedPage(
                  Math.min(catalogPage.totalPages, catalogPage.page + 1),
                )
              }
            >
              <ArrowRightIcon aria-hidden className="size-3.5" />
            </Button>
          </div>
        </nav>
      </section>
    </div>
  );
}

const DESKTOP_ASSET_INSPECTOR_QUERY = "(min-width: 1280px)";

function subscribeDesktopAssetInspector(onChange: () => void) {
  if (typeof window === "undefined") return () => undefined;
  const mediaQuery = window.matchMedia(DESKTOP_ASSET_INSPECTOR_QUERY);
  mediaQuery.addEventListener("change", onChange);
  return () => mediaQuery.removeEventListener("change", onChange);
}

function getDesktopAssetInspectorSnapshot() {
  return (
    typeof window !== "undefined" &&
    window.matchMedia(DESKTOP_ASSET_INSPECTOR_QUERY).matches
  );
}

function getServerDesktopAssetInspectorSnapshot() {
  return false;
}

function useDesktopAssetInspector() {
  return useSyncExternalStore(
    subscribeDesktopAssetInspector,
    getDesktopAssetInspectorSnapshot,
    getServerDesktopAssetInspectorSnapshot,
  );
}

export function AdminAssetDesktopInspector({
  children,
  item,
  kind,
  onClose,
}: {
  children?: ReactNode;
  item: AdminCatalogItem;
  kind?: AssetListKind;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const identifier = isCredentialMetadata(item) ? item.name : item.slug;
  const inspectorRef = useRef<HTMLElement>(null);
  const previouslyFocusedElementRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const activeElement = document.activeElement;
    previouslyFocusedElementRef.current =
      activeElement instanceof HTMLElement ? activeElement : null;

    return () => {
      previouslyFocusedElementRef.current?.focus();
    };
  }, []);

  useEffect(() => {
    inspectorRef.current?.focus();
  }, [item.id]);

  return (
    <aside
      id="admin-asset-inspector"
      ref={inspectorRef}
      aria-labelledby="admin-asset-inspector-title"
      data-testid="admin-asset-inspector"
      data-mode="desktop"
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          onClose();
        }
      }}
      className="border-border/70 bg-background fixed top-14 right-0 bottom-0 z-20 flex w-[clamp(32rem,34vw,48rem)] max-w-[calc(100vw-4rem)] min-w-0 flex-col border-l shadow-[-12px_0_30px_-24px_rgba(15,23,42,0.35)] focus:outline-none"
    >
      <header className="border-border/70 relative min-w-0 border-b px-6 py-5 pr-16">
        {kind ? (
          <p className="text-muted-foreground mb-2 text-xs font-medium">
            {localizedAssetKind(kind, t.adminAssets.navigation)}{" "}
            {t.adminAssets.common.details}
          </p>
        ) : null}
        <h2
          id="admin-asset-inspector-title"
          className="min-w-0 text-lg leading-6 font-semibold [overflow-wrap:anywhere]"
        >
          {item.display_name}
        </h2>
        <p className="text-muted-foreground mt-1 min-w-0 font-mono text-xs leading-5 [overflow-wrap:anywhere]">
          {identifier}
        </p>
        <div className="mt-3 flex items-center">
          <AssetStatusBadge status={item.status} />
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t.adminOperations.ui.close}
          title={t.adminOperations.ui.close}
          className="absolute top-4 right-4"
          onClick={onClose}
        >
          <XIcon aria-hidden className="size-4" />
        </Button>
      </header>
      {children}
    </aside>
  );
}

function SelectedAssetSheet({
  accountId,
  kind,
  item,
  secureWrite,
  onClose,
}: {
  accountId: string;
  kind: AssetListKind;
  item: AdminCatalogItem | null;
  secureWrite?: ReturnType<typeof useSecureCredentialWrite>;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const isDesktopInspector = useDesktopAssetInspector();

  if (!item) return null;

  const detail = (
    <SelectedAssetDetail
      key={item.id}
      accountId={accountId}
      kind={kind}
      item={item}
      secureWrite={secureWrite}
      onCredentialDeleted={onClose}
    />
  );

  if (isDesktopInspector) {
    return (
      <AdminAssetDesktopInspector item={item} kind={kind} onClose={onClose}>
        {detail}
      </AdminAssetDesktopInspector>
    );
  }

  return (
    <Sheet
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent
        id="admin-asset-inspector"
        closeLabel={t.adminOperations.ui.close}
        className="inset-0 h-dvh w-screen max-w-none min-w-0 gap-0 overflow-hidden border-0 p-0 sm:max-w-none"
      >
        <SheetHeader className="border-border/70 min-w-0 border-b px-6 py-5 pr-16">
          <p className="text-muted-foreground text-left text-xs font-medium">
            {localizedAssetKind(kind, t.adminAssets.navigation)}{" "}
            {t.adminAssets.common.details}
          </p>
          <SheetTitle className="min-w-0 text-lg leading-6 [overflow-wrap:anywhere]">
            {item.display_name}
          </SheetTitle>
          <SheetDescription className="mt-1 min-w-0 font-mono text-xs leading-5 [overflow-wrap:anywhere]">
            {isCredentialMetadata(item) ? item.name : item.slug}
          </SheetDescription>
          <div className="mt-2 flex items-center">
            <AssetStatusBadge status={item.status} />
          </div>
        </SheetHeader>
        {detail}
      </SheetContent>
    </Sheet>
  );
}

export function CredentialWriteError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="border-destructive/30 bg-destructive/5 text-destructive mb-6 rounded-xl border px-4 py-3 text-sm"
    >
      {message}
    </div>
  );
}

function CredentialList({
  accountId,
  data,
}: {
  accountId: string;
  data: AdminCredentialList;
}) {
  const { t } = useI18n();
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const secureWrite = useSecureCredentialWrite(accountId);
  const rotationStatus = useAdminCredentialRotationStatus(accountId);
  const selected =
    data.items.find((credential) => credential.id === selectedId) ?? null;

  return (
    <div className="min-w-0">
      <div className="mb-4">
        {rotationStatus.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : rotationStatus.error ? (
          <div className="border-destructive/30 bg-destructive/5 rounded-xl border p-4">
            <ErrorNotice error={rotationStatus.error} />
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-3"
              disabled={rotationStatus.isFetching}
              onClick={() => void rotationStatus.refetch()}
            >
              <RefreshCwIcon
                aria-hidden
                className={cn(
                  "size-3.5",
                  rotationStatus.isFetching &&
                    "animate-spin motion-reduce:animate-none",
                )}
              />
              {rotationStatus.isFetching
                ? t.adminAssets.common.retrying
                : t.adminAssets.common.retry}
            </Button>
          </div>
        ) : rotationStatus.data ? (
          <CredentialRotationStatusCard status={rotationStatus.data} />
        ) : null}
      </div>
      <CredentialWriteError message={secureWrite.errorMessage} />
      {secureWrite.noticeMessage ? (
        <p
          role="status"
          className="border-border bg-muted/30 text-muted-foreground mb-6 rounded-xl border px-4 py-3 text-sm"
        >
          {secureWrite.noticeMessage}
        </p>
      ) : null}
      <div className="mb-4 flex items-center justify-between gap-3">
        <p className="text-muted-foreground text-xs">
          {t.adminAssets.pages.credentialCount(data.items.length)}
        </p>
        <Button
          type="button"
          onClick={() => {
            secureWrite.clearError();
            setCreateOpen(true);
          }}
        >
          <PlusIcon aria-hidden className="size-4" />
          {t.adminAssets.common.createCredential}
        </Button>
      </div>
      {data.items.length === 0 ? (
        <EmptyState label={t.adminAssets.navigation.credential} />
      ) : (
        <CredentialCatalogDirectory
          items={data.items}
          selectedId={selectedId}
          onSelect={(credential) => setSelectedId(credential.id)}
        />
      )}
      <SelectedAssetSheet
        accountId={accountId}
        kind="credentials"
        item={selected}
        secureWrite={secureWrite}
        onClose={() => setSelectedId(null)}
      />
      <CredentialSecretDialog
        mode="create"
        open={createOpen}
        pending={secureWrite.pending}
        errorMessage={secureWrite.errorMessage}
        onOpenChange={setCreateOpen}
        onCreate={(input: CreateCredentialInput) => {
          void secureWrite
            .run(() => createAdminCredential(input))
            .then((success) => success && setCreateOpen(false));
        }}
      />
    </div>
  );
}

function EmptyState({ label }: { label: string }) {
  const { t } = useI18n();
  return (
    <div className="border-border/70 bg-muted/20 rounded-xl border border-dashed px-6 py-12 text-center">
      <ArchiveIcon
        aria-hidden
        className="text-muted-foreground mx-auto size-8"
      />
      <p className="mt-3 font-medium">
        {t.adminAssets.pages.emptySystem(label)}
      </p>
    </div>
  );
}

function SystemAssetList({
  accountId,
  kind,
  data,
  onRefresh,
  refreshing,
}: {
  accountId: string;
  kind: SystemCatalogKind;
  data: AdminAssetList;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = data.items.find((asset) => asset.id === selectedId) ?? null;

  return (
    <div className="min-w-0 space-y-4">
      <SystemCatalogDirectory
        kind={kind}
        items={data.items}
        selectedId={selectedId}
        onSelect={(asset) => setSelectedId(asset.id)}
        onRefresh={onRefresh}
        refreshing={refreshing}
      />
      <SelectedAssetSheet
        accountId={accountId}
        kind={kind}
        item={selected}
        onClose={() => setSelectedId(null)}
      />
    </div>
  );
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
  const meta = systemPageCopy(kind, t.adminAssets.pages);
  const Icon = PAGE_META[kind].icon;

  return (
    <AdminPage className="max-w-[96rem]">
      <header
        data-testid="admin-asset-breadcrumb"
        className="min-w-0 space-y-1"
      >
        <div className="flex min-w-0 items-center gap-2 text-sm">
          <Icon aria-hidden className="text-muted-foreground size-4 shrink-0" />
          <span className="text-muted-foreground">
            {t.adminAssets.pages.systemEyebrow}
          </span>
          <span aria-hidden className="text-border">
            /
          </span>
          <h1 className="truncate font-semibold">{meta.title}</h1>
        </div>
        <p className="text-muted-foreground max-w-3xl text-sm leading-5">
          {meta.description}
        </p>
      </header>
      {query.isLoading ? (
        <div
          role="status"
          aria-busy="true"
          aria-label={t.adminAssets.pages.loading}
          className="space-y-4"
        >
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : query.error ? (
        <AdminSection
          title={t.adminAssets.pages.loadFailed}
          className="border-destructive/30 bg-destructive/5"
        >
          <ErrorNotice error={query.error} />
          <Button
            type="button"
            className="mt-4"
            variant="outline"
            onClick={() => void query.refetch()}
          >
            {t.adminAssets.common.retry}
          </Button>
        </AdminSection>
      ) : kind === "credentials" ? (
        <CredentialList
          accountId={accountId}
          data={query.data as AdminCredentialList}
        />
      ) : (
        <SystemAssetList
          accountId={accountId}
          kind={kind}
          data={query.data as AdminAssetList}
          onRefresh={() => void query.refetch()}
          refreshing={query.isFetching}
        />
      )}
    </AdminPage>
  );
}

export function AdminAssetPage({ kind }: { kind: AssetListKind }) {
  const { user } = useAuth();
  if (user === null) return null;
  return <AuthenticatedAdminAssetPage accountId={user.id} kind={kind} />;
}
