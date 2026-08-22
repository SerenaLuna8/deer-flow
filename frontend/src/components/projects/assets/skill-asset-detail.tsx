import Link from "next/link";
import type { ComponentProps, ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import type { AssetVersion } from "@/core/shared-assets";

import { SkillSecretConfiguration } from "./skill-secret-configuration";
import { SkillVersionWorkbench } from "./skill-version-workbench";

export type SkillAssetVersion = Extract<AssetVersion, { skill_id: string }>;

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const kibibytes = value / 1024;
  if (kibibytes < 1024) return `${Number(kibibytes.toFixed(1))} KB`;
  return `${Number((kibibytes / 1024).toFixed(1))} MB`;
}

const SCAN_LABEL = {
  allow: "通过",
  warn: "警告",
  block: "阻止",
} as const;

export const SKILL_FILE_SNAPSHOT_LIMIT = 20;

type SkillWorkspaceProps = Omit<
  ComponentProps<typeof SkillVersionWorkbench>,
  "version"
> & {
  canManageSecrets: boolean;
  focusSecrets: boolean;
  onSecretsFocused: () => void;
  onSecretsDirtyChange: (dirty: boolean) => void;
};

export function skillSecretConfigurationMounted({
  selectedVersionId,
  editing,
}: {
  selectedVersionId: string;
  editing: boolean;
}): boolean {
  return !editing && selectedVersionId !== "";
}

export function skillSecretConfigurationVisible(
  selectedVersionId: string,
): boolean {
  return skillSecretConfigurationMounted({
    selectedVersionId,
    editing: false,
  });
}

export function resolveSkillSecretConfigurationAccess({
  scope,
  selectedVersionId,
  currentVersionId,
  relation,
  canManageSecrets,
  systemBindingEnabled,
  systemBindingVersionId,
}: {
  scope: "project" | "system";
  selectedVersionId: string;
  currentVersionId: string | null;
  relation: SkillAssetVersion["relation"];
  canManageSecrets: boolean;
  systemBindingEnabled: boolean;
  systemBindingVersionId: string | null;
}): { visible: boolean; canReplace: boolean; canClear: boolean } {
  const projectVersion = scope === "project";
  const projectReplaceable =
    projectVersion &&
    (selectedVersionId === currentVersionId || relation === "candidate");
  const projectClearable =
    projectVersion &&
    (relation === "current" ||
      relation === "candidate" ||
      relation === "historical");
  const boundSystemVersion =
    scope === "system" &&
    systemBindingEnabled &&
    systemBindingVersionId === selectedVersionId;
  return {
    visible: projectVersion || boundSystemVersion,
    canReplace: canManageSecrets && (projectReplaceable || boundSystemVersion),
    canClear: canManageSecrets && (projectClearable || boundSystemVersion),
  };
}

function SkillMetadata({ version }: { version: SkillAssetVersion }) {
  const snapshotFiles = version.file_views.slice(0, SKILL_FILE_SNAPSHOT_LIMIT);
  const remainingFileCount = version.file_views.length - snapshotFiles.length;

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Skill 说明</h3>
        <p className="text-muted-foreground text-sm leading-6 whitespace-pre-wrap">
          {version.description || "未填写说明。"}
        </p>
      </section>

      <section className="grid gap-4 sm:grid-cols-2">
        <div className="border-border/70 rounded-xl border p-4">
          <p className="text-muted-foreground text-xs">兼容性</p>
          <p className="mt-2 text-sm">{version.compatibility ?? "未声明"}</p>
        </div>
        <div className="border-border/70 rounded-xl border p-4">
          <p className="text-muted-foreground text-xs">扫描结果</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Badge
              variant={
                version.scan_decision === "allow" ? "default" : "secondary"
              }
            >
              {SCAN_LABEL[version.scan_decision]}
            </Badge>
            {version.scan_rule_ids.map((rule) => (
              <span key={rule} className="font-mono text-xs">
                {rule}
              </span>
            ))}
          </div>
        </div>
      </section>

      <section className="space-y-3">
        <div>
          <h3 className="text-sm font-semibold">文件快照</h3>
          <p className="text-muted-foreground mt-1 text-xs">
            共 {version.file_views.length}{" "}
            个文件。此处展示版本中已验证的文件元数据
            {remainingFileCount > 0
              ? `，仅列出前 ${SKILL_FILE_SNAPSHOT_LIMIT} 条`
              : ""}
            。
          </p>
        </div>
        {version.file_views.length === 0 ? (
          <p className="text-muted-foreground text-sm">没有文件元数据。</p>
        ) : (
          <div className="divide-border/70 overflow-hidden rounded-xl border">
            {snapshotFiles.map((file) => (
              <div key={file.path} className="space-y-1 px-4 py-3 text-sm">
                <p className="font-mono font-medium break-all">{file.path}</p>
                <p className="text-muted-foreground text-xs">
                  {file.media_type} · {formatBytes(file.size_bytes)}
                </p>
                <p className="text-muted-foreground truncate font-mono text-[11px]">
                  SHA-256 {file.sha256}
                </p>
              </div>
            ))}
            {remainingFileCount > 0 ? (
              <div
                role="status"
                className="bg-muted/25 text-muted-foreground border-t px-4 py-3 text-xs"
              >
                其余 {remainingFileCount}{" "}
                个文件未在此重复渲染，请在上方文件树中按目录查看。
              </div>
            ) : null}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h3 className="text-sm font-semibold">环境变量声明</h3>
        {version.secret_requirements.length === 0 ? (
          <p className="text-muted-foreground text-sm">无需秘密。</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {version.secret_requirements.map((requirement) => (
              <Badge key={requirement.name} variant="secondary">
                {requirement.name}
                {requirement.optional ? " · 可选" : " · 必需"}
              </Badge>
            ))}
          </div>
        )}
      </section>

      <details className="border-border/70 rounded-xl border px-4 py-3">
        <summary className="cursor-pointer text-sm font-medium">
          结构化元数据
        </summary>
        <div className="mt-3 grid gap-3">
          <pre className="bg-muted/45 overflow-x-auto rounded-lg p-3 text-xs">
            {JSON.stringify(version.frontmatter, null, 2)}
          </pre>
          <pre className="bg-muted/45 overflow-x-auto rounded-lg p-3 text-xs">
            {JSON.stringify(version.scan_summary, null, 2)}
          </pre>
        </div>
      </details>
    </div>
  );
}

export function SkillAssetDetail({
  version,
  workspace,
  designRecordHref = null,
}: {
  version: SkillAssetVersion;
  workspace?: SkillWorkspaceProps;
  designRecordHref?: string | null;
}) {
  const { t } = useI18n();
  if (!workspace) return <SkillMetadata version={version} />;

  const {
    canManageSecrets,
    focusSecrets,
    onSecretsFocused,
    onSecretsDirtyChange,
    ...workbench
  } = workspace;
  let secretConfiguration: ReactNode = null;
  if (!workspace.editing) {
    const access = resolveSkillSecretConfigurationAccess({
      scope: workspace.item.scope,
      selectedVersionId: version.id,
      currentVersionId: workspace.item.current_version_id,
      relation: version.relation,
      canManageSecrets,
      systemBindingEnabled: workspace.item.binding?.enabled === true,
      systemBindingVersionId:
        workspace.item.binding?.current_version_id ?? null,
    });
    secretConfiguration = access.visible ? (
      <SkillSecretConfiguration
        key={`${workspace.item.id}:${version.id}`}
        accountId={workspace.accountId}
        projectId={workspace.projectId}
        skillId={workspace.item.id}
        versionId={version.id}
        canReplace={access.canReplace}
        canClear={access.canClear}
        onDirtyChange={onSecretsDirtyChange}
      />
    ) : null;
  }
  return (
    <div className="space-y-8">
      {designRecordHref &&
      !workspace.editing &&
      !workspace.secretConfigurationDirty ? (
        <div>
          <Button asChild size="sm" variant="outline">
            <Link href={designRecordHref}>
              {t.skills.catalog.viewDesignRecord}
            </Link>
          </Button>
        </div>
      ) : null}
      <SkillVersionWorkbench
        {...workbench}
        version={version}
        focusSecrets={focusSecrets}
        onSecretsFocused={onSecretsFocused}
        secretConfiguration={secretConfiguration}
      />
      <details className="border-border/70 rounded-xl border px-4 py-3">
        <summary className="cursor-pointer text-sm font-medium">
          版本说明与检查结果
        </summary>
        <div className="border-border/70 mt-4 border-t pt-4">
          <SkillMetadata version={version} />
        </div>
      </details>
    </div>
  );
}
