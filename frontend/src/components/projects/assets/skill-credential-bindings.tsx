"use client";

import { AlertCircleIcon, KeyRoundIcon } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  SharedAssetApiError,
  skillCredentialBindingsInputSchema,
  useProjectSkillCredentialBindings,
  useUpdateProjectSkillCredentialBindings,
  type SkillCredentialBindingsInput,
  type SkillCredentialBindingsResponse,
  type SkillCredentialRequirement,
  type ProjectAssetItem,
} from "@/core/shared-assets";

import {
  SkillCredentialOptionSelect,
  skillCredentialRequirementOptions,
} from "./skill-credential-option-select";

export type BindingSelections = Record<string, string>;

export function skillCredentialBindingCanUnbind(
  skillActive: boolean,
  requirement: Pick<SkillCredentialRequirement, "optional">,
): boolean {
  return !skillActive || requirement.optional;
}

function configuredSelections(
  data: SkillCredentialBindingsResponse,
): BindingSelections {
  return Object.fromEntries(
    data.requirements.flatMap((requirement) =>
      requirement.configured
        ? [[requirement.name, requirement.credential_version_id]]
        : [],
    ),
  );
}

function sortedBindingEntries(
  selections: BindingSelections,
): Array<[string, string]> {
  return Object.entries(selections)
    .filter(([, credentialVersionId]) => credentialVersionId !== "")
    .sort(([left], [right]) => left.localeCompare(right));
}

function selectionsEqual(
  left: BindingSelections,
  right: BindingSelections,
): boolean {
  return (
    JSON.stringify(sortedBindingEntries(left)) ===
    JSON.stringify(sortedBindingEntries(right))
  );
}

export function skillCredentialSelectionsAfterServerRefresh(
  current: BindingSelections,
  previousOriginal: BindingSelections,
  nextOriginal: BindingSelections,
  nextRequirementNames: readonly string[],
): {
  selections: BindingSelections;
  preservedLocalChanges: boolean;
} {
  const selections: BindingSelections = {};
  let preservedLocalChanges = false;
  for (const name of nextRequirementNames) {
    const currentValue = current[name] ?? "";
    const previousValue = previousOriginal[name] ?? "";
    const nextValue = nextOriginal[name] ?? "";
    const locallyChanged = currentValue !== previousValue;
    const selectedValue = locallyChanged ? currentValue : nextValue;
    if (locallyChanged && currentValue !== nextValue) {
      preservedLocalChanges = true;
    }
    if (selectedValue) selections[name] = selectedValue;
  }
  return { selections, preservedLocalChanges };
}

export function skillCredentialBindingsPayload(
  expectedRevision: number,
  selections: BindingSelections,
): SkillCredentialBindingsInput {
  return skillCredentialBindingsInputSchema.parse({
    expected_revision: expectedRevision,
    bindings: sortedBindingEntries(selections).map(
      ([name, credentialVersionId]) => ({
        name,
        credential_version_id: credentialVersionId,
      }),
    ),
  });
}

function bindingErrorMessage(
  error: unknown,
  action: "load" | "save",
): string | null {
  if (!error) return null;
  if (error instanceof SharedAssetApiError) {
    if (error.status === 409) {
      return "环境变量配置已被其他人修改。请重新加载最新配置后再保存。";
    }
    if (error.status === 403) return "你没有修改环境变量配置的权限。";
    if (error.status === 404) return "当前 Skill 或发布版本已不存在。";
    if (error.code === "ASSET_RESPONSE_INVALID") {
      return "环境变量配置响应无效，已停止显示以保护敏感信息。";
    }
  }
  return action === "load"
    ? "环境变量配置暂时无法加载。"
    : "环境变量配置保存失败，请稍后重试。";
}

export function SkillCredentialBindingEditor({
  data,
  skillActive,
  canManage,
  credentialsHref,
  pending,
  errorMessage,
  onReload,
  onSave,
  onDirtyChange,
}: {
  data: SkillCredentialBindingsResponse;
  skillActive: boolean;
  canManage: boolean;
  credentialsHref: string;
  pending: boolean;
  errorMessage: string | null;
  onReload: () => void;
  onSave: (input: SkillCredentialBindingsInput) => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const originalSelections = useMemo(() => configuredSelections(data), [data]);
  const [selections, setSelections] =
    useState<BindingSelections>(originalSelections);
  const [serverRefreshPreserved, setServerRefreshPreserved] = useState(false);
  const sourceIdentity = `${data.skill_version_id}:${data.revision}`;
  const appliedSourceIdentityRef = useRef(sourceIdentity);
  const baselineSelectionsRef = useRef(originalSelections);
  const selectionsRef = useRef(selections);
  selectionsRef.current = selections;

  useEffect(() => {
    if (appliedSourceIdentityRef.current === sourceIdentity) return;
    appliedSourceIdentityRef.current = sourceIdentity;
    const reconciled = skillCredentialSelectionsAfterServerRefresh(
      selectionsRef.current,
      baselineSelectionsRef.current,
      originalSelections,
      data.requirements.map((requirement) => requirement.name),
    );
    baselineSelectionsRef.current = originalSelections;
    setSelections(reconciled.selections);
    setServerRefreshPreserved(reconciled.preservedLocalChanges);
  }, [data.requirements, originalSelections, sourceIdentity]);
  const dirty = !selectionsEqual(selections, originalSelections);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(
    () => () => {
      onDirtyChange?.(false);
    },
    [onDirtyChange],
  );

  return (
    <section className="border-border/70 space-y-4 rounded-xl border p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <KeyRoundIcon aria-hidden className="size-4" />
            <h3 className="text-sm font-semibold">环境变量</h3>
          </div>
          <p className="text-muted-foreground mt-1 text-xs leading-5">
            仅绑定当前发布版本声明的变量。密钥保存在 Credential
            中，此页面不会输入或回显密钥值。
          </p>
        </div>
        {canManage && !dirty ? (
          <Button asChild type="button" variant="outline" size="sm">
            <Link href={credentialsHref}>管理 Credential</Link>
          </Button>
        ) : null}
      </div>

      {data.requirements.length === 0 ? (
        <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
          当前发布版本没有声明环境变量。
        </p>
      ) : (
        <div className="space-y-3">
          {skillActive &&
          data.requirements.some(
            (requirement) =>
              !requirement.optional && !selections[requirement.name],
          ) ? (
            <div className="border-destructive/30 rounded-lg border p-3 text-sm">
              <p role="alert" className="text-destructive">
                当前活跃 Skill
                存在未绑定的必需环境变量。请先修复；必需绑定不能在活跃状态下解除。
              </p>
            </div>
          ) : null}
          {data.requirements.map((requirement) => {
            const selectedVersionId = selections[requirement.name] ?? "";
            return (
              <SkillCredentialOptionSelect
                key={requirement.name}
                name={requirement.name}
                optional={requirement.optional}
                options={skillCredentialRequirementOptions(
                  requirement,
                  selectedVersionId,
                )}
                value={selectedVersionId}
                disabled={!canManage || pending}
                error={
                  skillActive &&
                  !requirement.optional &&
                  selectedVersionId === ""
                }
                allowEmpty={
                  skillCredentialBindingCanUnbind(skillActive, requirement) ||
                  selectedVersionId === ""
                }
                manageHref={canManage && !dirty ? credentialsHref : undefined}
                onChange={(credentialVersionId) =>
                  setSelections((current) => {
                    const next = { ...current };
                    if (credentialVersionId) {
                      next[requirement.name] = credentialVersionId;
                    } else {
                      delete next[requirement.name];
                    }
                    return next;
                  })
                }
              />
            );
          })}
        </div>
      )}

      {serverRefreshPreserved ? (
        <p role="status" className="text-muted-foreground text-sm">
          服务器上的环境变量配置已更新；你的未保存选择已保留，并已合并未修改项。请核对后保存或撤销。
        </p>
      ) : null}

      {errorMessage ? (
        <div
          role="alert"
          className="text-destructive flex items-start gap-2 text-sm"
        >
          <AlertCircleIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
          <span>{errorMessage}</span>
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto p-0"
            onClick={() => {
              setSelections(originalSelections);
              baselineSelectionsRef.current = originalSelections;
              setServerRefreshPreserved(false);
              onReload();
            }}
          >
            重新加载
          </Button>
        </div>
      ) : null}

      {canManage && data.requirements.length > 0 ? (
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={!dirty || pending}
            onClick={() => {
              setSelections(originalSelections);
              baselineSelectionsRef.current = originalSelections;
              setServerRefreshPreserved(false);
            }}
          >
            撤销修改
          </Button>
          <Button
            type="button"
            disabled={!dirty || pending}
            onClick={() =>
              onSave(skillCredentialBindingsPayload(data.revision, selections))
            }
          >
            {pending ? "保存中…" : "保存配置"}
          </Button>
        </div>
      ) : null}
    </section>
  );
}

export function SkillCredentialBindings({
  accountId,
  projectId,
  skillId,
  currentPublishedVersionId,
  skillStatus,
  canManage,
  credentialsHref,
  focus = false,
  onFocused,
  onDirtyChange,
}: {
  accountId: string;
  projectId: string;
  skillId: string;
  currentPublishedVersionId: string | null;
  skillStatus: ProjectAssetItem["status"];
  canManage: boolean;
  credentialsHref: string;
  focus?: boolean;
  onFocused?: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const sectionRef = useRef<HTMLElement | null>(null);
  const bindings = useProjectSkillCredentialBindings(
    accountId,
    projectId,
    skillId,
    currentPublishedVersionId !== null,
  );
  const update = useUpdateProjectSkillCredentialBindings(
    accountId,
    projectId,
    skillId,
  );

  useEffect(() => {
    if (!focus) return;
    const frame = requestAnimationFrame(() => {
      sectionRef.current?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
      onFocused?.();
    });
    return () => cancelAnimationFrame(frame);
  }, [focus, onFocused]);

  if (currentPublishedVersionId === null) {
    return (
      <section
        ref={sectionRef}
        className="border-border/70 space-y-2 rounded-xl border p-4"
      >
        <div className="flex items-center gap-2">
          <KeyRoundIcon aria-hidden className="size-4" />
          <h3 className="text-sm font-semibold">环境变量</h3>
        </div>
        <p className="text-muted-foreground text-sm">
          发布 Skill 版本后才能配置环境变量。
        </p>
      </section>
    );
  }

  if (bindings.isLoading) {
    return (
      <section
        ref={sectionRef}
        aria-busy="true"
        aria-label="正在加载环境变量配置"
        className="border-border/70 space-y-3 rounded-xl border p-4"
      >
        <Skeleton className="h-5 w-28" />
        <Skeleton className="h-16 w-full" />
      </section>
    );
  }

  if (bindings.error || !bindings.data) {
    return (
      <section
        ref={sectionRef}
        className="border-border/70 space-y-3 rounded-xl border p-4"
      >
        <div className="flex items-center gap-2">
          <KeyRoundIcon aria-hidden className="size-4" />
          <h3 className="text-sm font-semibold">环境变量</h3>
        </div>
        <p role="alert" className="text-destructive text-sm">
          {bindingErrorMessage(bindings.error, "load") ??
            "环境变量配置暂时无法加载。"}
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void bindings.refetch()}
        >
          重试
        </Button>
      </section>
    );
  }

  return (
    <div
      ref={(element) => {
        sectionRef.current = element;
      }}
    >
      <SkillCredentialBindingEditor
        data={bindings.data}
        skillActive={skillStatus === "active"}
        canManage={canManage}
        credentialsHref={credentialsHref}
        pending={update.isPending}
        errorMessage={bindingErrorMessage(update.error, "save")}
        onDirtyChange={onDirtyChange}
        onReload={() => {
          update.reset();
          void bindings.refetch();
        }}
        onSave={(input) => update.mutate(input)}
      />
    </div>
  );
}
