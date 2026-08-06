type ProjectDownloadAccess = { apiBaseURL: string };

function projectPrivateWorkBaseURL(access: ProjectDownloadAccess) {
  const value = access.apiBaseURL.replace(/\/$/u, "");
  if (
    !/\/api\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\/private-work$/iu.test(
      value,
    )
  ) {
    throw new Error("Project downloads require a project private-work URL");
  }
  return value;
}

export function projectFileDownloadURL(
  access: ProjectDownloadAccess,
  threadId: string,
  fileId: string,
) {
  return `${projectPrivateWorkBaseURL(access)}/threads/${encodeURIComponent(threadId)}/files/${encodeURIComponent(fileId)}`;
}

export function projectArtifactDownloadURL(
  access: ProjectDownloadAccess,
  threadId: string,
  artifactId: string,
) {
  return `${projectPrivateWorkBaseURL(access)}/artifacts/${encodeURIComponent(artifactId)}?thread_id=${encodeURIComponent(threadId)}`;
}

export type ProjectFileReference = {
  id: string;
  logicalPath: string;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

function logicalPathOfReference(reference: string) {
  return reference
    .replace(/^\/mnt\/(?:data|user-data)\//u, "")
    .replace(/^\/+/, "");
}

export function resolveProjectArtifactReferenceURL(
  access: ProjectDownloadAccess,
  threadId: string,
  reference: string,
  files: readonly ProjectFileReference[],
) {
  if (UUID_PATTERN.test(reference)) {
    return projectArtifactDownloadURL(access, threadId, reference);
  }
  if (reference.startsWith("/Users/") || reference.startsWith("/home/")) {
    return null;
  }
  const logicalPath = logicalPathOfReference(reference);
  const file = files.find((candidate) => candidate.logicalPath === logicalPath);
  return file ? projectFileDownloadURL(access, threadId, file.id) : null;
}
