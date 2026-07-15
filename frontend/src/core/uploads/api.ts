/**
 * API functions for file uploads
 */

import { fetch } from "../api/fetcher";
import { getBackendBaseURL } from "../config";
import type { PrivateWorkAccess } from "../private-work/types";

export interface UploadedFileInfo {
  id?: string;
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

export type UploadRequestOptions = Pick<PrivateWorkAccess, "apiBaseURL"> &
  Partial<Pick<PrivateWorkAccess, "scope">>;

type PrivateUploadedFile = {
  id: string;
  logical_path: string;
  display_name: string;
  kind: string;
  media_type: string;
  size: number;
  sha256: string;
  status: string;
  created_at: string;
  updated_at: string;
};

function uploadAPIBaseURL(options?: UploadRequestOptions): string {
  return options?.apiBaseURL ?? `${getBackendBaseURL()}/api`;
}

function isProjectUpload(options?: UploadRequestOptions): boolean {
  return options?.scope != null;
}

export function supportsUploadLimits(options?: UploadRequestOptions): boolean {
  return !isProjectUpload(options);
}

function mapPrivateUploadedFile(
  file: PrivateUploadedFile,
  apiBaseURL: string,
  threadId: string,
): UploadedFileInfo {
  const virtualPath = `/mnt/user-data/${file.logical_path.replace(/^\/+/, "")}`;
  const extension = file.display_name.includes(".")
    ? file.display_name.slice(file.display_name.lastIndexOf(".") + 1)
    : undefined;
  return {
    id: file.id,
    filename: file.display_name,
    size: file.size,
    path: virtualPath,
    virtual_path: virtualPath,
    artifact_url: `${apiBaseURL}/threads/${encodeURIComponent(threadId)}/files/${encodeURIComponent(file.id)}`,
    extension,
    modified: Date.parse(file.updated_at),
  };
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
  if (isProjectUpload(options)) {
    const uploadedFiles: UploadedFileInfo[] = [];
    const apiBaseURL = uploadAPIBaseURL(options);
    for (const file of files) {
      const formData = new FormData();
      formData.append("file", file);
      const response = await fetch(
        `${apiBaseURL}/threads/${encodeURIComponent(threadId)}/uploads`,
        { method: "POST", body: formData },
      );
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, "Upload failed"));
      }
      const uploaded = (await response.json()) as PrivateUploadedFile;
      uploadedFiles.push(
        mapPrivateUploadedFile(uploaded, apiBaseURL, threadId),
      );
    }
    return {
      success: true,
      files: uploadedFiles,
      message: `${uploadedFiles.length} file(s) uploaded`,
      skipped_files: [],
    };
  }

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
  if (isProjectUpload(options)) {
    const apiBaseURL = uploadAPIBaseURL(options);
    const response = await fetch(
      `${apiBaseURL}/threads/${encodeURIComponent(threadId)}/uploads`,
    );
    if (!response.ok) {
      throw new Error(
        await readErrorDetail(response, "Failed to list uploaded files"),
      );
    }
    const files = (await response.json()) as PrivateUploadedFile[];
    return {
      files: files.map((file) =>
        mapPrivateUploadedFile(file, apiBaseURL, threadId),
      ),
      count: files.length,
    };
  }

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
  if (isProjectUpload(options)) {
    const response = await fetch(
      `${uploadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/uploads?file_id=${encodeURIComponent(filename)}`,
      { method: "DELETE" },
    );
    if (!response.ok) {
      throw new Error(await readErrorDetail(response, "Failed to delete file"));
    }
    const result = (await response.json()) as { success: boolean };
    return { ...result, message: "File deleted" };
  }

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
