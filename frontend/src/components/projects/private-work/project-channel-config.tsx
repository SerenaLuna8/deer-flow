"use client";

import { LoaderCircleIcon, Settings2Icon, Trash2Icon } from "lucide-react";
import type { FormEvent } from "react";

import { ChannelProviderIcon } from "@/components/projects/private-work/channel-provider-icon";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { GatewayApiError } from "@/core/api/errors";
import type {
  ChannelProviderId,
  KnownChannelProviderId,
} from "@/core/private-work/connection-types";
import type {
  ConfigureProjectChannelInstanceInput,
  ProjectChannelInstance,
  ProjectChannelInstanceStatus,
} from "@/core/private-work/connections";
import { cn } from "@/lib/utils";

type PublicChannelField = {
  name: string;
  label: string;
  required: boolean;
  placeholder: string;
};

type SecretChannelField = {
  name: string;
  label: string;
  required: boolean;
};

export type ProjectChannelProviderDescriptor = {
  provider: KnownChannelProviderId;
  displayName: string;
  publicFields: readonly PublicChannelField[];
  secretFields: readonly SecretChannelField[];
};

export const PROJECT_CHANNEL_PROVIDER_DESCRIPTORS = {
  dingtalk: {
    provider: "dingtalk",
    displayName: "钉钉",
    publicFields: [
      {
        name: "client_id",
        label: "Client ID",
        required: true,
        placeholder: "dingxxxxxxxxxxxxxxxx",
      },
    ],
    secretFields: [
      { name: "client_secret", label: "Client Secret", required: true },
    ],
  },
  discord: {
    provider: "discord",
    displayName: "Discord",
    publicFields: [],
    secretFields: [{ name: "bot_token", label: "Bot Token", required: true }],
  },
  feishu: {
    provider: "feishu",
    displayName: "飞书",
    publicFields: [
      {
        name: "app_id",
        label: "App ID",
        required: true,
        placeholder: "cli_xxxxxxxxxxxxxxxx",
      },
      {
        name: "domain",
        label: "Domain",
        required: false,
        placeholder: "https://open.feishu.cn",
      },
    ],
    secretFields: [{ name: "app_secret", label: "App Secret", required: true }],
  },
  slack: {
    provider: "slack",
    displayName: "Slack",
    publicFields: [],
    secretFields: [
      { name: "bot_token", label: "Bot Token", required: true },
      { name: "app_token", label: "App Token", required: true },
    ],
  },
  telegram: {
    provider: "telegram",
    displayName: "Telegram",
    publicFields: [
      {
        name: "bot_username",
        label: "Bot Username",
        required: false,
        placeholder: "actweave_bot",
      },
    ],
    secretFields: [{ name: "bot_token", label: "Bot Token", required: true }],
  },
  wechat: {
    provider: "wechat",
    displayName: "微信",
    publicFields: [],
    secretFields: [{ name: "bot_token", label: "Bot Token", required: true }],
  },
  wecom: {
    provider: "wecom",
    displayName: "企业微信",
    publicFields: [
      {
        name: "bot_id",
        label: "Bot ID",
        required: true,
        placeholder: "bot_xxxxxxxxxxxxxxxx",
      },
    ],
    secretFields: [{ name: "bot_secret", label: "Bot Secret", required: true }],
  },
} as const satisfies Record<
  KnownChannelProviderId,
  ProjectChannelProviderDescriptor
>;

export function projectChannelProviderDescriptor(
  provider: ChannelProviderId,
): ProjectChannelProviderDescriptor | null {
  return (
    PROJECT_CHANNEL_PROVIDER_DESCRIPTORS[provider as KnownChannelProviderId] ??
    null
  );
}

export function projectChannelConfigErrorMessage(
  provider: ChannelProviderId,
  error: unknown,
) {
  const descriptor = projectChannelProviderDescriptor(provider);
  if (error instanceof GatewayApiError) {
    if (error.code === "CHANNEL_INSTANCE_INVALID") {
      const credentialInvalid = error.fields.some(
        (field) => field === "credentials" || field.startsWith("credentials."),
      );
      if (credentialInvalid) {
        const credentialLabel =
          descriptor?.secretFields.length === 1
            ? descriptor.secretFields[0]?.label
            : "渠道凭据";
        return `${credentialLabel ?? "渠道凭据"} 无效，请重新填写后重试。`;
      }
      return "渠道配置无效，请检查填写内容后重试。";
    }
    if (error.code === "CHANNEL_INSTANCE_UNAVAILABLE") {
      return "渠道凭据暂时无法保存，请稍后重试。";
    }
    if (error.code === "CHANNEL_INSTANCE_IDENTITY_CONFLICT") {
      return "该渠道应用已绑定到其他项目。";
    }
    if (error.code === "CHANNEL_INSTANCE_CONFLICT") {
      return "渠道配置已发生变化，请刷新后重试。";
    }
    if (error.code === "CHANNEL_INSTANCE_FORBIDDEN") {
      return "需要项目 Admin 权限才能配置渠道。";
    }
  }
  return `无法保存${descriptor?.displayName ?? "渠道"}配置，请稍后重试。`;
}

export function canManageProjectChannels(capabilities: readonly string[]) {
  return capabilities.includes("project.channels.manage");
}

const PROJECT_CHANNEL_STATUS_LABELS: Record<
  ProjectChannelInstanceStatus,
  string
> = {
  unconfigured: "未配置",
  disabled: "已停用",
  stopped: "已停止",
  starting: "启动中",
  running: "运行正常",
  error: "运行异常",
};

export function channelInstanceStatusLabel(
  status: ProjectChannelInstanceStatus,
) {
  return PROJECT_CHANNEL_STATUS_LABELS[status];
}

function formValue(form: FormData, field: string) {
  const value = form.get(field);
  return typeof value === "string" ? value.trim() : "";
}

export function buildProjectChannelInstanceInput(
  provider: ChannelProviderId,
  configured: boolean,
  form: FormData,
  enabled = true,
): ConfigureProjectChannelInstanceInput {
  const descriptor = projectChannelProviderDescriptor(provider);
  if (!descriptor) throw new Error("暂不支持配置该渠道");

  const publicConfig: Record<string, string> = {};
  for (const field of descriptor.publicFields) {
    const value = formValue(form, field.name);
    if (field.required && !value) throw new Error(`请填写${field.label}`);
    if (value) publicConfig[field.name] = value;
  }

  const credentials: Record<string, string> = {};
  for (const field of descriptor.secretFields) {
    const value = formValue(form, field.name);
    if (!configured && field.required && !value) {
      throw new Error(`请填写${field.label}`);
    }
    if (value) credentials[field.name] = value;
  }

  return {
    publicConfig,
    ...(Object.keys(credentials).length > 0 ? { credentials } : {}),
    enabled,
  };
}

export function submitProjectChannelInstanceForm({
  provider,
  configured,
  enabled = true,
  form,
  clearSecrets,
  onSubmit,
}: {
  provider: ChannelProviderId;
  configured: boolean;
  enabled?: boolean;
  form: FormData;
  clearSecrets: () => void;
  onSubmit: (
    input: ConfigureProjectChannelInstanceInput,
  ) => void | Promise<void>;
}) {
  const input = buildProjectChannelInstanceInput(
    provider,
    configured,
    form,
    enabled,
  );
  clearSecrets();
  return onSubmit(input);
}

function clearProjectChannelSecretInputs(
  form: HTMLFormElement,
  descriptor: ProjectChannelProviderDescriptor,
) {
  for (const field of descriptor.secretFields) {
    const input = form.elements.namedItem(field.name);
    if (input instanceof HTMLInputElement) input.value = "";
  }
}

export function ChannelInstanceConfigDialog({
  instance,
  pending,
  errorMessage,
  onOpenChange,
  onSubmit,
}: {
  instance: ProjectChannelInstance | null;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onSubmit: (
    instance: ProjectChannelInstance,
    input: ConfigureProjectChannelInstanceInput,
  ) => void | Promise<void>;
}) {
  const descriptor = instance
    ? projectChannelProviderDescriptor(instance.provider)
    : null;
  const credentialLabel =
    descriptor?.secretFields.length === 1
      ? descriptor.secretFields[0]?.label
      : "凭据";

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!instance || !descriptor || pending) return;
    const formElement = event.currentTarget;
    await submitProjectChannelInstanceForm({
      provider: instance.provider,
      configured: instance.configured,
      enabled: instance.configured ? instance.enabled : true,
      form: new FormData(formElement),
      clearSecrets: () =>
        clearProjectChannelSecretInputs(formElement, descriptor),
      onSubmit: (input) => onSubmit(instance, input),
    });
  };

  return (
    <Dialog open={instance !== null} onOpenChange={onOpenChange}>
      <DialogContent closeLabel="关闭">
        <DialogHeader>
          <DialogTitle>
            {instance?.configured ? "修改" : "配置"}
            {descriptor?.displayName ?? "渠道"}
          </DialogTitle>
          <DialogDescription>
            {instance?.credential_configured
              ? `${credentialLabel} 已配置；留空表示保留。`
              : ""}
          </DialogDescription>
        </DialogHeader>
        {instance && descriptor ? (
          <form
            className="space-y-4"
            onSubmit={(event) => void handleSubmit(event)}
          >
            {descriptor.publicFields.map((field) => (
              <label key={field.name} className="block space-y-2 text-sm">
                <span className="font-medium">{field.label}</span>
                <Input
                  name={field.name}
                  required={field.required}
                  defaultValue={instance.public_config[field.name] ?? ""}
                  placeholder={field.placeholder}
                  disabled={pending}
                  autoComplete="off"
                />
              </label>
            ))}
            {descriptor.secretFields.map((field) => (
              <label key={field.name} className="block space-y-2 text-sm">
                <span className="font-medium">{field.label}</span>
                <Input
                  name={field.name}
                  type="password"
                  required={field.required && !instance.configured}
                  placeholder={
                    instance.credential_configured ? "已配置，留空表示保留" : ""
                  }
                  disabled={pending}
                  autoComplete="new-password"
                />
              </label>
            ))}
            {errorMessage ? (
              <p role="alert" className="text-destructive text-sm">
                {errorMessage}
              </p>
            ) : null}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={pending}
                onClick={() => onOpenChange(false)}
              >
                取消
              </Button>
              <Button type="submit" disabled={pending}>
                {pending ? <LoaderCircleIcon className="animate-spin" /> : null}
                保存
              </Button>
            </DialogFooter>
          </form>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

const PROJECT_CHANNEL_STATUS_STYLES: Record<
  ProjectChannelInstanceStatus,
  { dot: string; text: string }
> = {
  unconfigured: {
    dot: "bg-muted-foreground/45",
    text: "text-muted-foreground",
  },
  disabled: {
    dot: "bg-muted-foreground/45",
    text: "text-muted-foreground",
  },
  stopped: {
    dot: "bg-muted-foreground/45",
    text: "text-muted-foreground",
  },
  starting: {
    dot: "bg-amber-500",
    text: "text-amber-700 dark:text-amber-400",
  },
  running: {
    dot: "bg-success",
    text: "text-foreground",
  },
  error: {
    dot: "bg-destructive",
    text: "text-destructive",
  },
};

function ChannelInstanceStatusIndicator({
  status,
}: {
  status: ProjectChannelInstanceStatus;
}) {
  const label = channelInstanceStatusLabel(status);
  const styles = PROJECT_CHANNEL_STATUS_STYLES[status];

  return (
    <span
      role="status"
      data-status={status}
      aria-label={`渠道状态：${label}`}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 text-xs font-medium whitespace-nowrap",
        styles.text,
      )}
    >
      <span
        aria-hidden="true"
        className={cn("size-1.5 rounded-full", styles.dot)}
      />
      {label}
    </span>
  );
}

export function ChannelInstanceCard({
  instance,
  manageable,
  pendingAction,
  onConfigure,
  onToggle,
  onDelete,
}: {
  instance: ProjectChannelInstance;
  manageable: boolean;
  pendingAction: "configure" | "enable" | "disable" | "delete" | null;
  onConfigure: (instance: ProjectChannelInstance) => void;
  onToggle: (instance: ProjectChannelInstance, enabled: boolean) => void;
  onDelete: (instance: ProjectChannelInstance) => void;
}) {
  const descriptor = projectChannelProviderDescriptor(instance.provider);
  const pending = pendingAction !== null;
  const publicValues = descriptor?.publicFields
    .map((field) => ({
      label: field.label,
      value: instance.public_config[field.name],
    }))
    .filter((field): field is { label: string; value: string } =>
      Boolean(field.value),
    );
  const credentialLabel =
    descriptor?.secretFields.length === 1
      ? descriptor.secretFields[0]?.label
      : "凭据";

  return (
    <li className="bg-card hover:border-foreground/20 flex min-h-40 flex-col rounded-xl border p-5 transition-colors">
      <div className="flex flex-1 items-start gap-3">
        <div className="bg-muted flex size-10 shrink-0 items-center justify-center rounded-lg">
          <ChannelProviderIcon
            provider={instance.provider}
            className="size-6"
          />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <h3 className="font-semibold">
              {descriptor?.displayName ?? instance.display_name}
            </h3>
            <ChannelInstanceStatusIndicator status={instance.status} />
          </div>
          {publicValues && publicValues.length > 0 ? (
            <dl className="text-muted-foreground mt-3 space-y-1 text-xs">
              {publicValues.map((field) => (
                <div key={field.label} className="flex min-w-0 gap-2">
                  <dt className="shrink-0">{field.label}</dt>
                  <dd className="truncate font-mono">{field.value}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {instance.configured ? (
            <p className="text-muted-foreground mt-2 text-xs">
              {instance.credential_configured
                ? `${credentialLabel} 已配置`
                : "凭据未配置"}
            </p>
          ) : null}
          {instance.last_error ? (
            <p role="alert" className="text-destructive mt-2 text-xs">
              {instance.last_error}
            </p>
          ) : null}
        </div>
      </div>
      {manageable ? (
        <div className="border-border/70 mt-4 flex flex-wrap justify-end gap-2 border-t pt-4">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={pending}
            onClick={() => onConfigure(instance)}
          >
            {pendingAction === "configure" ? (
              <LoaderCircleIcon className="animate-spin" />
            ) : (
              <Settings2Icon />
            )}
            {instance.configured ? "修改" : "配置"}
          </Button>
          {instance.configured ? (
            <>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={pending}
                onClick={() => onToggle(instance, !instance.enabled)}
              >
                {pendingAction === "enable" || pendingAction === "disable" ? (
                  <LoaderCircleIcon className="animate-spin" />
                ) : null}
                {instance.enabled ? "停用" : "启用"}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="text-destructive hover:text-destructive"
                disabled={pending}
                onClick={() => onDelete(instance)}
              >
                {pendingAction === "delete" ? (
                  <LoaderCircleIcon className="animate-spin" />
                ) : (
                  <Trash2Icon />
                )}
                删除
              </Button>
            </>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
