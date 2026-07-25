"use client";

import {
  CheckCircle2Icon,
  CircleDotIcon,
  CircleIcon,
  ListIcon,
  Loader2Icon,
  MessageCircleQuestionMarkIcon,
} from "lucide-react";
import { useId, useMemo, useState, type KeyboardEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useI18n } from "@/core/i18n/hooks";
import {
  createHumanInputOptionResponse,
  createHumanInputTextResponse,
  type HumanInputOption,
  type HumanInputRequest,
  type HumanInputResponse,
} from "@/core/messages/human-input";
import { isIMEComposing } from "@/lib/ime";
import { cn } from "@/lib/utils";

import { MarkdownContent } from "./markdown-content";

export type HumanInputSubmitResult = boolean | void;

export function shouldSubmitHumanInputTextOnKeyDown(
  event: KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>,
  isComposing = false,
) {
  return (
    event.key === "Enter" &&
    !event.shiftKey &&
    !isIMEComposing(event, isComposing)
  );
}

export function HumanInputCard({
  request,
  disabled = false,
  pending = false,
  answeredResponse = null,
  onSubmit,
}: {
  request: HumanInputRequest;
  disabled?: boolean;
  pending?: boolean;
  answeredResponse?: HumanInputResponse | null;
  onSubmit?: (
    response: HumanInputResponse,
  ) => HumanInputSubmitResult | Promise<HumanInputSubmitResult>;
}) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [selectedOptionId, setSelectedOptionId] = useState("");
  const [error, setError] = useState("");
  const [isComposing, setIsComposing] = useState(false);
  const titleId = useId();
  const textInputId = useId();
  const allowText =
    request.input_mode === "free_text" ||
    request.input_mode === "choice_with_other";
  const options = useMemo(() => request.options ?? [], [request.options]);
  const readOnly = !onSubmit;
  const isDisabled =
    disabled || pending || Boolean(answeredResponse) || readOnly;
  const selectedOption = useMemo(
    () => options.find((option) => option.id === selectedOptionId) ?? null,
    [options, selectedOptionId],
  );
  const statusLabel = answeredResponse
    ? t.humanInput.answered
    : pending
      ? t.humanInput.pending
      : readOnly
        ? t.humanInput.readOnly
        : null;

  const submitResponse = async (response: HumanInputResponse) => {
    if (isDisabled || !onSubmit) {
      return;
    }
    setError("");
    const result = await onSubmit(response);
    if (result !== false && response.response_kind === "text") {
      setText("");
    }
  };

  const handleOptionClick = (option: HumanInputOption) => {
    if (isDisabled) {
      return;
    }
    setSelectedOptionId(option.id);
    setText("");
    setError("");
  };

  const handleSubmit = (event: { preventDefault(): void }) => {
    event.preventDefault();
    if (selectedOption) {
      void submitResponse(
        createHumanInputOptionResponse(request, selectedOption),
      );
      return;
    }
    const value = text.trim();
    if (!value) {
      setError(t.humanInput.emptyError);
      return;
    }
    void submitResponse(createHumanInputTextResponse(request, value));
  };

  const handleTextKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (shouldSubmitHumanInputTextOnKeyDown(event, isComposing)) {
      handleSubmit(event);
    }
  };

  if (answeredResponse) {
    return (
      <section
        aria-label={t.humanInput.answered}
        className="border-border/80 bg-muted/35 flex items-center gap-3 rounded-xl border px-4 py-3"
        data-human-input-state="answered"
        data-testid="human-input-card"
      >
        <CheckCircle2Icon
          aria-hidden
          className="text-success size-5 shrink-0"
        />
        <p className="min-w-0 flex-1 text-sm font-medium">
          {t.humanInput.answeredValue(answeredResponse.value)}
        </p>
      </section>
    );
  }

  return (
    <section
      aria-labelledby={titleId}
      className="text-card-foreground overflow-hidden"
      data-human-input-state="open"
      data-testid="human-input-card"
    >
      <div
        className="border-border/80 text-muted-foreground flex items-center gap-2 border-b pb-3 text-sm"
        data-testid="human-input-progress"
      >
        <ListIcon aria-hidden className="size-4" />
        <span>{t.humanInput.attentionCount(1)}</span>
      </div>

      <div className="grid grid-cols-[2.75rem_minmax(0,1fr)] gap-4 py-6 sm:grid-cols-[3.5rem_minmax(0,1fr)] sm:gap-5">
        <div className="bg-muted text-foreground flex size-11 items-center justify-center rounded-xl sm:size-14">
          <MessageCircleQuestionMarkIcon
            aria-hidden
            className="size-5 sm:size-6"
          />
        </div>

        <div className="min-w-0 space-y-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 space-y-2">
              <h2 id={titleId} className="text-xl leading-7 font-semibold">
                {request.title ?? t.toolCalls.needYourHelp}
              </h2>
              {request.context ? (
                <div className="text-muted-foreground text-[15px] leading-6">
                  <MarkdownContent
                    content={request.context}
                    isLoading={false}
                  />
                </div>
              ) : null}
            </div>
            {statusLabel ? (
              <Badge
                className={cn("h-6 rounded-md px-2", pending && "gap-1.5")}
                variant="secondary"
              >
                {pending ? (
                  <Loader2Icon className="size-3 animate-spin" />
                ) : null}
                {statusLabel}
              </Badge>
            ) : null}
          </div>

          <div className="text-foreground text-[15px] leading-7">
            <MarkdownContent content={request.question} isLoading={false} />
          </div>

          <form className="space-y-4" onSubmit={handleSubmit}>
            {options.length > 0 ? (
              <div
                aria-label={request.question}
                className="grid gap-3"
                role="radiogroup"
              >
                {options.map((option) => {
                  const selected = option.id === selectedOptionId;
                  return (
                    <Button
                      key={option.id}
                      aria-checked={selected}
                      className={cn(
                        "min-h-12 w-full justify-start rounded-xl px-4 py-3 text-left text-[15px] leading-6 whitespace-normal shadow-none",
                        selected &&
                          "border-ring bg-selection-subtle hover:bg-selection-subtle",
                      )}
                      disabled={isDisabled}
                      role="radio"
                      type="button"
                      variant="outline"
                      onClick={() => handleOptionClick(option)}
                    >
                      {selected ? (
                        <CircleDotIcon
                          aria-hidden
                          className="text-ring size-5 shrink-0"
                        />
                      ) : (
                        <CircleIcon
                          aria-hidden
                          className="text-muted-foreground/50 size-5 shrink-0"
                        />
                      )}
                      <span className="min-w-0 wrap-break-word whitespace-pre-wrap">
                        {option.label}
                      </span>
                    </Button>
                  );
                })}
              </div>
            ) : null}

            {allowText ? (
              <>
                <label className="sr-only" htmlFor={textInputId}>
                  {t.humanInput.otherLabel}
                </label>
                <Input
                  id={textInputId}
                  aria-invalid={Boolean(error)}
                  aria-describedby={error ? `${textInputId}-error` : undefined}
                  className="h-12 rounded-xl px-4 text-[15px] shadow-none"
                  disabled={isDisabled}
                  placeholder={t.humanInput.otherPlaceholder}
                  type="text"
                  value={text}
                  onChange={(event) => {
                    setText(event.target.value);
                    setSelectedOptionId("");
                    if (error) {
                      setError("");
                    }
                  }}
                  onCompositionEnd={() => setIsComposing(false)}
                  onCompositionStart={() => setIsComposing(true)}
                  onKeyDown={handleTextKeyDown}
                />
              </>
            ) : null}

            <div className="flex min-h-10 flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                {error ? (
                  <p
                    className="text-destructive text-sm"
                    id={`${textInputId}-error`}
                  >
                    {error}
                  </p>
                ) : (
                  <p className="text-muted-foreground text-sm">
                    {t.humanInput.changeBeforeSubmit}
                  </p>
                )}
              </div>
              <Button
                className="bg-selection text-selection-foreground hover:bg-selection/90 min-w-28 rounded-lg"
                disabled={isDisabled}
                type="submit"
              >
                {pending ? (
                  <Loader2Icon className="size-4 animate-spin" />
                ) : null}
                {t.humanInput.submit}
              </Button>
            </div>
          </form>
        </div>
      </div>
    </section>
  );
}
