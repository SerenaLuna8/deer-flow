import {
  DownloadIcon,
  LoaderIcon,
  PackageIcon,
  Trash2Icon,
} from "lucide-react";
import { useCallback, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardAction,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { urlOfArtifact } from "@/core/artifacts/utils";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import {
  projectArtifactDownloadURL,
  projectFileDownloadURL,
} from "@/core/private-work/files";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { installSkill, SkillRequestError } from "@/core/skills/api";
import { useDeleteUploadedFile, useUploadedFiles } from "@/core/uploads";
import {
  getFileExtensionDisplayName,
  getFileIcon,
  getFileName,
} from "@/core/utils/files";
import { cn } from "@/lib/utils";

import { useArtifacts } from "./context";

export function ArtifactFileList({
  className,
  files,
  threadId,
}: {
  className?: string;
  files: string[];
  threadId: string;
}) {
  const { t } = useI18n();
  const { user } = useAuth();
  const privateWork = usePrivateWorkAccess();
  const projectFiles = useUploadedFiles(
    threadId,
    privateWork,
    privateWork.scope !== null,
  );
  const deleteProjectFile = useDeleteUploadedFile(threadId, privateWork);
  const isAdmin = user?.system_role === "system_admin";
  const { select: selectArtifact, setOpen } = useArtifacts();
  const [installingFile, setInstallingFile] = useState<string | null>(null);

  const downloadURL = useCallback(
    (filepath: string) => {
      if (!privateWork.scope) {
        return urlOfArtifact({ filepath, threadId, download: true });
      }
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

  const handleInstallSkill = useCallback(
    async (e: React.MouseEvent, filepath: string) => {
      e.stopPropagation();
      e.preventDefault();

      if (installingFile) return;

      setInstallingFile(filepath);
      try {
        const result = await installSkill({
          thread_id: threadId,
          path: filepath,
        });
        if (result.success) {
          toast.success(result.message);
        } else {
          toast.error(result.message || "Failed to install skill");
        }
      } catch (error) {
        console.error("Failed to install skill:", error);
        if (error instanceof SkillRequestError && error.isAdminRequired) {
          toast.error(t.settings.skills.installAdminRequired);
        } else {
          toast.error("Failed to install skill");
        }
      } finally {
        setInstallingFile(null);
      }
    },
    [threadId, installingFile, t],
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
    <ul className={cn("flex w-full flex-col gap-4", className)}>
      {files.map((file) => (
        <Card
          key={file}
          className="relative cursor-pointer p-3"
          onClick={() => handleClick(file)}
        >
          <CardHeader className="grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 pr-2 pl-1">
            <CardTitle className="relative min-w-0 pl-8 leading-tight [overflow-wrap:anywhere] break-words">
              <div className="min-w-0">{getFileName(file)}</div>
              <div className="absolute top-2 -left-0.5">
                {getFileIcon(file, "size-6")}
              </div>
            </CardTitle>
            <CardDescription className="min-w-0 pl-8 text-xs">
              {getFileExtensionDisplayName(file)} file
            </CardDescription>
            <CardAction className="row-span-1 self-center">
              {!privateWork.scope && file.endsWith(".skill") && isAdmin && (
                <Button
                  variant="ghost"
                  disabled={installingFile === file}
                  onClick={(e) => handleInstallSkill(e, file)}
                >
                  {installingFile === file ? (
                    <LoaderIcon className="size-4 animate-spin" />
                  ) : (
                    <PackageIcon className="size-4" />
                  )}
                  {t.common.install}
                </Button>
              )}
              {privateWork.scope &&
                projectFiles.data?.files.some(
                  (candidate) =>
                    candidate.id &&
                    candidate.logical_path ===
                      file
                        .replace(/^\/mnt\/(?:data|user-data)\//u, "")
                        .replace(/^\/+/, ""),
                ) && (
                  <Button
                    variant="ghost"
                    disabled={deleteProjectFile.isPending}
                    onClick={(event) => handleDeleteProjectFile(event, file)}
                  >
                    {deleteProjectFile.isPending ? (
                      <LoaderIcon className="size-4 animate-spin" />
                    ) : (
                      <Trash2Icon className="size-4" />
                    )}
                    {t.common.delete}
                  </Button>
                )}
              {downloadURL(file) ? (
                <Button variant="ghost" asChild>
                  <a
                    href={downloadURL(file)!}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <DownloadIcon className="size-4" />
                    {t.common.download}
                  </a>
                </Button>
              ) : (
                <Button variant="ghost" disabled>
                  <DownloadIcon className="size-4" />
                  {t.common.download}
                </Button>
              )}
            </CardAction>
          </CardHeader>
        </Card>
      ))}
    </ul>
  );
}
