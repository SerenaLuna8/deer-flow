"use client";

import { useMemo } from "react";

import { useUploadedFiles } from "@/core/uploads";

import { resolveProjectArtifactReferenceURL } from "./files";
import { useProjectPrivateWorkScope } from "./provider";

export function useProjectArtifactReferenceURL(
  threadId: string,
  reference: string,
) {
  const privateWork = useProjectPrivateWorkScope();
  const projectFiles = useUploadedFiles(threadId, privateWork, true);

  return useMemo(
    () =>
      resolveProjectArtifactReferenceURL(
        privateWork,
        threadId,
        reference,
        (projectFiles.data?.files ?? []).flatMap((file) =>
          file.id && file.logical_path
            ? [{ id: file.id, logicalPath: file.logical_path }]
            : [],
        ),
      ),
    [privateWork, projectFiles.data?.files, reference, threadId],
  );
}
