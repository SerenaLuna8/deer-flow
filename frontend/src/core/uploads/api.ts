/**
 * API functions for file uploads
 */

import { z } from "zod";

import { fetch } from "../api/fetcher";
import { projectClientScopeSchema } from "../private-work/types";
import type { ProjectPrivateWorkScope } from "../private-work/types";

import { UploadApiError, UploadLimitValidationError } from "./errors";
import { validateUploadLimits } from "./file-validation";
import { uploadLimitsSchema, type UploadLimits } from "./limits";

export interface UploadedFileInfo {
  id?: string;
  kind?: ProjectFileKind;
  filename: string;
  size: number;
  path: string;
  logical_path?: string;
  virtual_path: string;
  artifact_url: string;
  extension?: string;
  modified?: number;
  markdown_file?: string;
  markdown_path?: string;
  markdown_virtual_path?: string;
  markdown_artifact_url?: string;
}

export type ProjectFileKind = "upload" | "workspace" | "output";

export const USER_DELETABLE_PROJECT_FILE_KINDS = new Set<ProjectFileKind>([
  "upload",
  "workspace",
  "output",
]);

export function canDeleteProjectFile(
  allowed: boolean,
  kind: ProjectFileKind | undefined,
) {
  return (
    allowed && kind !== undefined && USER_DELETABLE_PROJECT_FILE_KINDS.has(kind)
  );
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

export type UploadRequestOptions = Pick<
  ProjectPrivateWorkScope,
  "apiBaseURL" | "scope"
>;

type PrivateUploadedFile = {
  id: string;
  logical_path: string;
  display_name: string;
  kind: ProjectFileKind;
  media_type: string;
  size: number;
  sha256: string;
  status: string;
  created_at: string;
  updated_at: string;
};

function uploadAPIBaseURL(options: UploadRequestOptions): string {
  return options.apiBaseURL;
}

export function supportsUploadLimits(options: UploadRequestOptions): boolean {
  const scope = projectClientScopeSchema.safeParse(options.scope);
  if (!scope.success) return false;
  try {
    const url = new URL(options.apiBaseURL, "http://deerflow.invalid");
    return (
      url.pathname === `/api/projects/${scope.data.projectId}/private-work`
    );
  } catch {
    return false;
  }
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
    kind: file.kind,
    filename: file.display_name,
    size: file.size,
    path: virtualPath,
    logical_path: file.logical_path,
    virtual_path: virtualPath,
    artifact_url: `${apiBaseURL}/threads/${encodeURIComponent(threadId)}/files/${encodeURIComponent(file.id)}`,
    extension,
    modified: Date.parse(file.updated_at),
  };
}

const privateUploadErrorSchema = z
  .object({
    detail: z
      .object({
        code: z.string().regex(/^[A-Z][A-Z0-9_]{0,63}$/u),
        message: z.string().min(1),
        request_id: z.string().min(1),
      })
      .strict(),
  })
  .strict();

async function readUploadError(
  response: Response,
  fallback: string,
): Promise<UploadApiError> {
  const parsed = privateUploadErrorSchema.safeParse(
    await response.json().catch(() => null),
  );
  return new UploadApiError({
    status: response.status,
    code: parsed.success ? parsed.data.detail.code : "UPLOAD_REQUEST_FAILED",
    message: parsed.success ? parsed.data.detail.message : fallback,
    requestId: parsed.success ? parsed.data.detail.request_id : null,
    retryAfter: response.headers.get("Retry-After"),
  });
}

/**
 * Upload files to a thread
 */
export async function uploadFiles(
  threadId: string,
  files: File[],
  options: UploadRequestOptions,
  signal?: AbortSignal,
): Promise<UploadResponse> {
  signal?.throwIfAborted();
  if (files.length === 0) {
    return {
      success: true,
      files: [],
      message: "0 file(s) uploaded",
      skipped_files: [],
    };
  }
  const limits = await getUploadLimits(threadId, options, signal);
  signal?.throwIfAborted();
  const preflight = validateUploadLimits([], files, limits);
  if (preflight.violations.length > 0) {
    throw new UploadLimitValidationError(preflight.violations);
  }
  const uploadedFiles: UploadedFileInfo[] = [];
  const apiBaseURL = uploadAPIBaseURL(options);
  for (const file of files) {
    signal?.throwIfAborted();
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(
      `${apiBaseURL}/threads/${encodeURIComponent(threadId)}/uploads`,
      { method: "POST", body: formData, signal },
    );
    signal?.throwIfAborted();
    if (!response.ok) {
      throw await readUploadError(response, "Upload failed");
    }
    const uploaded = (await response.json()) as PrivateUploadedFile;
    signal?.throwIfAborted();
    uploadedFiles.push(mapPrivateUploadedFile(uploaded, apiBaseURL, threadId));
  }
  signal?.throwIfAborted();
  return {
    success: true,
    files: uploadedFiles,
    message: `${uploadedFiles.length} file(s) uploaded`,
    skipped_files: [],
  };
}

/**
 * Load the upload limits enforced by the gateway for a thread
 */
export async function getUploadLimits(
  threadId: string,
  options: UploadRequestOptions,
  signal?: AbortSignal,
): Promise<UploadLimits> {
  const parsedThreadId = z.string().uuid().parse(threadId);
  if (!supportsUploadLimits(options)) {
    throw new TypeError("Upload limits require an exact project-private scope");
  }
  const response = await fetch(
    `${uploadAPIBaseURL(options)}/threads/${parsedThreadId}/uploads/limits`,
    { signal },
  );
  signal?.throwIfAborted();

  if (!response.ok) {
    throw await readUploadError(response, "Failed to load upload limits");
  }

  const body = await response.json();
  signal?.throwIfAborted();
  return uploadLimitsSchema.parse(body);
}

/**
 * List all uploaded files for a thread
 */
export async function listUploadedFiles(
  threadId: string,
  options: UploadRequestOptions,
  signal?: AbortSignal,
): Promise<ListFilesResponse> {
  signal?.throwIfAborted();
  const apiBaseURL = uploadAPIBaseURL(options);
  const response = await fetch(
    `${apiBaseURL}/threads/${encodeURIComponent(threadId)}/uploads`,
    { signal },
  );
  signal?.throwIfAborted();

  if (!response.ok) {
    throw await readUploadError(response, "Failed to list uploaded files");
  }

  const files = (await response.json()) as PrivateUploadedFile[];
  signal?.throwIfAborted();
  return {
    files: files.map((file) =>
      mapPrivateUploadedFile(file, apiBaseURL, threadId),
    ),
    count: files.length,
  };
}

/**
 * Delete an uploaded file
 */
export async function deleteUploadedFile(
  threadId: string,
  filename: string,
  options: UploadRequestOptions,
  signal?: AbortSignal,
): Promise<{ success: boolean; message: string }> {
  signal?.throwIfAborted();
  const response = await fetch(
    `${uploadAPIBaseURL(options)}/threads/${encodeURIComponent(threadId)}/uploads?file_id=${encodeURIComponent(filename)}`,
    { method: "DELETE", signal },
  );
  signal?.throwIfAborted();

  if (!response.ok) {
    throw await readUploadError(response, "Failed to delete file");
  }

  const result = (await response.json()) as { success: boolean };
  signal?.throwIfAborted();
  return { ...result, message: "File deleted" };
}
