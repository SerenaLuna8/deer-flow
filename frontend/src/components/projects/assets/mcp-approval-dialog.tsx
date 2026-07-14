"use client";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { AssetVersion } from "@/core/shared-assets";

type McpVersion = Extract<AssetVersion, { mcp_server_id: string }>;

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
            <option value="">请选择 Credential</option>
            {activeCredentials.map((credential) => (
              <option key={credential.id} value={credential.current_version_id}>
                {credential.display_name} · {credential.name} · 当前版本{" "}
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
  onRetryCredentials,
  onApprove,
}: {
  version: McpVersion;
  pending: boolean;
  credentials: CredentialVersionOption[];
  credentialScope: "system" | "project";
  credentialsLoading?: boolean;
  credentialsError?: unknown;
  onRetryCredentials?: () => void;
  onApprove: (
    version: McpVersion,
    credentialVersions: Record<string, string>,
  ) => void;
}) {
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

        onApprove(
          version,
          Object.fromEntries(entries.filter(([, id]) => id !== "")),
        );
      }}
    >
      {credentialsLoading ? (
        <p role="status" className="text-muted-foreground text-sm">
          正在加载 Credential…
        </p>
      ) : credentialsError ? (
        <div className="space-y-2">
          <p role="alert" className="text-destructive text-sm">
            Credential 列表加载失败，请重试。
          </p>
          {onRetryCredentials && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={onRetryCredentials}
            >
              重试
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
                ? "必填槽位没有可用 Credential。"
                : "当前没有可选 Credential；可选槽位可留空并直接批准。"}
            </p>
          )}
        </>
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
          批准并发布
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
  onRetryCredentials,
  onOpenChange,
  onApprove,
}: {
  version: McpVersion | null;
  open: boolean;
  pending: boolean;
  credentials: CredentialVersionOption[];
  credentialScope: "system" | "project";
  credentialsLoading?: boolean;
  credentialsError?: unknown;
  onRetryCredentials?: () => void;
  onOpenChange: (open: boolean) => void;
  onApprove: (
    version: McpVersion,
    credentialVersions: Record<string, string>,
  ) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>批准 MCP 版本</DialogTitle>
          <DialogDescription>
            为每个 Credential 槽位选择当前作用域可见的已启用 Credential
            当前版本。批准成功后版本才会发布。
          </DialogDescription>
        </DialogHeader>
        {version && (
          <McpApprovalForm
            version={version}
            pending={pending}
            credentials={credentials}
            credentialScope={credentialScope}
            credentialsLoading={credentialsLoading}
            credentialsError={credentialsError}
            onRetryCredentials={onRetryCredentials}
            onApprove={(approvedVersion, bindings) => {
              onApprove(approvedVersion, bindings);
              onOpenChange(false);
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
