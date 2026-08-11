"use client";

import { python } from "@codemirror/lang-python";
import CodeMirror from "@uiw/react-codemirror";
import { useMemo, type FocusEvent, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";

import { utf8ByteLength } from "./python-source-controller";

export const PYTHON_WORKFLOW_EDITOR_POLICY = Object.freeze({
  language: "python" as const,
  executesCode: false,
  networkAccess: false,
});

export function WorkflowPythonEditor({
  disabled,
  error,
  maxBytes,
  onBlurCommit,
  onChange,
  onExplicitCommit,
  readOnly,
  value,
}: {
  disabled: boolean;
  error: string | null;
  maxBytes: number;
  onBlurCommit: () => void;
  onChange: (value: string) => void;
  onExplicitCommit: () => void;
  readOnly: boolean;
  value: string;
}) {
  const extensions = useMemo(() => [python()], []);
  const locked = disabled || readOnly;
  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget)) onBlurCommit();
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!locked && event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      event.stopPropagation();
      onExplicitCommit();
    }
  };

  return (
    <div
      aria-label="Python source editor"
      className="border-border space-y-2 rounded-md border p-2"
      data-codemirror="true"
      data-workflow-python-editor="true"
      onBlurCapture={handleBlur}
      onKeyDownCapture={handleKeyDown}
    >
      <div className="flex items-center justify-between gap-3 text-xs">
        <span className="font-medium">Python only</span>
        <span className="text-muted-foreground">
          {utf8ByteLength(value)} / {maxBytes} UTF-8 bytes
        </span>
      </div>
      <CodeMirror
        aria-label="Python source"
        basicSetup
        editable={!locked}
        extensions={extensions}
        height="260px"
        onChange={onChange}
        readOnly={locked}
        value={value}
      />
      <pre aria-hidden="true" className="sr-only">
        {value}
      </pre>
      {error ? (
        <p className="text-destructive text-xs" role="alert">
          {error}
        </p>
      ) : null}
      <div className="flex items-center justify-between gap-3">
        <p className="text-muted-foreground text-xs">
          Cmd/Ctrl+Enter 提交；编辑器内 Undo 只影响本地文本。
        </p>
        <Button
          disabled={locked}
          onClick={onExplicitCommit}
          size="sm"
          type="button"
          variant="outline"
        >
          提交源码
        </Button>
      </div>
    </div>
  );
}
