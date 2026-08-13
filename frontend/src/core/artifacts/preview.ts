import { buildWriteFileArtifactURL } from "./utils";

export type ArtifactViewMode = "code" | "preview";

type ArtifactPreviewMessage = {
  type?: string;
  id?: string;
  name?: string | null;
  tool_call_id?: string;
  content?: unknown;
  tool_calls?: Array<{
    id?: string;
    name?: string;
    args?: Record<string, unknown>;
  }>;
};

export type WriteArtifactSelection = {
  key: string;
  url: string;
};

export type WriteArtifactAutoOpenState = {
  threadId: string;
  initialized: boolean;
  runHasBeenLoading: boolean;
  seenKeys: ReadonlySet<string>;
};

export function createWriteArtifactAutoOpenState(
  threadId: string,
): WriteArtifactAutoOpenState {
  return {
    threadId,
    initialized: false,
    runHasBeenLoading: false,
    seenKeys: new Set(),
  };
}

export function advanceWriteArtifactAutoOpenState({
  state,
  threadId,
  selections,
  historyIsLoading,
  runIsLoading,
}: {
  state: WriteArtifactAutoOpenState;
  threadId: string;
  selections: readonly WriteArtifactSelection[];
  historyIsLoading: boolean;
  runIsLoading: boolean;
}): {
  state: WriteArtifactAutoOpenState;
  selection?: WriteArtifactSelection;
} {
  const switchedThread = state.threadId !== threadId;
  const currentState = switchedThread
    ? createWriteArtifactAutoOpenState(threadId)
    : state;
  const seenKeys = new Set(currentState.seenKeys);

  if (historyIsLoading) {
    for (const selection of selections) {
      seenKeys.add(selection.key);
    }
    return {
      state: {
        ...currentState,
        runHasBeenLoading: currentState.runHasBeenLoading || runIsLoading,
        seenKeys,
      },
    };
  }

  if (!currentState.initialized) {
    return {
      state: {
        ...currentState,
        initialized: true,
        runHasBeenLoading: runIsLoading,
        seenKeys: new Set(selections.map((selection) => selection.key)),
      },
    };
  }

  const unseenSelections = selections.filter(
    (selection) => !seenKeys.has(selection.key),
  );
  for (const selection of unseenSelections) {
    seenKeys.add(selection.key);
  }
  const runCanAutoOpen = runIsLoading || currentState.runHasBeenLoading;

  return {
    state: {
      ...currentState,
      runHasBeenLoading: runIsLoading,
      seenKeys,
    },
    selection: runCanAutoOpen ? unseenSelections.at(-1) : undefined,
  };
}

export function extractWriteArtifactSelections(
  messages: ArtifactPreviewMessage[],
): WriteArtifactSelection[] {
  const selections: WriteArtifactSelection[] = [];
  for (const message of messages) {
    if (message.type !== "ai" || !message.id) continue;
    for (const toolCall of message.tool_calls ?? []) {
      if (
        (toolCall.name !== "write_file" && toolCall.name !== "str_replace") ||
        !toolCall.id ||
        typeof toolCall.args?.path !== "string" ||
        !toolCall.args.path
      ) {
        continue;
      }
      selections.push({
        key: `${message.id}/${toolCall.id}`,
        url: buildWriteFileArtifactURL({
          filepath: toolCall.args.path,
          messageId: message.id,
          toolCallId: toolCall.id,
        }),
      });
    }
  }
  return selections;
}

export function isWriteFileArtifact(filepath: string) {
  return filepath.startsWith("write-file:");
}

function hasSuccessfulWriteResult(toolResult: string | undefined) {
  return toolResult?.trim() === "OK";
}

function hasFailedWriteResult(toolResult: string | undefined) {
  return (
    typeof toolResult === "string" && !hasSuccessfulWriteResult(toolResult)
  );
}

function getTextContent(content: unknown) {
  if (typeof content === "string") {
    return content.trim();
  }
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (
          typeof part === "object" &&
          part !== null &&
          "text" in part &&
          typeof part.text === "string"
        ) {
          return part.text;
        }
        return "";
      })
      .join("")
      .trim();
  }
  return undefined;
}

function findToolResult(
  toolCallId: string,
  messages: ArtifactPreviewMessage[],
) {
  for (const message of messages) {
    if (message.type === "tool" && message.tool_call_id === toolCallId) {
      return getTextContent(message.content);
    }
  }
  return undefined;
}

function parseWriteFileArtifact(filepath: string) {
  if (!isWriteFileArtifact(filepath)) {
    return undefined;
  }
  try {
    const url = new URL(filepath);
    return {
      path: decodeURIComponent(url.pathname),
      messageId: url.searchParams.get("message_id") ?? undefined,
      toolCallId: url.searchParams.get("tool_call_id") ?? undefined,
    };
  } catch {
    return undefined;
  }
}

function normalizeArtifactLogicalPath(filepath: string) {
  return filepath
    .replace(/^\/mnt\/(?:data|user-data)\//u, "")
    .replace(/^\/+/, "");
}

export function resolveDurableArtifactSelection(
  selectedArtifact: string | null,
  readyFiles: ReadonlyArray<{ logical_path?: string | null }>,
) {
  if (!selectedArtifact) {
    return undefined;
  }
  const transient = parseWriteFileArtifact(selectedArtifact);
  if (!transient) {
    return undefined;
  }
  const logicalPath = normalizeArtifactLogicalPath(transient.path);
  return readyFiles.find(
    (file) =>
      typeof file.logical_path === "string" &&
      normalizeArtifactLogicalPath(file.logical_path) === logicalPath,
  )?.logical_path;
}

export function mergeDurableArtifactPaths(
  stateArtifacts: readonly string[],
  readyFiles: ReadonlyArray<{ logical_path?: string | null }>,
) {
  const readyByLogicalPath = new Map<string, string>();
  for (const file of readyFiles) {
    if (typeof file.logical_path === "string" && file.logical_path) {
      readyByLogicalPath.set(
        normalizeArtifactLogicalPath(file.logical_path),
        file.logical_path,
      );
    }
  }

  const merged: string[] = [];
  const seen = new Set<string>();
  const add = (artifact: string) => {
    const key = normalizeArtifactLogicalPath(artifact);
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    merged.push(artifact);
  };

  for (const artifact of stateArtifacts) {
    add(
      readyByLogicalPath.get(normalizeArtifactLogicalPath(artifact)) ??
        artifact,
    );
  }
  for (const logicalPath of readyByLogicalPath.values()) {
    add(logicalPath);
  }
  return merged;
}

export function buildWriteFileDraftContent({
  filepath,
  messages,
}: {
  filepath: string;
  messages: ArtifactPreviewMessage[];
}) {
  const target = parseWriteFileArtifact(filepath);
  if (!target) {
    return undefined;
  }

  let draft = "";
  let hasDraft = false;

  for (const message of messages) {
    if (message.type !== "ai") {
      continue;
    }

    for (const toolCall of message.tool_calls ?? []) {
      const args = toolCall.args ?? {};
      if (
        toolCall.name !== "write_file" ||
        args.path !== target.path ||
        typeof args.content !== "string"
      ) {
        continue;
      }

      const toolCallId = toolCall.id;
      const toolResult = toolCallId
        ? findToolResult(toolCallId, messages)
        : undefined;
      const isSelected =
        toolCallId === target.toolCallId &&
        (!target.messageId || message.id === target.messageId);
      if (isSelected && hasFailedWriteResult(toolResult)) {
        return undefined;
      }

      const shouldInclude =
        hasSuccessfulWriteResult(toolResult) ||
        (isSelected && toolResult === undefined);

      if (!shouldInclude) {
        continue;
      }

      if (args.append === true && hasDraft) {
        draft += args.content;
      } else {
        draft = args.content;
      }
      hasDraft = true;

      if (isSelected) {
        return draft;
      }
    }
  }

  return hasDraft ? draft : undefined;
}

export function getArtifactViewState({
  filepath,
  isSupportPreview,
  toolResult,
}: {
  filepath: string;
  isSupportPreview: boolean;
  toolResult?: string;
}): {
  canPreview: boolean;
  initialViewMode: ArtifactViewMode;
} {
  const isWriteArtifact = isWriteFileArtifact(filepath);
  const canPreview =
    isSupportPreview && (!isWriteArtifact || !hasFailedWriteResult(toolResult));
  return {
    canPreview,
    initialViewMode: canPreview ? "preview" : "code",
  };
}

export function appendHtmlPreviewBaseHref(
  content: string,
  url?: string,
  currentHref = globalThis.location?.href ?? "http://localhost/",
) {
  if (!url || /<base\s/i.exec(content)) {
    return content;
  }

  const baseHref = htmlBaseHref(url, currentHref);
  const baseElement = `<base href="${escapeHtmlAttribute(baseHref)}">`;
  if (/<head[^>]*>/i.exec(content)) {
    return content.replace(/<head([^>]*)>/i, `<head$1>${baseElement}`);
  }
  return `${baseElement}${content}`;
}

function htmlBaseHref(url: string, currentHref: string) {
  const baseUrl = new URL(url, currentHref);
  baseUrl.pathname = baseUrl.pathname.replace(/\/[^/]*$/, "/");
  baseUrl.search = "";
  baseUrl.hash = "";
  return baseUrl.toString();
}

function escapeHtmlAttribute(value: string) {
  return value.replaceAll("&", "&amp;").replaceAll('"', "&quot;");
}

export const HTML_PREVIEW_SCROLL_MESSAGE_SOURCE =
  "deerflow-artifact-preview-scroll";

export function createHtmlPreviewScrollKey(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `artifact-scroll:${(hash >>> 0).toString(36)}`;
}

function escapeJavaScriptString(value: string) {
  return JSON.stringify(value)
    .replace(/</g, "\\u003C")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

function htmlScrollRestorationScript(messageKey: string) {
  return `<script data-deerflow-artifact-scroll-restoration>
(() => {
  const source = ${escapeJavaScriptString(HTML_PREVIEW_SCROLL_MESSAGE_SOURCE)};
  const key = ${escapeJavaScriptString(messageKey)};
  const post = (type, payload = {}) => {
    window.parent.postMessage({ source, key, type, ...payload }, "*");
  };
  const save = () => {
    post("save", {
      x: Math.round(window.scrollX || 0),
      y: Math.round(window.scrollY || 0),
    });
  };
  const restore = (x, y) => {
    if (Number.isFinite(x) && Number.isFinite(y)) {
      window.scrollTo(x, y);
    }
  };
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (
      !data ||
      data.source !== source ||
      data.key !== key ||
      data.type !== "restore"
    ) {
      return;
    }
    restore(data.x, data.y);
  });
  window.addEventListener("scroll", save, { passive: true });
  window.addEventListener("pagehide", save);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => post("restore-request"), { once: true });
  } else {
    post("restore-request");
  }
  window.addEventListener("load", () => post("restore-request"), { once: true });
})();
</script>`;
}

export function appendHtmlPreviewScrollRestoration(
  content: string,
  scrollKey = "default",
) {
  if (content.includes("data-deerflow-artifact-scroll-restoration")) {
    return content;
  }
  const script = htmlScrollRestorationScript(
    createHtmlPreviewScrollKey(scrollKey),
  );
  if (/<head(?:\s[^>]*)?>/i.test(content)) {
    return content.replace(
      /<head(?:\s[^>]*)?>/i,
      (headTag) => `${headTag}${script}`,
    );
  }
  if (/<\/body\s*>/i.test(content)) {
    return content.replace(/<\/body\s*>/i, `${script}</body>`);
  }
  return `${content}${script}`;
}
