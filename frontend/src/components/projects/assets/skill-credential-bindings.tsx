"use client";

import { AlertCircleIcon, KeyRoundIcon, PlusIcon, XIcon } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
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
} from "@/core/shared-assets";

type BindingSelections = Record<string, string>;

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

function credentialOptionLabel(
  requirement: SkillCredentialRequirement,
  credentialVersionId: string,
): string {
  const eligible = requirement.eligible_credentials.find(
    (credential) => credential.credential_version_id === credentialVersionId,
  );
  if (eligible) {
    return `${eligible.display_name} · 版本 ${eligible.version_number}`;
  }
  if (
    requirement.configured &&
    requirement.credential_version_id === credentialVersionId
  ) {
    return `${requirement.credential_display_name} · 版本 ${requirement.credential_version_number}（当前绑定）`;
  }
  return "不可用的 Credential 版本";
}

function requirementOptions(
  requirement: SkillCredentialRequirement,
  selectedVersionId: string,
) {
  const options = [...requirement.eligible_credentials];
  if (
    selectedVersionId !== "" &&
    !options.some(
      (credential) => credential.credential_version_id === selectedVersionId,
    ) &&
    requirement.configured &&
    requirement.credential_version_id === selectedVersionId
  ) {
    options.unshift({
      credential_id: requirement.credential_id,
      credential_version_id: requirement.credential_version_id,
      display_name: requirement.credential_display_name,
      version_number: requirement.credential_version_number,
    });
  }
  return options;
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

function RequirementBadge({
  requirement,
}: {
  requirement: SkillCredentialRequirement;
}) {
  return (
    <Badge variant="secondary">{requirement.optional ? "可选" : "必需"}</Badge>
  );
}

export function SkillCredentialBindingEditor({
  data,
  canManage,
  credentialsHref,
  pending,
  errorMessage,
  onReload,
  onSave,
}: {
  data: SkillCredentialBindingsResponse;
  canManage: boolean;
  credentialsHref: string;
  pending: boolean;
  errorMessage: string | null;
  onReload: () => void;
  onSave: (input: SkillCredentialBindingsInput) => void;
}) {
  const originalSelections = useMemo(() => configuredSelections(data), [data]);
  const [selections, setSelections] =
    useState<BindingSelections>(originalSelections);
  const [addName, setAddName] = useState("");
  const [addCredentialVersionId, setAddCredentialVersionId] = useState("");
  const sourceIdentity = `${data.skill_version_id}:${data.revision}`;
  const appliedSourceIdentityRef = useRef(sourceIdentity);

  useEffect(() => {
    if (appliedSourceIdentityRef.current === sourceIdentity) return;
    appliedSourceIdentityRef.current = sourceIdentity;
    setSelections(originalSelections);
    setAddName("");
    setAddCredentialVersionId("");
  }, [originalSelections, sourceIdentity]);

  const requirementsByName = useMemo(
    () =>
      new Map(
        data.requirements.map((requirement) => [requirement.name, requirement]),
      ),
    [data.requirements],
  );
  const boundRequirements = data.requirements.filter(
    (requirement) => selections[requirement.name],
  );
  const unboundRequirements = data.requirements.filter(
    (requirement) => !selections[requirement.name],
  );
  const selectedAddRequirement = requirementsByName.get(addName);
  const dirty = !selectionsEqual(selections, originalSelections);

  function selectAddRequirement(name: string) {
    setAddName(name);
    const firstCredential =
      requirementsByName.get(name)?.eligible_credentials[0];
    setAddCredentialVersionId(firstCredential?.credential_version_id ?? "");
  }

  function addBinding() {
    if (!addName || !addCredentialVersionId) return;
    setSelections((current) => ({
      ...current,
      [addName]: addCredentialVersionId,
    }));
    setAddName("");
    setAddCredentialVersionId("");
  }

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
        {canManage ? (
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
        <>
          {boundRequirements.length === 0 ? (
            <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
              尚未绑定环境变量。
            </p>
          ) : (
            <div className="space-y-3">
              {boundRequirements.map((requirement) => {
                const selectedVersionId = selections[requirement.name] ?? "";
                return (
                  <div
                    key={requirement.name}
                    className="bg-muted/25 grid gap-3 rounded-lg border p-3 sm:grid-cols-[minmax(0,1fr)_minmax(240px,1fr)_auto]"
                  >
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <code className="text-sm font-medium break-all">
                          {requirement.name}
                        </code>
                        <RequirementBadge requirement={requirement} />
                      </div>
                      <p className="text-muted-foreground mt-1 text-xs">
                        {credentialOptionLabel(requirement, selectedVersionId)}
                      </p>
                    </div>

                    {canManage ? (
                      <label className="space-y-1">
                        <span className="text-muted-foreground text-xs">
                          替换 Credential
                        </span>
                        <select
                          aria-label={`${requirement.name} Credential`}
                          className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
                          value={selectedVersionId}
                          disabled={pending}
                          onChange={(event) =>
                            setSelections((current) => ({
                              ...current,
                              [requirement.name]: event.target.value,
                            }))
                          }
                        >
                          {requirementOptions(
                            requirement,
                            selectedVersionId,
                          ).map((credential) => (
                            <option
                              key={credential.credential_version_id}
                              value={credential.credential_version_id}
                            >
                              {credential.display_name} · 版本{" "}
                              {credential.version_number}
                            </option>
                          ))}
                        </select>
                      </label>
                    ) : null}

                    {canManage ? (
                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        aria-label={`解除 ${requirement.name} 绑定`}
                        disabled={pending}
                        onClick={() =>
                          setSelections((current) => {
                            const next = { ...current };
                            delete next[requirement.name];
                            return next;
                          })
                        }
                      >
                        <XIcon aria-hidden className="size-4" />
                      </Button>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}

          {canManage && unboundRequirements.length > 0 ? (
            <div className="space-y-3 rounded-lg border border-dashed p-3">
              <div className="flex items-center gap-2 text-sm font-medium">
                <PlusIcon aria-hidden className="size-4" />
                添加环境变量
              </div>
              <div className="grid gap-3 sm:grid-cols-[minmax(180px,0.8fr)_minmax(240px,1fr)_auto]">
                <label className="space-y-1">
                  <span className="text-muted-foreground text-xs">
                    Skill 声明
                  </span>
                  <select
                    aria-label="选择 Skill 环境变量"
                    className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
                    value={addName}
                    disabled={pending}
                    onChange={(event) =>
                      selectAddRequirement(event.target.value)
                    }
                  >
                    <option value="">请选择变量</option>
                    {unboundRequirements.map((requirement) => (
                      <option key={requirement.name} value={requirement.name}>
                        {requirement.name}
                        {requirement.optional ? "（可选）" : "（必需）"}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="space-y-1">
                  <span className="text-muted-foreground text-xs">
                    Credential
                  </span>
                  <select
                    aria-label="选择 Credential"
                    className="border-input bg-background h-9 w-full rounded-md border px-3 text-sm"
                    value={addCredentialVersionId}
                    disabled={!selectedAddRequirement || pending}
                    onChange={(event) =>
                      setAddCredentialVersionId(event.target.value)
                    }
                  >
                    <option value="">
                      {selectedAddRequirement?.eligible_credentials.length === 0
                        ? "没有包含同名 env 字段的 Credential"
                        : "请选择 Credential"}
                    </option>
                    {selectedAddRequirement?.eligible_credentials.map(
                      (credential) => (
                        <option
                          key={credential.credential_version_id}
                          value={credential.credential_version_id}
                        >
                          {credential.display_name} · 版本{" "}
                          {credential.version_number}
                        </option>
                      ),
                    )}
                  </select>
                </label>
                <Button
                  type="button"
                  variant="outline"
                  className="self-end"
                  disabled={!addName || !addCredentialVersionId || pending}
                  onClick={addBinding}
                >
                  添加
                </Button>
              </div>
              {selectedAddRequirement?.eligible_credentials.length === 0 ? (
                <p className="text-muted-foreground text-xs">
                  请先在 Credential 页面创建一个包含{" "}
                  <code>{selectedAddRequirement.name}</code> env 字段的项目
                  Credential。
                </p>
              ) : null}
            </div>
          ) : null}
        </>
      )}

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
            onClick={onReload}
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
            onClick={() => setSelections(originalSelections)}
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
  canManage,
  credentialsHref,
}: {
  accountId: string;
  projectId: string;
  skillId: string;
  currentPublishedVersionId: string | null;
  canManage: boolean;
  credentialsHref: string;
}) {
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

  if (currentPublishedVersionId === null) {
    return (
      <section className="border-border/70 space-y-2 rounded-xl border p-4">
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
      <section className="border-border/70 space-y-3 rounded-xl border p-4">
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
    <SkillCredentialBindingEditor
      data={bindings.data}
      canManage={canManage}
      credentialsHref={credentialsHref}
      pending={update.isPending}
      errorMessage={bindingErrorMessage(update.error, "save")}
      onReload={() => {
        update.reset();
        void bindings.refetch();
      }}
      onSave={(input) => update.mutate(input)}
    />
  );
}
