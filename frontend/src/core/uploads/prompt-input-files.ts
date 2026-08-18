import type { FileUIPart } from "ai";

import type { FileInMessage } from "../messages/utils";

export type ReadyPromptInputFile = Omit<FileInMessage, "file_id" | "status"> & {
  file_id: string;
  status: "uploaded";
};

export type PromptInputFilePart = FileUIPart & {
  // Transient submit-time handle to the original browser File; not serializable.
  file?: File;
  // Stable only for the lifetime of one composer attachment. It lets a retry
  // reuse an already-uploaded opaque file after pre-admission failure.
  clientId?: string;
  // A server-admitted file restored from a failed Run. It intentionally has no
  // browser File or data URL: resubmission reuses this opaque, Thread-scoped
  // file-version id and lets Worker revalidate the authoritative metadata.
  readyFile?: ReadyPromptInputFile;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu;

export function isReadyPromptInputFilePart(
  filePart: PromptInputFilePart,
): filePart is PromptInputFilePart & { readyFile: ReadyPromptInputFile } {
  const readyFile = filePart.readyFile;
  return (
    readyFile !== undefined &&
    UUID_PATTERN.test(readyFile.file_id) &&
    readyFile.filename.length > 0 &&
    Number.isSafeInteger(readyFile.size) &&
    readyFile.size >= 0 &&
    readyFile.status === "uploaded"
  );
}

export function readyPromptInputFileToMessage(
  filePart: PromptInputFilePart,
): ReadyPromptInputFile {
  if (!isReadyPromptInputFilePart(filePart)) {
    throw new TypeError(
      "Prompt attachment does not contain a valid ready uploaded file reference.",
    );
  }
  return { ...filePart.readyFile };
}

export function readyPromptInputFileToPart(
  readyFile: ReadyPromptInputFile,
): PromptInputFilePart {
  const filePart: PromptInputFilePart = {
    type: "file",
    url: "",
    mediaType: "",
    filename: readyFile.filename,
    readyFile: { ...readyFile },
  };
  if (!isReadyPromptInputFilePart(filePart)) {
    throw new TypeError("Invalid ready uploaded file reference.");
  }
  return filePart;
}

export async function promptInputFilePartToFile(
  filePart: PromptInputFilePart,
): Promise<File | null> {
  if (filePart.file instanceof File) {
    const filename =
      typeof filePart.filename === "string" && filePart.filename.length > 0
        ? filePart.filename
        : filePart.file.name;
    const mediaType =
      typeof filePart.mediaType === "string" && filePart.mediaType.length > 0
        ? filePart.mediaType
        : filePart.file.type;

    if (filePart.file.name === filename && filePart.file.type === mediaType) {
      return filePart.file;
    }

    return new File([filePart.file], filename, { type: mediaType });
  }

  if (!filePart.url || !filePart.filename) {
    return null;
  }

  try {
    const response = await fetch(filePart.url);
    if (!response.ok) {
      throw new Error(
        `HTTP ${response.status} while fetching fallback file URL`,
      );
    }
    const blob = await response.blob();

    return new File([blob], filePart.filename, {
      type: filePart.mediaType || blob.type,
    });
  } catch (error) {
    console.warn("promptInputFilePartToFile: fetch fallback failed", {
      error,
      url: filePart.url,
      filename: filePart.filename,
    });
    return null;
  }
}
