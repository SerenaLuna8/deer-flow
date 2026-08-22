"use client";

import { useMemo, useState } from "react";

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
  useClearProjectMcpSecret,
  useReplaceProjectMcpSecret,
  useProjectMcpSecrets,
  type McpSecretPayload,
} from "@/core/shared-assets";

type FieldValues = Record<string, Record<string, string>>;

export function McpSecretConfiguration({
  accountId,
  projectId,
  assetId,
  versionId,
  canManage,
}: {
  accountId: string;
  projectId: string;
  assetId: string;
  versionId: string;
  canManage: boolean;
}) {
  const query = useProjectMcpSecrets(accountId, projectId, assetId, versionId);
  const replace = useReplaceProjectMcpSecret(accountId, projectId);
  const clear = useClearProjectMcpSecret(accountId, projectId);
  const [values, setValues] = useState<Record<string, FieldValues>>({});
  const [clearName, setClearName] = useState<string | null>(null);
  const [pending, setPending] = useState<"replace" | "clear" | null>(null);
  const [mutationError, setMutationError] = useState<unknown>(null);
  const slots = useMemo(() => query.data?.slots ?? [], [query.data?.slots]);
  const error = query.error ?? mutationError;

  const completeSlots = useMemo(
    () =>
      new Set(
        slots.flatMap((slot) => {
          const slotValues = values[slot.name] ?? {};
          const fields = Object.entries(slot.payload_schema).flatMap(
            ([group, names]) => names.map((name) => [group, name] as const),
          );
          if (fields.length === 0) return [];
          return fields.every(
            ([group, name]) => (slotValues[group]?.[name] ?? "") !== "",
          )
            ? [slot.name]
            : [];
        }),
      ),
    [slots, values],
  );

  async function saveSlot(slotName: string) {
    const payload = values[slotName] as McpSecretPayload | undefined;
    if (!payload || !completeSlots.has(slotName)) return;
    setPending("replace");
    setMutationError(null);
    const submittedPayload = consumeWriteOnlyInput(payload, () =>
      setValues((current) => ({ ...current, [slotName]: {} })),
    );
    try {
      await replace.execute({
        assetId,
        versionId,
        slotName,
        input: { payload: submittedPayload },
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
        assetId,
        versionId,
        slotName: clearName,
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

  return (
    <section className="space-y-4" aria-label="MCP 秘密配置">
      <div>
        <h3 className="text-sm font-semibold">调用秘密</h3>
        <p className="text-muted-foreground mt-1 text-xs">
          每个槽位整体替换。留空表示保留；API 永不返回已保存的秘密值。
        </p>
      </div>
      {query.isLoading ? (
        <p role="status" className="text-muted-foreground text-sm">
          正在读取配置状态…
        </p>
      ) : slots.length === 0 ? (
        <p className="text-muted-foreground text-sm">此版本没有秘密槽位。</p>
      ) : (
        <div className="space-y-4">
          {slots.map((slot) => (
            <div key={slot.name} className="space-y-3 rounded-xl border p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <p className="font-medium">
                    <code>{slot.name}</code>
                  </p>
                  <p className="text-muted-foreground text-xs">
                    {slot.required ? "必需" : "可选"} ·{" "}
                    {slot.configured ? "已配置" : "未配置"}
                  </p>
                </div>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={!canManage || !slot.configured || pending !== null}
                  onClick={() => setClearName(slot.name)}
                >
                  清除
                </Button>
              </div>
              {Object.entries(slot.payload_schema).flatMap(([group, names]) =>
                names.map((name) => (
                  <label
                    key={`${group}:${name}`}
                    className="grid gap-2 text-sm"
                  >
                    <span>
                      <code>
                        {group}.{name}
                      </code>
                    </span>
                    <Input
                      type="password"
                      autoComplete="new-password"
                      disabled={!canManage || pending !== null}
                      value={values[slot.name]?.[group]?.[name] ?? ""}
                      placeholder={
                        slot.configured ? "留空以保留" : "输入秘密值"
                      }
                      aria-label={`${slot.name} ${group} ${name} 秘密值`}
                      onChange={(event) =>
                        setValues((current) => ({
                          ...current,
                          [slot.name]: {
                            ...(current[slot.name] ?? {}),
                            [group]: {
                              ...(current[slot.name]?.[group] ?? {}),
                              [name]: event.target.value,
                            },
                          },
                        }))
                      }
                    />
                  </label>
                )),
              )}
              {canManage ? (
                <Button
                  type="button"
                  size="sm"
                  disabled={!completeSlots.has(slot.name) || pending !== null}
                  onClick={() => void saveSlot(slot.name)}
                >
                  {pending === "replace" ? "正在保存…" : "替换此槽位"}
                </Button>
              ) : null}
            </div>
          ))}
        </div>
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
            <DialogTitle>清除此 MCP 秘密槽位？</DialogTitle>
            <DialogDescription>
              清除后配置会重新计算就绪状态，新的 Run 将读取清除后的状态。
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
