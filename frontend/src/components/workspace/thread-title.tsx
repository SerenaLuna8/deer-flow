import type { BaseStream } from "@langchain/langgraph-sdk";
import { useEffect } from "react";

import { useI18n } from "@/core/i18n/hooks";
import type { AgentThreadState } from "@/core/threads";

export function ThreadDocumentTitle({
  thread,
}: {
  thread: BaseStream<AgentThreadState>;
}) {
  const { t } = useI18n();
  useEffect(() => {
    let _title = t.pages.untitled;

    if (thread.values?.title) {
      _title = thread.values.title;
    }
    if (thread.isThreadLoading) {
      document.title = `Loading... - ${t.pages.appName}`;
    } else {
      document.title = `${_title} - ${t.pages.appName}`;
    }
  }, [
    t.pages.untitled,
    t.pages.appName,
    thread.isThreadLoading,
    thread.values,
  ]);

  return null;
}
