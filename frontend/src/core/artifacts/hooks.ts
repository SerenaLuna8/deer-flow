import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { useThread } from "@/components/workspace/messages/context";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { scopedPrivateWorkQueryKey } from "@/core/private-work/query-keys";
import type { PrivateWorkAccess } from "@/core/private-work/types";

import { loadArtifactContent, loadArtifactContentFromToolCall } from "./loader";

export function useArtifactContent({
  filepath,
  threadId,
  enabled,
  url,
  privateWork: explicitPrivateWork,
}: {
  filepath: string;
  threadId: string;
  enabled?: boolean;
  url?: string;
  privateWork?: PrivateWorkAccess;
}) {
  const privateWork = usePrivateWorkAccess(explicitPrivateWork);
  const isWriteFile = useMemo(() => {
    return filepath.startsWith("write-file:");
  }, [filepath]);
  const { thread, isMock } = useThread();
  const content = useMemo(() => {
    if (isWriteFile) {
      return loadArtifactContentFromToolCall({ url: filepath, thread });
    }
    return null;
  }, [filepath, isWriteFile, thread]);

  const { data, isLoading, error } = useQuery({
    queryKey: scopedPrivateWorkQueryKey(
      privateWork.scope,
      "artifact",
      filepath,
      threadId,
      isMock,
      url,
    ),
    queryFn: ({ signal }) => {
      return loadArtifactContent({ filepath, threadId, isMock, url, signal });
    },
    enabled,
    retry: privateWork.scope ? false : undefined,
    // Cache artifact content for 5 minutes to avoid repeated fetches (especially for .skill ZIP extraction)
    staleTime: 5 * 60 * 1000,
  });
  return {
    content: isWriteFile ? content : data?.content,
    url: isWriteFile ? undefined : data?.url,
    isLoading,
    error,
  };
}
