import { FileIcon, FileTextIcon } from "lucide-react";

import { getFileExtension } from "@/core/utils/files";
import { cn } from "@/lib/utils";

const documentTypes: Record<string, { label: string; color: string }> = {
  pdf: { label: "PDF", color: "text-red-600 dark:text-red-400" },
  docx: { label: "DOC", color: "text-blue-600 dark:text-blue-400" },
  xlsx: { label: "XLS", color: "text-emerald-600 dark:text-emerald-400" },
  csv: { label: "CSV", color: "text-emerald-600 dark:text-emerald-400" },
  md: { label: "MD", color: "text-sky-600 dark:text-sky-400" },
  pptx: { label: "PPT", color: "text-orange-600 dark:text-orange-400" },
  txt: { label: "TXT", color: "text-slate-500 dark:text-slate-400" },
  html: { label: "HTML", color: "text-violet-600 dark:text-violet-400" },
  htm: { label: "HTML", color: "text-violet-600 dark:text-violet-400" },
  epub: { label: "EPUB", color: "text-teal-600 dark:text-teal-400" },
};

export function KnowledgeFileTypeIcon({ fileName }: { fileName: string }) {
  const type = documentTypes[getFileExtension(fileName)];

  return (
    <span
      aria-hidden
      title={type?.label}
      className={cn(
        "relative inline-flex h-9 w-7 shrink-0 items-start justify-center",
        type?.color ?? "text-muted-foreground",
      )}
    >
      {type ? (
        <>
          <FileIcon className="size-6" strokeWidth={1.5} />
          <span className="bg-card absolute bottom-0 left-1/2 -translate-x-1/2 rounded-sm px-0.5 text-[8px] leading-3 font-bold tracking-tight">
            {type.label}
          </span>
        </>
      ) : (
        <FileTextIcon className="mt-1.5 size-6" strokeWidth={1.5} />
      )}
    </span>
  );
}
