import type { Message } from "@langchain/langgraph-sdk";
import {
  BookOpenTextIcon,
  BrainIcon,
  ChevronUp,
  CoinsIcon,
  FolderOpenIcon,
  GlobeIcon,
  ListTodoIcon,
  MessageCircleQuestionMarkIcon,
  NotebookPenIcon,
  SearchIcon,
  SquareTerminalIcon,
  WrenchIcon,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import {
  ChainOfThought,
  ChainOfThoughtContent,
  ChainOfThoughtSearchResult,
  ChainOfThoughtSearchResults,
  ChainOfThoughtStep,
} from "@/components/ai-elements/chain-of-thought";
import { CodeBlock } from "@/components/ai-elements/code-block";
import { Button } from "@/components/ui/button";
import { buildWriteFileArtifactURL } from "@/core/artifacts/utils";
import { useI18n } from "@/core/i18n/hooks";
import { indexToolCallData } from "@/core/messages/tool-call-index";
import { formatTokenCount } from "@/core/messages/usage";
import type { TokenDebugStep } from "@/core/messages/usage-model";
import {
  extractReasoningContentFromMessage,
  getReasoningDurationSeconds,
} from "@/core/messages/utils";
import { useRehypeSplitWordsIntoSpans } from "@/core/rehype";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { extractTitleFromMarkdown } from "@/core/utils/markdown";
import { cn } from "@/lib/utils";

import { useArtifacts } from "../artifacts";
import { FlipDisplay } from "../flip-display";
import { Tooltip } from "../tooltip";

import { MarkdownContent } from "./markdown-content";
import {
  ThinkingDisclosure,
  ThinkingDisclosureContent,
} from "./thinking-disclosure";

const REMEMBERED_RESULT_PREFIX = "Remembered for the next organization pass: ";
const MEMORY_DISABLED_RESULT =
  "Project memory is currently disabled, so nothing can be remembered.";

export function MessageGroup({
  className,
  messages,
  isLoading = false,
  renderTaskToolCall,
  showAllSteps = false,
  tokenDebugSteps = [],
  showTokenDebugSummaries = false,
}: {
  className?: string;
  messages: Message[];
  isLoading?: boolean;
  renderTaskToolCall?: (taskId: string, messageId?: string) => React.ReactNode;
  showAllSteps?: boolean;
  tokenDebugSteps?: TokenDebugStep[];
  showTokenDebugSummaries?: boolean;
}) {
  const { t } = useI18n();
  const [showAbove, setShowAbove] = useState(
    isStaticWebsiteOnly() || isLoading || showAllSteps,
  );
  const steps = useMemo(
    () => convertToSteps(messages, renderTaskToolCall !== undefined),
    [messages, renderTaskToolCall],
  );
  const debugStepByMessageId = useMemo(
    () =>
      new Map(
        tokenDebugSteps.map(
          (step) => [step.messageId || step.id, step] as const,
        ),
      ),
    [tokenDebugSteps],
  );
  const toolCallCountByMessageId = useMemo(() => {
    const counts = new Map<string, number>();

    for (const step of steps) {
      if (step.type !== "toolCall" || !step.messageId) {
        continue;
      }

      counts.set(step.messageId, (counts.get(step.messageId) ?? 0) + 1);
    }

    return counts;
  }, [steps]);
  const lastToolCallStep = useMemo(() => {
    const filteredSteps = steps.filter((step) => step.type === "toolCall");
    return filteredSteps[filteredSteps.length - 1];
  }, [steps]);
  const aboveLastToolCallSteps = useMemo(() => {
    if (lastToolCallStep) {
      const index = steps.indexOf(lastToolCallStep);
      return steps.slice(0, index);
    }
    return [];
  }, [lastToolCallStep, steps]);
  const foldableAboveLastToolCallSteps = useMemo(
    () =>
      aboveLastToolCallSteps.filter(
        (step) => step.type === "reasoning" || step.name !== "task",
      ),
    [aboveLastToolCallSteps],
  );
  const afterLastToolCallReasoningSteps = useMemo(() => {
    if (lastToolCallStep) {
      const index = steps.indexOf(lastToolCallStep);
      return steps
        .slice(index + 1)
        .filter((step): step is CoTReasoningStep => step.type === "reasoning");
    }
    return steps.filter(
      (step): step is CoTReasoningStep => step.type === "reasoning",
    );
  }, [lastToolCallStep, steps]);
  const lastReasoningStep = afterLastToolCallReasoningSteps.at(-1);
  const rehypePlugins = useRehypeSplitWordsIntoSpans(isLoading);
  const firstEligibleDebugSummaryStepIndexByMessageId = useMemo(() => {
    const firstIndices = new Map<string, number>();

    if (!showTokenDebugSummaries) {
      return firstIndices;
    }

    for (const [index, step] of steps.entries()) {
      const messageId = step.messageId;
      if (!messageId || firstIndices.has(messageId)) {
        continue;
      }

      const debugStep = debugStepByMessageId.get(messageId);
      if (!debugStep) {
        continue;
      }

      const toolCallCount = toolCallCountByMessageId.get(messageId) ?? 0;
      if (!debugStep.sharedAttribution && toolCallCount > 0) {
        continue;
      }
      if (
        !debugStep.sharedAttribution &&
        toolCallCount === 0 &&
        debugStep.label === t.common.thinking &&
        debugStep.secondaryLabels.length === 0
      ) {
        continue;
      }

      firstIndices.set(messageId, index);
    }

    return firstIndices;
  }, [
    debugStepByMessageId,
    showTokenDebugSummaries,
    steps,
    t.common.thinking,
    toolCallCountByMessageId,
  ]);

  const renderDebugSummary = (
    messageId: string | undefined,
    stepIndex: number,
  ) => {
    if (!showTokenDebugSummaries || !messageId) {
      return null;
    }

    const debugStep = debugStepByMessageId.get(messageId);
    if (!debugStep) {
      return null;
    }
    if (
      firstEligibleDebugSummaryStepIndexByMessageId.get(messageId) !== stepIndex
    ) {
      return null;
    }

    return (
      <ChainOfThoughtStep
        key={`token-debug-${messageId}`}
        icon={CoinsIcon}
        label={
          <DebugStepLabel
            label={debugStep.label}
            token={formatDebugToken(debugStep, t)}
          />
        }
        description={
          debugStep.sharedAttribution
            ? t.tokenUsage.sharedAttribution
            : undefined
        }
      >
        {debugStep.secondaryLabels.length > 0 && (
          <ChainOfThoughtSearchResults>
            {debugStep.secondaryLabels.map((label, index) => (
              <ChainOfThoughtSearchResult
                key={`${debugStep.id}-${index}-${label}`}
              >
                {label}
              </ChainOfThoughtSearchResult>
            ))}
          </ChainOfThoughtSearchResults>
        )}
      </ChainOfThoughtStep>
    );
  };

  const getStepRenderKey = (step: CoTStep, prefix: string) => {
    const sourceId =
      typeof step.id === "string" && step.id.trim().length > 0
        ? step.id
        : steps.indexOf(step);
    return `${prefix}-${sourceId}`;
  };

  const renderToolCall = (step: CoTToolCallStep) => {
    const debugStep =
      showTokenDebugSummaries && step.messageId
        ? debugStepByMessageId.get(step.messageId)
        : undefined;

    return (
      <ToolCall
        key={getStepRenderKey(step, "tool")}
        {...step}
        tokenDebugStep={
          debugStep && !debugStep.sharedAttribution ? debugStep : undefined
        }
      />
    );
  };

  const renderProcessToolCall = (step: CoTToolCallStep) => {
    if (step.name === "task" && step.id && renderTaskToolCall) {
      return (
        <div key={getStepRenderKey(step, "task")} className="w-full">
          {renderTaskToolCall(step.id, step.messageId)}
        </div>
      );
    }
    return renderToolCall(step);
  };

  const renderReasoningRound = (
    step: CoTReasoningStep,
    {
      defaultOpen,
      isStreaming = false,
    }: { defaultOpen?: boolean; isStreaming?: boolean } = {},
  ) => {
    const stepIndex = steps.indexOf(step);
    const debugStep =
      showTokenDebugSummaries && step.messageId
        ? debugStepByMessageId.get(step.messageId)
        : undefined;
    const inlineThinkingToken = shouldInlineThinkingToken({
      debugStep,
      enabled: showTokenDebugSummaries,
      thinkingLabel: t.common.thinking,
      toolCallCount: step.messageId
        ? (toolCallCountByMessageId.get(step.messageId) ?? 0)
        : 0,
      t,
    });
    return (
      <div key={getStepRenderKey(step, "reasoning")} className="w-full">
        {renderDebugSummary(step.messageId, stepIndex)}
        <ThinkingDisclosure
          className="mb-0"
          defaultOpen={defaultOpen}
          duration={step.reasoningDurationSeconds}
          isStreaming={isStreaming}
          statusDetail={inlineThinkingToken}
        >
          <ThinkingDisclosureContent>
            <MarkdownContent
              content={step.reasoning ?? ""}
              isLoading={isStreaming}
              rehypePlugins={rehypePlugins}
            />
          </ThinkingDisclosureContent>
        </ThinkingDisclosure>
      </div>
    );
  };

  useEffect(() => {
    if (isLoading || showAllSteps) {
      setShowAbove(true);
    }
  }, [isLoading, showAllSteps]);

  if (steps.length === 0) {
    return null;
  }

  if (showAllSteps) {
    return (
      <div className={cn("flex w-full flex-col gap-3", className)}>
        {steps.map((step) => {
          if (step.type === "reasoning") {
            return renderReasoningRound(step, { defaultOpen: true });
          }
          const stepIndex = steps.indexOf(step);
          return (
            <div key={getStepRenderKey(step, "tool-round")} className="w-full">
              {renderDebugSummary(step.messageId, stepIndex)}
              {renderProcessToolCall(step)}
            </div>
          );
        })}
      </div>
    );
  }

  if (
    lastReasoningStep &&
    !lastToolCallStep &&
    steps.every((step) => step.type === "reasoning")
  ) {
    return (
      <div className={cn("flex w-full flex-col gap-2", className)}>
        {steps.map((step, index) => {
          if (step.type !== "reasoning") {
            return null;
          }
          const isLatest = index === steps.length - 1;
          return renderReasoningRound(step, {
            defaultOpen: isLoading && !isLatest ? true : undefined,
            isStreaming: isLoading && isLatest,
          });
        })}
      </div>
    );
  }

  return (
    <ChainOfThought
      className={cn("w-full gap-2 rounded-lg border p-0.5", className)}
      open={true}
    >
      {foldableAboveLastToolCallSteps.length > 0 && !showAllSteps && (
        <Button
          key="above"
          className="w-full items-start justify-start text-left"
          variant="ghost"
          onClick={() => setShowAbove(!showAbove)}
        >
          <ChainOfThoughtStep
            label={
              <span className="opacity-60">
                {showAbove
                  ? t.toolCalls.lessSteps
                  : t.toolCalls.moreSteps(
                      foldableAboveLastToolCallSteps.length,
                    )}
              </span>
            }
            icon={
              <ChevronUp
                className={cn(
                  "size-4 opacity-60 transition-transform duration-200",
                  showAbove ? "rotate-180" : "",
                )}
              />
            }
          ></ChainOfThoughtStep>
        </Button>
      )}
      {lastToolCallStep && (
        <ChainOfThoughtContent className="px-4 pb-2">
          {aboveLastToolCallSteps.flatMap((step) => {
            if (
              !showAbove &&
              (step.type === "reasoning" || step.name !== "task")
            ) {
              return [];
            }
            const stepIndex = steps.indexOf(step);
            if (step.type === "reasoning") {
              return renderReasoningRound(step, {
                defaultOpen: isLoading,
              });
            }

            return [
              renderDebugSummary(step.messageId, stepIndex),
              renderProcessToolCall(step),
            ];
          })}
          {renderDebugSummary(
            lastToolCallStep.messageId,
            steps.indexOf(lastToolCallStep),
          )}
          {lastToolCallStep && (
            <FlipDisplay uniqueKey={lastToolCallStep.id ?? ""}>
              {renderProcessToolCall(lastToolCallStep)}
            </FlipDisplay>
          )}
        </ChainOfThoughtContent>
      )}
      {afterLastToolCallReasoningSteps.map((step) =>
        renderReasoningRound(step, {
          defaultOpen:
            isLoading && step !== lastReasoningStep ? true : undefined,
          isStreaming: isLoading && step === lastReasoningStep,
        }),
      )}
    </ChainOfThought>
  );
}

function formatDebugToken(
  debugStep: TokenDebugStep,
  t: ReturnType<typeof useI18n>["t"],
) {
  return debugStep.usage
    ? `${formatTokenCount(debugStep.usage.totalTokens)} ${t.tokenUsage.label}`
    : t.tokenUsage.unavailableShort;
}

function shouldInlineThinkingToken({
  debugStep,
  toolCallCount,
  enabled,
  thinkingLabel,
  t,
}: {
  debugStep?: TokenDebugStep;
  toolCallCount: number;
  enabled: boolean;
  thinkingLabel: string;
  t: ReturnType<typeof useI18n>["t"];
}) {
  if (
    !enabled ||
    !debugStep ||
    debugStep.sharedAttribution ||
    toolCallCount > 0 ||
    debugStep.label !== thinkingLabel
  ) {
    return null;
  }

  return formatDebugToken(debugStep, t);
}

function DebugStepLabel({
  label,
  token,
}: {
  label: React.ReactNode;
  token?: string | null;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0 flex-1">{label}</div>
      {token ? (
        <div className="text-muted-foreground shrink-0 font-mono text-[11px]">
          {token}
        </div>
      ) : null}
    </div>
  );
}

function ToolCall({
  id,
  messageId,
  name,
  args,
  result,
  tokenDebugStep,
}: {
  id?: string;
  messageId?: string;
  name: string;
  args: Record<string, unknown>;
  result?: string | Record<string, unknown>;
  tokenDebugStep?: TokenDebugStep;
}) {
  const { t } = useI18n();
  const { project_slug: projectSlug } = useParams<{ project_slug?: string }>();
  const { enabled: artifactsEnabled, setOpen, select } = useArtifacts();
  const tokenLabel = tokenDebugStep
    ? formatDebugToken(tokenDebugStep, t)
    : null;
  const resolveLabel = (fallback: React.ReactNode) =>
    tokenDebugStep ? (
      <DebugStepLabel label={tokenDebugStep.label} token={tokenLabel} />
    ) : (
      fallback
    );
  const writeFilePath =
    (name === "write_file" || name === "str_replace") &&
    typeof args.path === "string" &&
    args.path
      ? args.path
      : undefined;
  const writeFileArtifactUrl =
    artifactsEnabled && writeFilePath
      ? buildWriteFileArtifactURL({
          filepath: writeFilePath,
          messageId,
          toolCallId: id,
        })
      : null;

  if (name === "web_search") {
    let label: React.ReactNode = t.toolCalls.searchForRelatedInfo;
    if (typeof args.query === "string") {
      label = t.toolCalls.searchOnWebFor(args.query);
    }
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(label)}
        icon={SearchIcon}
      >
        {Array.isArray(result) && (
          <ChainOfThoughtSearchResults>
            {result.map((item) => (
              <ChainOfThoughtSearchResult key={item.url}>
                <a href={item.url} target="_blank" rel="noopener noreferrer">
                  {item.title}
                </a>
              </ChainOfThoughtSearchResult>
            ))}
          </ChainOfThoughtSearchResults>
        )}
      </ChainOfThoughtStep>
    );
  } else if (name === "image_search") {
    let label: React.ReactNode = t.toolCalls.searchForRelatedImages;
    if (typeof args.query === "string") {
      label = t.toolCalls.searchForRelatedImagesFor(args.query);
    }
    const results = (
      result as {
        results: {
          source_url: string;
          thumbnail_url: string;
          image_url: string;
          title: string;
        }[];
      }
    )?.results;
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(label)}
        icon={SearchIcon}
      >
        {Array.isArray(results) && (
          <ChainOfThoughtSearchResults>
            {Array.isArray(results) &&
              results.map((item) => (
                <Tooltip key={item.image_url} content={item.title}>
                  <a
                    className="size-24 overflow-hidden rounded-lg object-cover"
                    href={item.source_url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <div className="bg-accent size-24">
                      <img
                        className="size-full object-cover"
                        src={item.thumbnail_url}
                        alt={item.title}
                        width={100}
                        height={100}
                      />
                    </div>
                  </a>
                </Tooltip>
              ))}
          </ChainOfThoughtSearchResults>
        )}
      </ChainOfThoughtStep>
    );
  } else if (name === "present_files") {
    return (
      <ChainOfThoughtStep
        key={id}
        label={t.toolCalls.presentFiles}
        icon={FolderOpenIcon}
      ></ChainOfThoughtStep>
    );
  } else if (name === "web_fetch") {
    const url = (args as { url: string })?.url;
    let title = url;
    if (typeof result === "string") {
      const potentialTitle = extractTitleFromMarkdown(result);
      if (potentialTitle && potentialTitle.toLowerCase() !== "untitled") {
        title = potentialTitle;
      }
    }
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(t.toolCalls.viewWebPage)}
        icon={GlobeIcon}
      >
        <ChainOfThoughtSearchResult>
          {url && (
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              className="cursor-pointer"
            >
              {title}
            </a>
          )}
        </ChainOfThoughtSearchResult>
      </ChainOfThoughtStep>
    );
  } else if (name === "ls") {
    let description: string | undefined = (args as { description: string })
      ?.description;
    if (!description) {
      description = t.toolCalls.listFolder;
    }
    const path: string | undefined = (args as { path: string })?.path;
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(description)}
        icon={FolderOpenIcon}
      >
        {path && (
          <ChainOfThoughtSearchResult className="cursor-pointer">
            {path}
          </ChainOfThoughtSearchResult>
        )}
      </ChainOfThoughtStep>
    );
  } else if (name === "read_file") {
    let description: string | undefined = (args as { description: string })
      ?.description;
    if (!description) {
      description = t.toolCalls.readFile;
    }
    const { path } = args as { path: string; content: string };
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(description)}
        icon={BookOpenTextIcon}
      >
        {path && (
          <ChainOfThoughtSearchResult className="cursor-pointer">
            {path}
          </ChainOfThoughtSearchResult>
        )}
      </ChainOfThoughtStep>
    );
  } else if (name === "write_file" || name === "str_replace") {
    let description: string | undefined = (args as { description: string })
      ?.description;
    if (!description) {
      description = t.toolCalls.writeFile;
    }
    return (
      <ChainOfThoughtStep
        key={id}
        className={cn(writeFileArtifactUrl && "cursor-pointer")}
        label={resolveLabel(description)}
        icon={NotebookPenIcon}
        onClick={
          writeFileArtifactUrl
            ? () => {
                select(writeFileArtifactUrl);
                setOpen(true);
              }
            : undefined
        }
      >
        {writeFilePath && (
          <ChainOfThoughtSearchResult className="cursor-pointer">
            {writeFilePath}
          </ChainOfThoughtSearchResult>
        )}
      </ChainOfThoughtStep>
    );
  } else if (name === "bash") {
    const description: string | undefined = (args as { description: string })
      ?.description;
    if (!description) {
      return (
        <ChainOfThoughtStep
          key={id}
          label={resolveLabel(t.toolCalls.executeCommand)}
          icon={SquareTerminalIcon}
        />
      );
    }
    const command: string | undefined = (args as { command: string })?.command;
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(description)}
        icon={SquareTerminalIcon}
      >
        {command && (
          <CodeBlock
            className="mx-0 cursor-pointer border-none px-0"
            showLineNumbers={false}
            language="bash"
            code={command}
          />
        )}
      </ChainOfThoughtStep>
    );
  } else if (name === "ask_clarification") {
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(t.toolCalls.needYourHelp)}
        icon={MessageCircleQuestionMarkIcon}
      ></ChainOfThoughtStep>
    );
  } else if (name === "write_todos") {
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(t.toolCalls.writeTodos)}
        icon={ListTodoIcon}
      ></ChainOfThoughtStep>
    );
  } else if (name === "remember") {
    // These are fixed outcomes from the backend remember tool. A pending
    // proposal has no result yet, so retain the action label while streaming.
    const rememberedLine =
      typeof result === "string" && result.startsWith(REMEMBERED_RESULT_PREFIX)
        ? result.slice(REMEMBERED_RESULT_PREFIX.length)
        : null;
    const memoryDisabled = result === MEMORY_DISABLED_RESULT;
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(
          memoryDisabled
            ? t.toolCalls.memoryDisabledNotSaved
            : t.toolCalls.rememberMemory,
        )}
        icon={BrainIcon}
      >
        {rememberedLine && (
          <ChainOfThoughtSearchResult data-testid="remember-chip">
            {projectSlug ? (
              <Link
                href={`/projects/${encodeURIComponent(projectSlug)}/memory#memory-pending`}
                className="cursor-pointer"
              >
                {t.toolCalls.remembered} {rememberedLine}
              </Link>
            ) : (
              <>
                {t.toolCalls.remembered} {rememberedLine}
              </>
            )}
          </ChainOfThoughtSearchResult>
        )}
      </ChainOfThoughtStep>
    );
  } else {
    const description: string | undefined = (args as { description: string })
      ?.description;
    return (
      <ChainOfThoughtStep
        key={id}
        label={resolveLabel(description ?? t.toolCalls.useTool(name))}
        icon={WrenchIcon}
      ></ChainOfThoughtStep>
    );
  }
}

interface GenericCoTStep<T extends string = string> {
  id?: string;
  messageId?: string;
  type: T;
}

interface CoTReasoningStep extends GenericCoTStep<"reasoning"> {
  reasoning: string | null;
  reasoningDurationSeconds?: number;
}

interface CoTToolCallStep extends GenericCoTStep<"toolCall"> {
  name: string;
  args: Record<string, unknown>;
  result?: string;
}

type CoTStep = CoTReasoningStep | CoTToolCallStep;

function convertToSteps(
  messages: Message[],
  includeTaskToolCalls = false,
): CoTStep[] {
  const steps: CoTStep[] = [];
  const { toolCallResults } = indexToolCallData(messages);
  for (const message of messages) {
    if (message.type === "ai") {
      const reasoning = extractReasoningContentFromMessage(message);
      if (reasoning) {
        const step: CoTReasoningStep = {
          id: message.id,
          messageId: message.id,
          type: "reasoning",
          reasoning,
          reasoningDurationSeconds: getReasoningDurationSeconds(message),
        };
        steps.push(step);
      }
      for (const tool_call of message.tool_calls ?? []) {
        if (
          tool_call.name === "ask_clarification" ||
          (tool_call.name === "task" && !includeTaskToolCalls)
        ) {
          continue;
        }
        const step: CoTToolCallStep = {
          id: tool_call.id,
          messageId: message.id,
          type: "toolCall",
          name: tool_call.name,
          args: tool_call.args,
        };
        const toolCallId = tool_call.id;
        if (toolCallId) {
          const toolCallResult = toolCallResults.get(toolCallId);
          if (toolCallResult) {
            try {
              const json = JSON.parse(toolCallResult);
              step.result = json;
            } catch {
              step.result = toolCallResult;
            }
          }
        }
        steps.push(step);
      }
    }
  }
  return steps;
}
