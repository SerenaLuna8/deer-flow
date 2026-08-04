"use client";

import {
  adminAssetErrorMessage,
  projectMcpCredentialErrorMessage,
} from "@/components/admin/assets/admin-asset-view-model";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import type { AssetVersion } from "@/core/shared-assets";

type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;
type McpApprovalResult = boolean | void;
type McpApprovalHandler = (
  version: McpVersion,
  credentialVersions: Record<string, string>,
) => McpApprovalResult | Promise<McpApprovalResult>;

type McpApprovalCopy = Translations["adminAssets"]["dialogs"]["approval"];

export function mcpApprovalCopy(
  mode: "publish" | "configure-grants",
  localized?: McpApprovalCopy,
) {
  if (mode === "configure-grants") {
    return {
      title: localized?.configureTitle ?? "配置 MCP Credential 授权",
      description:
        localized?.configureDescription ??
        "为已发布的 packaged System MCP 选择系统 Credential。此操作只配置槽位授权，不修改或重新发布 MCP 定义。",
      submitLabel: localized?.saveGrants ?? "保存授权",
      emptyOptionalMessage:
        localized?.configureEmptyOptional ??
        "当前没有可选 Credential；可选槽位可留空并保存，以清除既有授权。",
    } as const;
  }
  return {
    title: localized?.publishTitle ?? "批准 MCP 配置",
    description:
      localized?.publishDescription ??
      "为每个 Credential 槽位选择当前作用域可见的已启用 Credential。批准成功后配置才会发布。",
    submitLabel: localized?.approve ?? "批准并发布配置",
    emptyOptionalMessage:
      localized?.publishEmptyOptional ??
      "当前没有可选 Credential；可选槽位可留空并直接批准。",
  } as const;
}

export async function settleMcpApproval(
  operation: () => McpApprovalResult | Promise<McpApprovalResult>,
): Promise<boolean> {
  try {
    return (await operation()) !== false;
  } catch {
    return false;
  }
}

export type CredentialVersionOption = {
  id: string;
  scope: "system" | "project";
  name: string;
  display_name: string;
  credential_type: string;
  status: string;
  current_version_id: string | null;
};

type EligibleCredentialVersion = CredentialVersionOption & {
  current_version_id: string;
};

function isEligibleCredentialVersion(
  credential: CredentialVersionOption,
  credentialScope: "system" | "project",
): credential is EligibleCredentialVersion {
  return (
    credential.scope === credentialScope &&
    credential.status === "active" &&
    credential.current_version_id !== null
  );
}

export function McpCredentialSelectors({
  version,
  credentials,
  credentialScope,
}: {
  version: McpVersion;
  credentials: CredentialVersionOption[];
  credentialScope: "system" | "project";
}) {
  const { t } = useI18n();
  const activeCredentials = credentials.filter((credential) =>
    isEligibleCredentialVersion(credential, credentialScope),
  );
  return (
    <>
      {version.credential_slots.map((slot) => (
        <label key={slot.id} className="grid gap-2 text-sm">
          {slot.name} Credential
          <select
            name={`slot:${slot.name}`}
            required={slot.required}
            defaultValue=""
            className="border-input bg-background h-9 rounded-md border px-3 text-sm"
          >
            <option value="">
              {t.adminAssets.dialogs.approval.selectCredential}
            </option>
            {activeCredentials.map((credential) => (
              <option key={credential.id} value={credential.current_version_id}>
                {credential.display_name} · {credential.name} ·{" "}
                {t.adminAssets.dialogs.approval.currentVersion}{" "}
                {credential.current_version_id}
              </option>
            ))}
          </select>
        </label>
      ))}
    </>
  );
}

export function McpApprovalForm({
  version,
  pending,
  credentials,
  credentialScope,
  credentialsLoading = false,
  credentialsError,
  approvalError,
  onRetryCredentials,
  onApprove,
  submitLabel,
  emptyOptionalMessage,
}: {
  version: McpVersion;
  pending: boolean;
  credentials: CredentialVersionOption[];
  credentialScope: "system" | "project";
  credentialsLoading?: boolean;
  credentialsError?: unknown;
  approvalError?: unknown;
  onRetryCredentials?: () => void;
  onApprove: McpApprovalHandler;
  submitLabel?: string;
  emptyOptionalMessage?: string;
}) {
  const { t } = useI18n();
  const localizedSubmitLabel =
    submitLabel ?? t.adminAssets.dialogs.approval.approve;
  const localizedEmptyOptionalMessage =
    emptyOptionalMessage ?? t.adminAssets.dialogs.approval.publishEmptyOptional;
  const hasRequiredSlots = version.credential_slots.some(
    (slot) => slot.required,
  );
  const hasEligibleCredential = credentials.some((credential) =>
    isEligibleCredentialVersion(credential, credentialScope),
  );
  const requiredCredentialsUnavailable =
    hasRequiredSlots && !hasEligibleCredential;

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        const entries = version.credential_slots.map((slot) => {
          const value = form.get(`slot:${slot.name}`);
          return [
            slot.name,
            typeof value === "string" ? value.trim() : "",
          ] as const;
        });
        const missingRequiredSlot = version.credential_slots.some(
          (slot) =>
            slot.required &&
            entries.find(([slotName]) => slotName === slot.name)?.[1] === "",
        );
        if (missingRequiredSlot) return;

        void onApprove(
          version,
          Object.fromEntries(entries.filter(([, id]) => id !== "")),
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
          {onRetryCredentials && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={onRetryCredentials}
            >
              {t.adminAssets.common.retry}
            </Button>
          )}
        </div>
      ) : (
        <>
          <McpCredentialSelectors
            version={version}
            credentials={credentials}
            credentialScope={credentialScope}
          />
          {!hasEligibleCredential && version.credential_slots.length > 0 && (
            <p
              role={hasRequiredSlots ? "alert" : "status"}
              className={
                hasRequiredSlots
                  ? "text-destructive text-sm"
                  : "text-muted-foreground text-sm"
              }
            >
              {hasRequiredSlots
                ? t.adminAssets.dialogs.approval.requiredUnavailable
                : localizedEmptyOptionalMessage}
            </p>
          )}
        </>
      )}
      {Boolean(approvalError) && (
        <p role="alert" className="text-destructive text-sm">
          {credentialScope === "project"
            ? projectMcpCredentialErrorMessage(
                approvalError,
                t.adminAssets.errors,
              )
            : adminAssetErrorMessage(approvalError, t.adminAssets.errors)}
        </p>
      )}
      <DialogFooter>
        <Button
          type="submit"
          disabled={
            pending ||
            credentialsLoading ||
            Boolean(credentialsError) ||
            requiredCredentialsUnavailable
          }
        >
          {localizedSubmitLabel}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function McpApprovalDialog({
  version,
  open,
  pending,
  credentials,
  credentialScope,
  credentialsLoading = false,
  credentialsError,
  approvalError,
  onRetryCredentials,
  onOpenChange,
  onApprove,
  mode = "publish",
}: {
  version: McpVersion | null;
  open: boolean;
  pending: boolean;
  credentials: CredentialVersionOption[];
  credentialScope: "system" | "project";
  credentialsLoading?: boolean;
  credentialsError?: unknown;
  approvalError?: unknown;
  onRetryCredentials?: () => void;
  onOpenChange: (open: boolean) => void;
  onApprove: McpApprovalHandler;
  mode?: "publish" | "configure-grants";
}) {
  const { t } = useI18n();
  const copy = mcpApprovalCopy(mode, t.adminAssets.dialogs.approval);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          <DialogDescription>{copy.description}</DialogDescription>
        </DialogHeader>
        {version && (
          <McpApprovalForm
            version={version}
            pending={pending}
            credentials={credentials}
            credentialScope={credentialScope}
            credentialsLoading={credentialsLoading}
            credentialsError={credentialsError}
            approvalError={approvalError}
            onRetryCredentials={onRetryCredentials}
            submitLabel={copy.submitLabel}
            emptyOptionalMessage={copy.emptyOptionalMessage}
            onApprove={(approvedVersion, bindings) =>
              void settleMcpApproval(() =>
                onApprove(approvedVersion, bindings),
              ).then((approved) => {
                if (approved) onOpenChange(false);
              })
            }
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
