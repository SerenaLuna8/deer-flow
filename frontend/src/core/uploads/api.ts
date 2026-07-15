/**
 * API functions for file uploads
 */

import { fetch } from "../api/fetcher";
import { getBackendBaseURL } from "../config";
import type { PrivateWorkAccess } from "../private-work/types";

export interface UploadedFileInfo {
  filename: string;
  size: number;
  path: string;
  virtual_path: string;
  artifact_url: string;
  extension?: string;
  modified?: number;
  markdown_file?: string;
  markdown_path?: string;
  markdown_virtual_path?: string;
  markdown_artifact_url?: string;
}

export interface UploadResponse {
  success: boolean;
  files: UploadedFileInfo[];
  message: string;
  skipped_files: string[];
}

export interface ListFilesResponse {
  files: UploadedFileInfo[];
  count: number;
}

export interface UploadLimits {
  max_files: number;
  max_file_size: number;
  max_total_size: number;
}

export type UploadRequestOptions = Pick<PrivateWorkAccess, "apiBaseURL">;

function uploadAPIBaseURL(options?: UploadRequestOptions): string {
  return options?.apiBaseURL ?? `${getBackendBaseURL()}/api`;
}

async function readErrorDetail(
  response: Response,
  fallback: string,
): Promise<string> {
  const error = await response.json().catch(() => ({ detail: fallback }));
  return error.detail ?? fallback;
}

/**
 * Upload files to a thread
 */
export async function uploadFiles(
  threadId: string,
  files: File[],
  options?: UploadRequestOptions,
): Promise<UploadResponse> {
  const formData = new FormData();

  files.forEach((file) => {
    formData.append("files", file);
  });

  const response = await fetch(
    `${uploadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/uploads`,
    {
      method: "POST",
      body: formData,
    },
  );

  if (!response.ok) {
    throw new Error(await readErrorDetail(response, "Upload failed"));
  }

  return response.json();
}

/**
 * Load the upload limits enforced by the gateway for a thread
 */
export async function getUploadLimits(
  threadId: string,
  options?: UploadRequestOptions,
): Promise<UploadLimits> {
  const response = await fetch(
    `${uploadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/uploads/limits`,
  );

  if (!response.ok) {
    throw new Error(
      await readErrorDetail(response, "Failed to load upload limits"),
    );
  }

  return response.json();
}

/**
 * List all uploaded files for a thread
 */
export async function listUploadedFiles(
  threadId: string,
  options?: UploadRequestOptions,
): Promise<ListFilesResponse> {
  const response = await fetch(
    `${uploadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/uploads/list`,
  );

  if (!response.ok) {
    throw new Error(
      await readErrorDetail(response, "Failed to list uploaded files"),
    );
  }

  return response.json();
}

/**
 * Delete an uploaded file
 */
export async function deleteUploadedFile(
  threadId: string,
  filename: string,
  options?: UploadRequestOptions,
): Promise<{ success: boolean; message: string }> {
  const response = await fetch(
    `${uploadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/uploads/${encodeURIComponent(filename)}`,
    {
      method: "DELETE",
    },
  );

  if (!response.ok) {
    throw new Error(await readErrorDetail(response, "Failed to delete file"));
  }

  return response.json();
}
