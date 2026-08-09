"use client";

import { Loader2Icon, SearchIcon, WrenchIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  SharedAssetApiError,
  useProjectAssets,
  useUpdateProjectAgentCapabilityBindings,
  type AssetVersion,
  type ProjectAssetItem,
  type ProjectAssetList,
} from "@/core/shared-assets";

import { useMcpDependencyRuntime } from "./use-mcp-dependency-runtime";

type AgentAssetVersion = Extract<AssetVersion, { agent_id: string }>;
type DependencyKind = "skill" | "mcp";

export type AgentDependencyOption = {
  kind: DependencyKind;
  assetId: string;
  versionId: string;
  scope: "system" | "project";
  name: string;
  slug: string;
};

export function agentDependencyOptions(
  kind: DependencyKind,
  catalog: ProjectAssetList,
): AgentDependencyOption[] {
  const systemOptions = catalog.system_items.flatMap((item) =>
    item.status === "active" && item.binding?.enabled
      ? [
          {
            kind,
            assetId: item.id,
            versionId: item.binding.version_id,
            scope: "system" as const,
            name: item.display_name,
            slug: item.slug,
          },
        ]
      : [],
  );
  const projectOptions = catalog.project_items.flatMap((item) =>
    item.status === "active" && item.current_published_version_id
      ? [
          {
            kind,
            assetId: item.id,
            versionId: item.current_published_version_id,
            scope: "project" as const,
            name: item.display_name,
            slug: item.slug,
          },
        ]
      : [],
  );
  return [...systemOptions, ...projectOptions];
}

function sameIds(left: readonly string[], right: readonly string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function toggleId(values: readonly string[], value: string): string[] {
  return values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
}

function DependencySection({
  title,
  emptyLabel,
  options,
  selectedIds,
  editing,
  query,
  onToggle,
}: {
  title: string;
  emptyLabel: string;
  options: readonly AgentDependencyOption[];
  selectedIds: readonly string[];
  editing: boolean;
  query: string;
  onToggle: (versionId: string) => void;
}) {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = options.filter((option) => {
    if (!normalizedQuery) return true;
    return [option.name, option.slug].some((value) =>
      value.toLocaleLowerCase().includes(normalizedQuery),
    );
  });
  const knownIds = new Set(options.map((option) => option.versionId));
  const unresolvedIds = selectedIds.filter((id) => !knownIds.has(id));

  return (
    <section className="space-y-3" aria-label={title}>
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold">{title}</h3>
        <Badge variant="secondary">已绑定 {selectedIds.length}</Badge>
      </div>
      {visible.length === 0 && unresolvedIds.length === 0 ? (
        <p className="text-muted-foreground rounded-xl border border-dashed p-4 text-sm">
          {emptyLabel}
        </p>
      ) : (
        <div className="border-border/70 max-h-72 overflow-y-auto rounded-xl border">
          {visible.map((option) => {
            const checked = selectedIds.includes(option.versionId);
            return (
              <label
                key={option.versionId}
                className="hover:bg-muted/40 flex cursor-pointer items-start gap-3 border-b px-4 py-3 last:border-b-0"
              >
                <input
                  type="checkbox"
                  className="accent-foreground mt-1 size-4 shrink-0"
                  checked={checked}
                  disabled={!editing}
                  onChange={() => onToggle(option.versionId)}
                />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{option.name}</span>
                    <Badge variant="outline">
                      {option.scope === "system" ? "系统" : "项目"}
                    </Badge>
                  </span>
                  <span className="text-muted-foreground mt-1 block font-mono text-xs">
                    {option.slug}
                  </span>
                </span>
              </label>
            );
          })}
          {unresolvedIds.map((versionId) => (
            <label
              key={versionId}
              className="bg-warning/5 flex cursor-pointer items-start gap-3 border-b px-4 py-3 last:border-b-0"
            >
              <input
                type="checkbox"
                className="accent-foreground mt-1 size-4 shrink-0"
                checked
                disabled={!editing}
                onChange={() => onToggle(versionId)}
              />
              <span className="min-w-0 flex-1">
                <span className="text-sm font-medium">历史绑定版本</span>
                <span className="text-muted-foreground mt-1 block font-mono text-xs break-all">
                  {versionId}
                </span>
                <span className="text-muted-foreground mt-1 block text-xs">
                  当前目录未提供该精确版本；可以保留或取消绑定。
                </span>
              </span>
            </label>
          ))}
        </div>
      )}
    </section>
  );
}

export function AgentCapabilityWorkbench({
  accountId,
  projectId,
  item,
  version,
  canAuthor,
  onDirtyChange,
  onVersionCreated,
}: {
  accountId: string;
  projectId: string;
  item: ProjectAssetItem;
  version: AgentAssetVersion | null;
  canAuthor: boolean;
  onDirtyChange: (dirty: boolean) => void;
  onVersionCreated: (versionId: string) => void;
}) {
  const skills = useProjectAssets(accountId, projectId, "skills");
  const mcps = useProjectAssets(accountId, projectId, "mcp-servers");
  const update = useUpdateProjectAgentCapabilityBindings(accountId, projectId);
  const initialSkills = version?.skill_version_ids ?? [];
  const initialMcps = version?.mcp_version_ids ?? [];
  const [baselineSkills, setBaselineSkills] = useState(initialSkills);
  const [baselineMcps, setBaselineMcps] = useState(initialMcps);
  const [draftSkills, setDraftSkills] = useState(initialSkills);
  const [draftMcps, setDraftMcps] = useState(initialMcps);
  const [editing, setEditing] = useState(false);
  const [query, setQuery] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const savedVersionIdRef = useRef<string | null>(null);
  const expectedAssetVersionRef = useRef(item.version);
  const dirty =
    !sameIds(baselineSkills, draftSkills) || !sameIds(baselineMcps, draftMcps);
  const skillOptions = useMemo(
    () =>
      skills.data
        ? agentDependencyOptions("skill", skills.data as ProjectAssetList)
        : [],
    [skills.data],
  );
  const mcpOptions = useMemo(
    () =>
      mcps.data
        ? agentDependencyOptions("mcp", mcps.data as ProjectAssetList)
        : [],
    [mcps.data],
  );
  const mcpRuntime = useMcpDependencyRuntime({
    accountId,
    projectId,
    requiredVersionIds: draftMcps,
    enabled: editing,
  });
  const catalogLoading = skills.isLoading || mcps.isLoading;
  const catalogError = skills.error ?? mcps.error;

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(
    () => () => {
      onDirtyChange(false);
    },
    [onDirtyChange],
  );

  useEffect(() => {
    if (dirty || update.isPending) return;
    if (
      savedVersionIdRef.current &&
      version?.id !== savedVersionIdRef.current
    ) {
      return;
    }
    savedVersionIdRef.current = null;
    const nextSkills = version?.skill_version_ids ?? [];
    const nextMcps = version?.mcp_version_ids ?? [];
    setBaselineSkills(nextSkills);
    setBaselineMcps(nextMcps);
    setDraftSkills(nextSkills);
    setDraftMcps(nextMcps);
    expectedAssetVersionRef.current = item.version;
  }, [dirty, item.version, update.isPending, version]);

  function discard() {
    setDraftSkills(baselineSkills);
    setDraftMcps(baselineMcps);
    setEditing(false);
    setLocalError(null);
  }

  async function save() {
    setLocalError(null);
    try {
      const result = await update.mutateAsync({
        assetId: item.id,
        input: {
          skill_version_ids: draftSkills,
          mcp_version_ids: draftMcps,
          expected_asset_version: expectedAssetVersionRef.current,
        },
      });
      const nextVersion = result.data;
      setBaselineSkills(nextVersion.skill_version_ids);
      setBaselineMcps(nextVersion.mcp_version_ids);
      setDraftSkills(nextVersion.skill_version_ids);
      setDraftMcps(nextVersion.mcp_version_ids);
      expectedAssetVersionRef.current += 1;
      savedVersionIdRef.current = nextVersion.id;
      setEditing(false);
      onVersionCreated(nextVersion.id);
    } catch (error) {
      if (
        error instanceof SharedAssetApiError &&
        error.code === "ASSET_CONFLICT"
      ) {
        setLocalError(
          "Agent 已在其他窗口发生变化。当前选择仍保留，请刷新详情后重新保存。",
        );
      } else {
        setLocalError(adminAssetErrorMessage(error));
      }
    }
  }

  const saveBlockedReason = catalogLoading
    ? "正在加载项目能力目录"
    : catalogError
      ? "项目能力目录加载失败"
      : mcpRuntime.isLoading
        ? "正在验证 MCP 绑定"
        : mcpRuntime.error
          ? "MCP 绑定验证失败"
          : mcpRuntime.blockReason;

  return (
    <section className="space-y-5" aria-labelledby="agent-capability-title">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2
            id="agent-capability-title"
            className="flex items-center gap-2 text-base font-semibold"
          >
            <WrenchIcon aria-hidden className="size-4" />
            工具绑定
          </h2>
          <p className="text-muted-foreground mt-1 text-sm leading-6">
            增删当前项目可用的 Skill 与 MCP。保存后发布新的 Agent
            版本，旧版本保持不变。
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {editing ? (
            <>
              <Button
                type="button"
                variant="outline"
                disabled={update.isPending}
                onClick={discard}
              >
                取消
              </Button>
              <Button
                type="button"
                disabled={
                  !dirty || update.isPending || saveBlockedReason !== null
                }
                title={saveBlockedReason ?? undefined}
                onClick={() => void save()}
              >
                {update.isPending ? (
                  <Loader2Icon aria-hidden className="size-4 animate-spin" />
                ) : null}
                {update.isPending ? "发布中…" : "保存并发布新版本"}
              </Button>
            </>
          ) : canAuthor ? (
            <Button
              type="button"
              disabled={!version || catalogLoading || Boolean(catalogError)}
              onClick={() => {
                setLocalError(null);
                setEditing(true);
              }}
            >
              编辑绑定
            </Button>
          ) : null}
        </div>
      </div>

      <div className="bg-muted/30 flex flex-wrap items-center gap-2 rounded-xl px-4 py-3 text-sm">
        <span className="text-muted-foreground">内置工具组</span>
        <span className="font-medium">
          {version?.tool_groups.length ?? 0} 个
        </span>
        <span className="text-muted-foreground">本次保持不变</span>
      </div>

      {editing ? (
        <div className="relative">
          <SearchIcon
            aria-hidden
            className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
          />
          <Input
            value={query}
            className="pl-9"
            placeholder="搜索 Skill 或 MCP"
            aria-label="搜索 Agent 能力"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      ) : null}

      {catalogLoading ? (
        <p role="status" className="text-muted-foreground text-sm">
          正在加载项目能力目录…
        </p>
      ) : catalogError ? (
        <p role="alert" className="text-destructive text-sm">
          项目能力目录加载失败，请稍后重试。
        </p>
      ) : (
        <div className="grid gap-5 lg:grid-cols-2">
          <DependencySection
            title="Skill"
            emptyLabel="当前项目暂无可绑定的 Skill。"
            options={skillOptions}
            selectedIds={draftSkills}
            editing={editing}
            query={query}
            onToggle={(versionId) =>
              setDraftSkills((current) => toggleId(current, versionId))
            }
          />
          <DependencySection
            title="MCP"
            emptyLabel="当前项目暂无可绑定的 MCP。"
            options={mcpOptions}
            selectedIds={draftMcps}
            editing={editing}
            query={query}
            onToggle={(versionId) =>
              setDraftMcps((current) => toggleId(current, versionId))
            }
          />
        </div>
      )}

      {editing && saveBlockedReason ? (
        <p role="alert" className="text-destructive text-sm">
          {saveBlockedReason}
        </p>
      ) : null}
      {localError ? (
        <p role="alert" className="text-destructive text-sm">
          {localError}
        </p>
      ) : null}
    </section>
  );
}
