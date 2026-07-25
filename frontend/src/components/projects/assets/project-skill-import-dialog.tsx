"use client";

import { FileArchiveIcon, UploadIcon, XIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { adminAssetErrorMessage } from "@/components/admin/assets/admin-asset-view-model";
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
import { SharedAssetApiError } from "@/core/shared-assets";

export const PROJECT_SKILL_ARCHIVE_ACCEPT =
  ".zip,.skill,.tar,.tar.gz,.tgz,application/zip,application/x-tar,application/gzip";

const SUPPORTED_ARCHIVE_SUFFIXES = [
  ".tar.gz",
  ".skill",
  ".tgz",
  ".tar",
  ".zip",
] as const;

type SelectedArchive = Pick<File, "name" | "size">;

export type ProjectSkillArchiveSelection = {
  file: File | null;
  errorMessage: string | null;
  resetInput: boolean;
};

export function isSupportedProjectSkillArchiveName(name: string): boolean {
  const normalized = name.trim().toLocaleLowerCase();
  return SUPPORTED_ARCHIVE_SUFFIXES.some((suffix) =>
    normalized.endsWith(suffix),
  );
}

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  const kilobytes = size / 1024;
  if (kilobytes < 1024) {
    return `${Number.isInteger(kilobytes) ? kilobytes : kilobytes.toFixed(1)} KB`;
  }
  const megabytes = kilobytes / 1024;
  return `${Number.isInteger(megabytes) ? megabytes : megabytes.toFixed(1)} MB`;
}

export function projectSkillImportErrorMessage(error: unknown): string {
  if (error instanceof SharedAssetApiError) {
    if (error.status === 409) {
      return "当前项目已存在同名或同标识的 Skill，请更换压缩包后重试。";
    }
    if (error.status === 413 || error.code === "ASSET_UPLOAD_TOO_LARGE") {
      return "压缩包超过上传或解压限制，请缩小后重试。";
    }
    if (error.status === 422 || error.code === "ASSET_VALIDATION_FAILED") {
      return "压缩包无效或格式不受支持，请确认其中包含有效的 SKILL.md。";
    }
  }
  return adminAssetErrorMessage(error);
}

export function resolveProjectSkillArchiveSelection(
  file: File | null,
): ProjectSkillArchiveSelection {
  if (!file) {
    return { file: null, errorMessage: null, resetInput: true };
  }
  if (!isSupportedProjectSkillArchiveName(file.name)) {
    return {
      file: null,
      errorMessage: "仅支持 .zip、.skill、.tar、.tar.gz 或 .tgz 格式的压缩包。",
      resetInput: true,
    };
  }
  if (file.size === 0) {
    return {
      file: null,
      errorMessage: "压缩包不能为空。",
      resetInput: true,
    };
  }
  return { file, errorMessage: null, resetInput: false };
}

export function ProjectSkillImportForm({
  selectedFile,
  inputResetKey,
  pending,
  errorMessage,
  onFileChange,
  onSelectionChange,
  onSubmit,
  onCancel,
}: {
  selectedFile: SelectedArchive | null;
  inputResetKey: number;
  pending: boolean;
  errorMessage: string | null;
  onFileChange: (file: File | null) => void;
  onSelectionChange: () => void;
  onSubmit: () => void;
  onCancel?: () => void;
}) {
  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (selectedFile && !pending) onSubmit();
      }}
    >
      <label className="grid gap-2 text-sm" htmlFor="project-skill-archive">
        Skill 压缩包
        <Input
          key={inputResetKey}
          id="project-skill-archive"
          type="file"
          accept={PROJECT_SKILL_ARCHIVE_ACCEPT}
          disabled={pending}
          aria-describedby="project-skill-archive-help"
          onChange={(event) => {
            onSelectionChange();
            onFileChange(event.currentTarget.files?.item(0) ?? null);
          }}
        />
      </label>
      <p
        id="project-skill-archive-help"
        className="text-muted-foreground text-xs leading-5"
      >
        支持 .zip、.skill、.tar、.tar.gz 和 .tgz。压缩包中需包含有效的
        SKILL.md，名称与标识将从文件内容读取。
      </p>

      {selectedFile ? (
        <div className="bg-muted/45 flex items-center gap-3 rounded-xl border px-3 py-3">
          <FileArchiveIcon
            aria-hidden
            className="text-muted-foreground size-5 shrink-0"
          />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium">
              {selectedFile.name}
            </span>
            <span className="text-muted-foreground mt-0.5 block text-xs">
              {formatFileSize(selectedFile.size)}
            </span>
          </span>
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            disabled={pending}
            aria-label="移除已选择的压缩包"
            onClick={() => {
              onSelectionChange();
              onFileChange(null);
            }}
          >
            <XIcon aria-hidden className="size-4" />
          </Button>
        </div>
      ) : null}

      {errorMessage ? (
        <p role="alert" className="text-destructive text-sm">
          {errorMessage}
        </p>
      ) : null}

      <DialogFooter>
        {onCancel ? (
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={onCancel}
          >
            取消
          </Button>
        ) : null}
        <Button type="submit" disabled={!selectedFile || pending}>
          <UploadIcon aria-hidden className="size-4" />
          {pending ? "上传并校验中…" : "上传并创建"}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function ProjectSkillImportDialog({
  open,
  pending,
  errorMessage,
  onOpenChange,
  onSelectionChange,
  onSubmit,
}: {
  open: boolean;
  pending: boolean;
  errorMessage: string | null;
  onOpenChange: (open: boolean) => void;
  onSelectionChange: () => void;
  onSubmit: (archive: File) => void;
}) {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [inputResetKey, setInputResetKey] = useState(0);

  useEffect(() => {
    if (open) return;
    setSelectedFile(null);
    setLocalError(null);
  }, [open]);

  function selectFile(file: File | null) {
    const selection = resolveProjectSkillArchiveSelection(file);
    setSelectedFile(selection.file);
    setLocalError(selection.errorMessage);
    if (selection.resetInput) {
      setInputResetKey((current) => current + 1);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && pending) return;
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>上传压缩包创建 Skill</DialogTitle>
          <DialogDescription>
            导入完整目录与文件并直接发布首个版本。新 Skill
            默认停用，可在检查后启用。
          </DialogDescription>
        </DialogHeader>
        <ProjectSkillImportForm
          selectedFile={selectedFile}
          inputResetKey={inputResetKey}
          pending={pending}
          errorMessage={localError ?? errorMessage}
          onFileChange={selectFile}
          onSelectionChange={onSelectionChange}
          onCancel={() => onOpenChange(false)}
          onSubmit={() => {
            if (selectedFile) onSubmit(selectedFile);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}
