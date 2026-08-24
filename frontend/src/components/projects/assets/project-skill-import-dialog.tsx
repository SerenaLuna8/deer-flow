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
import {
  SharedAssetApiError,
  type SkillArchiveSecurityRiskConfirmation,
} from "@/core/shared-assets";

export const PROJECT_SKILL_ARCHIVE_ACCEPT =
  ".zip,.skill,.tar,.tar.gz,.tgz,application/zip,application/x-tar,application/gzip";

export const PROJECT_SKILL_IMPORT_DESCRIPTION =
  "导入完整目录与文件并保存为首个候选版本。新 Skill 默认停用，检查后可激活该版本。";

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
    if (error.code === "SKILL_ARCHIVE_SECURITY_BLOCKED") {
      const diagnostics = error.skillArchiveSecurityDiagnostics ?? [];
      if (diagnostics.length === 0) {
        return "Skill 压缩包未通过安全扫描，请检查包内脚本后重试。";
      }
      const riskConfirmation = error.skillArchiveSecurityRiskConfirmation;
      return [
        riskConfirmation
          ? "Skill 压缩包存在以下安全风险："
          : "Skill 压缩包未通过安全扫描，请修改以下位置后重试：",
        ...(riskConfirmation
          ? ["确认后仅保存为受阻候选版本，修复阻断项前不能激活。"]
          : []),
        ...diagnostics.map(({ rule_id: ruleId, file, line }) => {
          const location = file ?? "压缩包";
          return `- ${ruleId}（${line === null ? location : `${location}:${line}`}）`;
        }),
      ].join("\n");
    }
    if (error.status === 422 || error.code === "ASSET_VALIDATION_FAILED") {
      return "压缩包无效或格式不受支持，请确认其中包含有效的 SKILL.md。";
    }
  }
  return adminAssetErrorMessage(error);
}

export function resolveProjectSkillArchiveRiskConfirmation(
  error: unknown,
  pending: boolean,
  submittedConfirmation?: SkillArchiveSecurityRiskConfirmation,
): SkillArchiveSecurityRiskConfirmation | null {
  if (pending) return submittedConfirmation ?? null;
  if (
    error instanceof SharedAssetApiError &&
    error.code === "SKILL_ARCHIVE_SECURITY_BLOCKED"
  ) {
    return error.skillArchiveSecurityRiskConfirmation ?? null;
  }
  return null;
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
  securityRiskConfirmation,
  onFileChange,
  onSelectionChange,
  onSubmit,
  onCancel,
}: {
  selectedFile: SelectedArchive | null;
  inputResetKey: number;
  pending: boolean;
  errorMessage: string | null;
  securityRiskConfirmation: SkillArchiveSecurityRiskConfirmation | null;
  onFileChange: (file: File | null) => void;
  onSelectionChange: () => void;
  onSubmit: (
    securityRiskConfirmation?: SkillArchiveSecurityRiskConfirmation,
  ) => void;
  onCancel?: () => void;
}) {
  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (selectedFile && !pending && !securityRiskConfirmation) onSubmit();
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
        <p
          role="alert"
          className="text-destructive text-sm break-words whitespace-pre-line"
        >
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
        {securityRiskConfirmation ? (
          <Button
            type="button"
            variant="destructive"
            disabled={!selectedFile || pending}
            onClick={() => onSubmit(securityRiskConfirmation)}
          >
            <UploadIcon aria-hidden className="size-4" />
            {pending ? "确认上传中…" : "确认风险仍然上传"}
          </Button>
        ) : (
          <Button type="submit" disabled={!selectedFile || pending}>
            <UploadIcon aria-hidden className="size-4" />
            {pending ? "上传并校验中…" : "上传并创建"}
          </Button>
        )}
      </DialogFooter>
    </form>
  );
}

export function ProjectSkillImportDialog({
  open,
  pending,
  errorMessage,
  securityRiskConfirmation,
  onOpenChange,
  onSelectionChange,
  onSubmit,
}: {
  open: boolean;
  pending: boolean;
  errorMessage: string | null;
  securityRiskConfirmation: SkillArchiveSecurityRiskConfirmation | null;
  onOpenChange: (open: boolean) => void;
  onSelectionChange: () => void;
  onSubmit: (
    archive: File,
    securityRiskConfirmation?: SkillArchiveSecurityRiskConfirmation,
  ) => void;
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
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>上传压缩包创建 Skill</DialogTitle>
          <DialogDescription>
            {PROJECT_SKILL_IMPORT_DESCRIPTION}
          </DialogDescription>
        </DialogHeader>
        <ProjectSkillImportForm
          selectedFile={selectedFile}
          inputResetKey={inputResetKey}
          pending={pending}
          errorMessage={localError ?? errorMessage}
          securityRiskConfirmation={securityRiskConfirmation}
          onFileChange={selectFile}
          onSelectionChange={onSelectionChange}
          onCancel={() => onOpenChange(false)}
          onSubmit={(confirmation) => {
            if (selectedFile) onSubmit(selectedFile, confirmation);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}
