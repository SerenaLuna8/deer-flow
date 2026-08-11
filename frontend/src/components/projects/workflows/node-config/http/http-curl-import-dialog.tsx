"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import type { WorkflowHttpAuthoringV1 } from "@/core/project-workflows/catalog";
import type { JsonValue } from "@/core/project-workflows/types";

import {
  applyHttpCurlDialog,
  closeHttpCurlDialog,
  createHttpCurlDialogState,
  previewHttpCurlDialog,
  type HttpCurlDialogError,
} from "./http-node-config-helpers";

const ERROR_COPY: Record<HttpCurlDialogError, string> = {
  unsafe_or_invalid:
    "cURL 内容不符合安全导入合同；请移除秘密、文件、Shell 或危险传输选项。",
  request_limit_exceeded:
    "Normalized request 超过当前公开请求大小上限，未生成可应用预览。",
  write_capability_required:
    "当前成员缺少 workflow.http.write，不能导入写方法。",
  endpoint_not_available:
    "cURL origin 或 method 不在当前 HTTP Catalog authoring authority 中。",
};

const jsonText = (value: JsonValue): string => JSON.stringify(value, null, 2);

export function HttpCurlImportDialog({
  canWrite,
  currentConfig,
  disabled,
  maxRequestBytes,
  httpAuthoring,
  onApply,
  onClose,
}: {
  canWrite: boolean;
  currentConfig: Record<string, JsonValue>;
  disabled: boolean;
  maxRequestBytes: number | null;
  httpAuthoring: WorkflowHttpAuthoringV1 | null;
  onApply: (config: Record<string, JsonValue>) => void;
  onClose: () => void;
}) {
  const [state, setState] = useState(() => createHttpCurlDialogState());

  const clearAndClose = () => {
    setState(closeHttpCurlDialog());
    onClose();
  };

  const apply = () => {
    const result = applyHttpCurlDialog(
      state,
      currentConfig,
      canWrite,
      maxRequestBytes,
      httpAuthoring,
    );
    setState(result.state);
    if (result.config !== null) onApply(result.config);
    onClose();
  };

  return (
    <Dialog
      onOpenChange={(open) => {
        if (!open) clearAndClose();
      }}
      open
    >
      <DialogContent closeLabel="关闭 cURL 导入" className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>导入 cURL</DialogTitle>
          <DialogDescription>
            仅本地解析受限单请求；不会调用 Shell、fetch
            或网络。原文关闭或应用后立即清空。
          </DialogDescription>
        </DialogHeader>
        <Textarea
          aria-label="cURL 原文"
          disabled={disabled}
          onChange={(event) =>
            setState(createHttpCurlDialogState(event.currentTarget.value))
          }
          placeholder="curl https://api.example.com/v1/items"
          spellCheck={false}
          value={state.source}
        />
        {state.error ? (
          <p className="text-destructive text-sm" role="alert">
            {ERROR_COPY[state.error]}
          </p>
        ) : null}
        {state.preview ? (
          <section aria-label="cURL normalized diff" className="space-y-3">
            <h3 className="text-sm font-semibold">Normalized diff</h3>
            {state.preview.changes.length === 0 ? (
              <p className="text-muted-foreground text-sm">没有语义变化。</p>
            ) : (
              <ul className="max-h-80 space-y-3 overflow-auto">
                {state.preview.changes.map((change) => (
                  <li
                    className="border-border grid gap-2 rounded-md border p-3 sm:grid-cols-2"
                    key={change.field}
                  >
                    <p className="col-span-full text-xs font-semibold">
                      {change.field}
                    </p>
                    <pre className="bg-muted overflow-auto rounded p-2 text-xs">
                      {jsonText(change.before)}
                    </pre>
                    <pre className="bg-muted overflow-auto rounded p-2 text-xs">
                      {jsonText(change.after)}
                    </pre>
                  </li>
                ))}
              </ul>
            )}
          </section>
        ) : null}
        <DialogFooter>
          <Button onClick={clearAndClose} type="button" variant="outline">
            取消
          </Button>
          <Button
            disabled={disabled || state.source.trim().length === 0}
            onClick={() =>
              setState((current) =>
                previewHttpCurlDialog(
                  current,
                  currentConfig,
                  canWrite,
                  maxRequestBytes,
                  httpAuthoring,
                ),
              )
            }
            type="button"
            variant="outline"
          >
            解析并预览
          </Button>
          <Button
            disabled={disabled || state.preview === null}
            onClick={apply}
            type="button"
          >
            Apply
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
