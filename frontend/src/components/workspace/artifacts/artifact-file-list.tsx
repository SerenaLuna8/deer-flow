import { DownloadIcon, LoaderIcon, Trash2Icon } from "lucide-react";
import { useCallback } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useI18n } from "@/core/i18n/hooks";
import {
  projectArtifactDownloadURL,
  projectFileDownloadURL,
} from "@/core/private-work/files";
import { useProjectPrivateWorkScope } from "@/core/private-work/provider";
import { useDeleteUploadedFile, useUploadedFiles } from "@/core/uploads";
import { canDeleteProjectFile } from "@/core/uploads/api";
import {
  getFileExtensionDisplayName,
  getFileIcon,
  getFileName,
} from "@/core/utils/files";
import { cn } from "@/lib/utils";

import { Tooltip } from "../tooltip";

import { useArtifacts } from "./context";
export function ArtifactFileList({
  className,
  files,
  surface = "directory",
  threadId,
  canDelete = false,
}: {
  className?: string;
  files: string[];
  surface?: "directory" | "message";
  threadId: string;
  canDelete?: boolean;
}) {
  const { t } = useI18n();
  const privateWork = useProjectPrivateWorkScope();
  const projectFiles = useUploadedFiles(threadId, privateWork, true);
  const deleteProjectFile = useDeleteUploadedFile(threadId, privateWork);
  const { select: selectArtifact, setOpen } = useArtifacts();

  const downloadURL = useCallback(
    (filepath: string) => {
      const normalizedPath = filepath
        .replace(/^\/mnt\/(?:data|user-data)\//u, "")
        .replace(/^\/+/, "");
      const file = projectFiles.data?.files.find(
        (candidate) => candidate.logical_path === normalizedPath,
      );
      if (file?.id) {
        return projectFileDownloadURL(privateWork, threadId, file.id);
      }
      return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu.test(
        filepath,
      )
        ? projectArtifactDownloadURL(privateWork, threadId, filepath)
        : null;
    },
    [privateWork, projectFiles.data?.files, threadId],
  );

  const handleClick = useCallback(
    (filepath: string) => {
      selectArtifact(filepath);
      setOpen(true);
    },
    [selectArtifact, setOpen],
  );

  const handleDeleteProjectFile = useCallback(
    async (event: React.MouseEvent, filepath: string) => {
      event.stopPropagation();
      event.preventDefault();
      const normalizedPath = filepath
        .replace(/^\/mnt\/(?:data|user-data)\//u, "")
        .replace(/^\/+/, "");
      const file = projectFiles.data?.files.find(
        (candidate) => candidate.logical_path === normalizedPath,
      );
      if (!file?.id) return;
      try {
        await deleteProjectFile.mutateAsync(file.id);
        toast.success("File deleted");
      } catch (error) {
        console.error("Failed to delete project file:", error);
        toast.error("Failed to delete file");
      }
    },
    [deleteProjectFile, projectFiles.data?.files],
  );

  return (
    <ul
      className={cn(
        "flex w-full flex-col",
        surface === "message" ? "gap-2" : "gap-4",
        className,
      )}
      data-testid={
        surface === "message" ? "assistant-delivered-files" : undefined
      }
    >
      {files.map((file) => {
        const normalizedPath = file
          .replace(/^\/mnt\/(?:data|user-data)\//u, "")
          .replace(/^\/+/, "");
        const projectFile = projectFiles.data?.files.find(
          (candidate) => candidate.logical_path === normalizedPath,
        );
        const fileDownloadURL = downloadURL(file);
        return (
          <li key={file}>
            <Card
              className={cn(
                "p-2",
                surface === "message" &&
                  "border-border/70 bg-muted/15 shadow-none",
              )}
            >
              <div className="flex min-w-0 items-center gap-2">
                <button
                  type="button"
                  className="hover:bg-muted/60 focus-visible:ring-ring/50 flex min-w-0 flex-1 items-center gap-3 rounded-lg p-2 text-left transition-colors outline-none focus-visible:ring-[3px]"
                  aria-label={`${t.workspaceChanges.openFile}: ${getFileName(file)}`}
                  onClick={() => handleClick(file)}
                >
                  <span className="text-muted-foreground shrink-0">
                    {getFileIcon(
                      file,
                      surface === "message" ? "size-5" : "size-6",
                    )}
                  </span>
                  <span className="min-w-0">
                    <span className="block font-medium [overflow-wrap:anywhere] break-words">
                      {getFileName(file)}
                    </span>
                    <span className="text-muted-foreground block text-xs">
                      {getFileExtensionDisplayName(file)} {t.common.file}
                    </span>
                  </span>
                </button>
                <div className="flex shrink-0 items-center">
                  {surface === "directory" &&
                    projectFile?.id &&
                    canDeleteProjectFile(canDelete, projectFile.kind) && (
                      <Button
                        variant="ghost"
                        disabled={deleteProjectFile.isPending}
                        onClick={(event) =>
                          handleDeleteProjectFile(event, file)
                        }
                      >
                        {deleteProjectFile.isPending ? (
                          <LoaderIcon className="size-4 animate-spin" />
                        ) : (
                          <Trash2Icon className="size-4" />
                        )}
                        {t.common.delete}
                      </Button>
                    )}
                  {fileDownloadURL ? (
                    surface === "message" ? (
                      <Tooltip content={t.common.download}>
                        <Button size="icon-sm" variant="ghost" asChild>
                          <a
                            aria-label={t.common.download}
                            href={fileDownloadURL}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            <DownloadIcon className="size-4" />
                          </a>
                        </Button>
                      </Tooltip>
                    ) : (
                      <Button variant="ghost" asChild>
                        <a
                          href={fileDownloadURL}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <DownloadIcon className="size-4" />
                          {t.common.download}
                        </a>
                      </Button>
                    )
                  ) : (
                    <Button
                      aria-label={
                        surface === "message" ? t.common.download : undefined
                      }
                      size={surface === "message" ? "icon-sm" : "default"}
                      variant="ghost"
                      disabled
                    >
                      <DownloadIcon className="size-4" />
                      {surface === "directory" && t.common.download}
                    </Button>
                  )}
                </div>
              </div>
            </Card>
          </li>
        );
      })}
    </ul>
  );
}
