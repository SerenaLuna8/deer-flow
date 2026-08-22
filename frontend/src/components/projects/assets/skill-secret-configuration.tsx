"use client";

import { EyeIcon, EyeOffIcon } from "lucide-react";
import { useEffect, useState } from "react";

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
import { consumeWriteOnlyInput } from "@/core/api/write-only-input";
import {
  useClearProjectSkillSecret,
  useReplaceProjectSkillSecrets,
  useProjectSkillSecrets,
} from "@/core/shared-assets";

export function SkillSecretConfiguration({
  accountId,
  projectId,
  skillId,
  versionId,
  canReplace,
  canClear,
  onDirtyChange,
}: {
  accountId: string;
  projectId: string;
  skillId: string;
  versionId: string;
  canReplace: boolean;
  canClear: boolean;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const query = useProjectSkillSecrets(
    accountId,
    projectId,
    skillId,
    versionId,
  );
  const replace = useReplaceProjectSkillSecrets(accountId, projectId);
  const clear = useClearProjectSkillSecret(accountId, projectId);
  const [values, setValues] = useState<Record<string, string>>({});
  const [visibleNames, setVisibleNames] = useState<Set<string>>(
    () => new Set(),
  );
  const [clearName, setClearName] = useState<string | null>(null);
  const [pending, setPending] = useState<"replace" | "clear" | null>(null);
  const [mutationError, setMutationError] = useState<unknown>(null);
  const dirty = Object.values(values).some((value) => value !== "");

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  async function save() {
    const secrets = Object.fromEntries(
      Object.entries(values).filter(([, value]) => value !== ""),
    );
    if (Object.keys(secrets).length === 0) return;
    setPending("replace");
    setMutationError(null);
    const submittedSecrets = consumeWriteOnlyInput(secrets, () => {
      setValues({});
      setVisibleNames(new Set());
    });
    try {
      await replace.execute({
        skillId,
        versionId,
        input: { secrets: submittedSecrets },
      });
      await query.refetch();
    } catch (error) {
      setMutationError(error);
    } finally {
      setPending(null);
    }
  }

  async function confirmClear() {
    if (!clearName) return;
    setPending("clear");
    setMutationError(null);
    try {
      await clear.mutateAsync({
        skillId,
        versionId,
        secretName: clearName,
        input: { confirmed: true },
      });
      await query.refetch();
      setClearName(null);
    } catch (error) {
      setMutationError(error);
    } finally {
      setPending(null);
    }
  }

  const error = query.error ?? mutationError;

  return (
    <section className="space-y-4" aria-label="Skill 秘密配置">
      <div>
        <h3 className="text-sm font-semibold">运行秘密</h3>
        <p className="text-muted-foreground mt-1 text-xs">
          秘密值仅可写入。编辑时留空表示保留当前值；保存后输入框会立即清空。
        </p>
      </div>

      {query.isLoading ? (
        <p role="status" className="text-muted-foreground text-sm">
          正在读取配置状态…
        </p>
      ) : query.data?.requirements.length ? (
        <div className="space-y-3">
          {query.data.requirements.map((requirement) => (
            <div
              key={requirement.name}
              className="grid gap-3 rounded-xl border p-3 md:grid-cols-[minmax(14rem,0.8fr)_minmax(0,1fr)_auto] md:items-center"
            >
              <div className="min-w-0">
                <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                  <code className="break-all">{requirement.name}</code>
                  <span className="text-muted-foreground text-xs">
                    {requirement.optional ? "可选" : "必需"} ·{" "}
                    {requirement.configured ? "已配置" : "未配置"}
                  </span>
                </span>
              </div>
              <div className="relative min-w-0">
                <Input
                  className="pr-10"
                  type={
                    visibleNames.has(requirement.name) ? "text" : "password"
                  }
                  autoComplete="new-password"
                  value={values[requirement.name] ?? ""}
                  disabled={!canReplace || pending !== null}
                  aria-label={`${requirement.name} 秘密值`}
                  placeholder={
                    requirement.configured ? "留空以保留" : "输入秘密值"
                  }
                  onChange={(event) =>
                    setValues((current) => ({
                      ...current,
                      [requirement.name]: event.target.value,
                    }))
                  }
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  className="absolute inset-y-0 right-0 my-auto"
                  disabled={!canReplace || pending !== null}
                  aria-label={`${visibleNames.has(requirement.name) ? "隐藏" : "显示"} ${requirement.name} 秘密值`}
                  aria-pressed={visibleNames.has(requirement.name)}
                  onClick={() =>
                    setVisibleNames((current) => {
                      const next = new Set(current);
                      if (next.has(requirement.name)) {
                        next.delete(requirement.name);
                      } else {
                        next.add(requirement.name);
                      }
                      return next;
                    })
                  }
                >
                  {visibleNames.has(requirement.name) ? (
                    <EyeOffIcon aria-hidden className="size-4" />
                  ) : (
                    <EyeIcon aria-hidden className="size-4" />
                  )}
                </Button>
              </div>
              <Button
                type="button"
                variant="outline"
                className="shrink-0 justify-self-end"
                disabled={
                  !canClear || !requirement.configured || pending !== null
                }
                onClick={() => setClearName(requirement.name)}
              >
                清除
              </Button>
            </div>
          ))}
          {canReplace ? (
            <Button
              type="button"
              disabled={!dirty || pending !== null}
              onClick={() => void save()}
            >
              {pending === "replace" ? "正在保存…" : "保存非空秘密值"}
            </Button>
          ) : canClear ? (
            <p className="text-muted-foreground text-sm">
              Historical Version
              的定义和值不可替换；仍可清除已配置值以显式撤销。
            </p>
          ) : (
            <p className="text-muted-foreground text-sm">
              当前账户没有管理项目秘密的权限。
            </p>
          )}
        </div>
      ) : (
        <p className="text-muted-foreground text-sm">此版本未声明运行秘密。</p>
      )}

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error instanceof Error ? error.message : "秘密配置操作失败。"}
        </p>
      ) : null}

      <Dialog
        open={clearName !== null}
        onOpenChange={(open) => !open && setClearName(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>清除秘密值？</DialogTitle>
            <DialogDescription>
              清除后，依赖此值的配置会立即变为未就绪；新的 Run 将无法使用它。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setClearName(null)}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={pending !== null}
              onClick={() => void confirmClear()}
            >
              {pending === "clear" ? "正在清除…" : "确认清除"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
