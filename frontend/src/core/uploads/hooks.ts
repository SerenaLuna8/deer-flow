/**
 * React hooks for file uploads
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { usePrivateWorkAccess } from "../private-work/provider";
import { scopedPrivateWorkQueryKey } from "../private-work/query-keys";
import type { PrivateWorkAccess } from "../private-work/types";

import {
  deleteUploadedFile,
  getUploadLimits,
  listUploadedFiles,
  supportsUploadLimits,
  uploadFiles,
  type UploadedFileInfo,
  type UploadResponse,
} from "./api";

/**
 * Hook to load the gateway-enforced upload limits.
 * Callers intentionally degrade to server-side validation if this request fails.
 */
export function useUploadLimits(
  threadId: string,
  explicitPrivateWork?: PrivateWorkAccess,
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery({
    queryKey: scopedPrivateWorkQueryKey(
      privateWork.scope,
      "uploads",
      "limits",
      threadId,
    ),
    queryFn: () => getUploadLimits(threadId, privateWork),
    enabled: !!threadId && supportsUploadLimits(privateWork),
    retry: false,
    staleTime: 60_000,
  });
}

/**
 * Hook to upload files
 */
export function useUploadFiles(
  threadId: string,
  explicitPrivateWork?: PrivateWorkAccess,
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();

  return useMutation<UploadResponse, Error, File[]>({
    mutationKey: scopedPrivateWorkQueryKey(
      privateWork.scope,
      "uploads",
      "create",
      threadId,
    ),
    mutationFn: (files: File[]) => uploadFiles(threadId, files, privateWork),
    onSuccess: () => {
      // Invalidate the uploaded files list
      void queryClient.invalidateQueries({
        queryKey: scopedPrivateWorkQueryKey(
          privateWork.scope,
          "uploads",
          "list",
          threadId,
        ),
      });
    },
  });
}

/**
 * Hook to list uploaded files
 */
export function useUploadedFiles(
  threadId: string,
  explicitPrivateWork?: PrivateWorkAccess,
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  return useQuery({
    queryKey: scopedPrivateWorkQueryKey(
      privateWork.scope,
      "uploads",
      "list",
      threadId,
    ),
    queryFn: () => listUploadedFiles(threadId, privateWork),
    enabled: !!threadId,
  });
}

/**
 * Hook to delete an uploaded file
 */
export function useDeleteUploadedFile(
  threadId: string,
  explicitPrivateWork?: PrivateWorkAccess,
) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const queryClient = useQueryClient();

  return useMutation({
    mutationKey: scopedPrivateWorkQueryKey(
      privateWork.scope,
      "uploads",
      "delete",
      threadId,
    ),
    mutationFn: (filename: string) =>
      deleteUploadedFile(threadId, filename, privateWork),
    onSuccess: () => {
      // Invalidate the uploaded files list
      void queryClient.invalidateQueries({
        queryKey: scopedPrivateWorkQueryKey(
          privateWork.scope,
          "uploads",
          "list",
          threadId,
        ),
      });
    },
  });
}

/**
 * Hook to handle file uploads in submit flow
 * Returns a function that uploads files and returns their info
 */
export function useUploadFilesOnSubmit(
  threadId: string,
  explicitPrivateWork?: PrivateWorkAccess,
) {
  const uploadMutation = useUploadFiles(threadId, explicitPrivateWork);

  return useCallback(
    async (files: File[]): Promise<UploadedFileInfo[]> => {
      if (files.length === 0) {
        return [];
      }

      const result = await uploadMutation.mutateAsync(files);
      return result.files;
    },
    [uploadMutation],
  );
}
