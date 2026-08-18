"use client";

import {
  CheckCircle2Icon,
  CheckIcon,
  ChevronDownIcon,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/core/i18n/hooks";
import {
  buildHumanInputFormSummary,
  buildInitialHumanInputFormValues,
  createHumanInputFormResponse,
  createHumanInputOptionResponse,
  createHumanInputTextResponse,
  humanInputResponseDisplayValue,
  readHumanInputFormValue,
  type HumanInputField,
  type HumanInputFormValue,
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

function isEmptyFieldValue(value: HumanInputFormValue | undefined) {
  if (value === undefined) {
    return true;
  }
  if (typeof value === "string") {
    return value.trim().length === 0;
  }
  if (Array.isArray(value)) {
    return value.length === 0;
  }
  return value === false;
}

export function findMissingRequiredFields(
  fields: HumanInputField[],
  values: Record<string, HumanInputFormValue>,
) {
  return fields.filter(
    (field) =>
      field.required &&
      isEmptyFieldValue(readHumanInputFormValue(values, field.name)),
  );
}

function FormFieldInput({
  field,
  value,
  disabled,
  selectPlaceholder,
  controlId,
  labelId,
  invalid,
  errorId,
  onChange,
}: {
  field: HumanInputField;
  value: HumanInputFormValue | undefined;
  disabled: boolean;
  selectPlaceholder: string;
  controlId: string;
  labelId: string;
  invalid: boolean;
  errorId: string;
  onChange: (value: HumanInputFormValue) => void;
}) {
  const stringValue = typeof value === "string" ? value : "";
  const ariaProps = {
    "aria-required": field.required || undefined,
    "aria-invalid": invalid || undefined,
    "aria-describedby": invalid ? errorId : undefined,
  };
  const groupErrorAriaProps = {
    "aria-invalid": invalid || undefined,
    "aria-describedby": invalid ? errorId : undefined,
  };

  if (field.type === "textarea") {
    return (
      <Textarea
        id={controlId}
        className="min-h-24 resize-y rounded-xl px-4 py-3 text-[15px] shadow-none"
        disabled={disabled}
        placeholder={field.placeholder}
        value={stringValue}
        onChange={(event) => onChange(event.target.value)}
        {...ariaProps}
      />
    );
  }

  if (field.type === "select") {
    return (
      <Select
        disabled={disabled}
        value={stringValue}
        onValueChange={(next) => onChange(next)}
      >
        <SelectTrigger
          id={controlId}
          className="h-12 w-full rounded-xl px-4 text-[15px] shadow-none"
          aria-labelledby={labelId}
          {...ariaProps}
        >
          <SelectValue placeholder={field.placeholder ?? selectPlaceholder} />
        </SelectTrigger>
        <SelectContent>
          {(field.options ?? []).map((option) => (
            <SelectItem key={option.id} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  if (field.type === "multi_select") {
    const selectedValues = Array.isArray(value) ? value : [];
    return (
      <div
        id={controlId}
        className="flex flex-wrap gap-2"
        role="group"
        aria-labelledby={labelId}
        {...groupErrorAriaProps}
      >
        {(field.options ?? []).map((option) => {
          const selected = selectedValues.includes(option.value);
          return (
            <Button
              key={option.id}
              aria-pressed={selected}
              className={cn(
                "h-9 w-fit rounded-lg px-3 text-left leading-5 whitespace-normal shadow-none",
                selected &&
                  "border-ring bg-selection-subtle hover:bg-selection-subtle",
              )}
              disabled={disabled}
              type="button"
              variant="outline"
              onClick={() => {
                onChange(
                  selected
                    ? selectedValues.filter((entry) => entry !== option.value)
                    : [...selectedValues, option.value],
                );
              }}
            >
              <span
                aria-hidden
                className={cn(
                  "border-border flex size-4 shrink-0 items-center justify-center rounded border",
                  selected &&
                    "border-ring bg-selection text-selection-foreground",
                )}
              >
                {selected ? <CheckIcon className="size-3" /> : null}
              </span>
              {option.label}
            </Button>
          );
        })}
      </div>
    );
  }

  return (
    <Input
      id={controlId}
      className="h-12 rounded-xl px-4 text-[15px] shadow-none"
      disabled={disabled}
      placeholder={field.placeholder}
      type={
        field.type === "number"
          ? "number"
          : field.type === "date"
            ? "date"
            : "text"
      }
      value={stringValue}
      onChange={(event) => onChange(event.target.value)}
      {...ariaProps}
    />
  );
}

export { FormFieldInput as HumanInputFormFieldInput };

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
  const [formValues, setFormValues] = useState<
    Record<string, HumanInputFormValue>
  >(() => buildInitialHumanInputFormValues(request.fields ?? []));
  const [invalidFieldNames, setInvalidFieldNames] = useState<Set<string>>(
    () => new Set(),
  );
  const titleId = useId();
  const textInputId = useId();
  const formFieldIdBase = useId();
  const formErrorId = `${formFieldIdBase}-error`;
  const isForm = request.input_mode === "form";
  const allowText =
    request.input_mode === "free_text" ||
    request.input_mode === "choice_with_other";
  const options = useMemo(() => request.options ?? [], [request.options]);
  const fields = useMemo(() => request.fields ?? [], [request.fields]);
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

  const handleFormValueChange = (name: string, value: HumanInputFormValue) => {
    const remaining = new Set(invalidFieldNames);
    remaining.delete(name);
    setInvalidFieldNames(remaining);
    if (remaining.size === 0) {
      setError("");
    }
    setFormValues((previous) => ({ ...previous, [name]: value }));
  };

  const handleSubmit = (event: { preventDefault(): void }) => {
    event.preventDefault();
    if (isForm) {
      const missing = findMissingRequiredFields(fields, formValues);
      if (missing.length > 0) {
        setInvalidFieldNames(new Set(missing.map((field) => field.name)));
        setError(t.humanInput.requiredError);
        return;
      }
      if (!buildHumanInputFormSummary(request, formValues).trim()) {
        setError(t.humanInput.emptyError);
        return;
      }
      void submitResponse(createHumanInputFormResponse(request, formValues));
      return;
    }
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
    const displayValue = humanInputResponseDisplayValue(
      request,
      answeredResponse,
    );
    return (
      <section
        aria-label={t.humanInput.answered}
        aria-live="polite"
        className="border-border/80 bg-muted/35 overflow-hidden rounded-xl border"
        data-human-input-state="answered"
        data-testid="human-input-card"
      >
        <details className="group">
          <summary className="focus-visible:ring-ring flex cursor-pointer list-none items-center gap-3 px-4 py-3 text-sm font-medium outline-none focus-visible:ring-2 focus-visible:ring-inset [&::-webkit-details-marker]:hidden">
            <CheckCircle2Icon
              aria-hidden
              className="text-success size-5 shrink-0"
            />
            <span className="min-w-0 flex-1 break-words">
              {t.humanInput.answeredValue(displayValue)}
            </span>
            <ChevronDownIcon
              aria-hidden
              className="text-muted-foreground size-4 shrink-0 transition-transform group-open:rotate-180"
            />
          </summary>

          <div
            className="border-border/70 space-y-5 border-t px-4 py-5 sm:px-6"
            data-testid="human-input-answered-details"
          >
            <div className="space-y-2">
              <h2 className="text-base leading-6 font-semibold">
                {request.title ?? t.toolCalls.needYourHelp}
              </h2>
              {request.context ? (
                <div className="text-muted-foreground text-sm leading-6">
                  <MarkdownContent
                    content={request.context}
                    isLoading={false}
                  />
                </div>
              ) : null}
            </div>

            <div className="text-foreground text-[15px] leading-7">
              <MarkdownContent content={request.question} isLoading={false} />
            </div>

            {options.length > 0 ? (
              <div className="space-y-2">
                <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
                  {t.humanInput.availableOptions}
                </p>
                <ul
                  className="grid gap-2"
                  aria-label={t.humanInput.availableOptions}
                >
                  {options.map((option) => {
                    const selected =
                      answeredResponse.response_kind === "option" &&
                      answeredResponse.option_id === option.id;
                    return (
                      <li
                        key={option.id}
                        className={cn(
                          "border-border/70 bg-background/70 flex items-center gap-2 rounded-lg border px-3 py-2 text-sm",
                          selected && "border-success/40 bg-success/5",
                        )}
                        data-human-input-option-selected={selected}
                      >
                        {selected ? (
                          <CheckCircle2Icon
                            aria-hidden
                            className="text-success size-4 shrink-0"
                          />
                        ) : (
                          <CircleIcon
                            aria-hidden
                            className="text-muted-foreground size-4 shrink-0"
                          />
                        )}
                        <span className="min-w-0 flex-1 break-words">
                          {option.label}
                        </span>
                        {selected ? (
                          <span className="text-success text-xs font-medium">
                            {t.humanInput.selected}
                          </span>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              </div>
            ) : null}

            {fields.length > 0 ? (
              <div className="space-y-2">
                <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
                  {t.humanInput.requestedInformation}
                </p>
                <ul
                  className="grid gap-2"
                  aria-label={t.humanInput.requestedInformation}
                >
                  {fields.map((field) => (
                    <li
                      key={field.name}
                      className="border-border/70 bg-background/70 rounded-lg border px-3 py-2 text-sm"
                    >
                      <span className="font-medium">{field.label}</span>
                      {field.required ? (
                        <span className="text-muted-foreground ml-1 text-xs">
                          ({t.humanInput.requiredA11yLabel})
                        </span>
                      ) : null}
                      {(field.options ?? []).length > 0 ? (
                        <p className="text-muted-foreground mt-1 text-xs leading-5">
                          {(field.options ?? [])
                            .map((option) => option.label)
                            .join(", ")}
                        </p>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <div className="border-success/30 bg-success/5 rounded-lg border px-3 py-3">
              <p className="text-success text-xs font-medium tracking-wide uppercase">
                {t.humanInput.yourAnswer}
              </p>
              <p className="mt-1 text-sm leading-6 break-words whitespace-pre-wrap">
                {displayValue}
              </p>
            </div>
          </div>
        </details>
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

          <form className="relative space-y-4" onSubmit={handleSubmit}>
            {isForm
              ? fields.map((field, index) => {
                  const controlId = `${formFieldIdBase}-${index}`;
                  const labelId = `${controlId}-label`;
                  const fieldValue = readHumanInputFormValue(
                    formValues,
                    field.name,
                  );
                  const invalid = invalidFieldNames.has(field.name);

                  if (field.type === "checkbox") {
                    return (
                      <label
                        key={field.name}
                        className="flex w-fit cursor-pointer items-center gap-2 text-[15px] leading-6"
                        htmlFor={controlId}
                      >
                        <input
                          id={controlId}
                          checked={fieldValue === true}
                          className="accent-selection size-4"
                          disabled={isDisabled}
                          type="checkbox"
                          aria-required={field.required || undefined}
                          aria-invalid={invalid || undefined}
                          aria-describedby={invalid ? formErrorId : undefined}
                          onChange={(event) =>
                            handleFormValueChange(
                              field.name,
                              event.target.checked,
                            )
                          }
                        />
                        {field.label}
                        {field.required ? (
                          <>
                            <span className="text-destructive" aria-hidden>
                              *
                            </span>
                            <span className="sr-only">
                              {t.humanInput.requiredA11yLabel}
                            </span>
                          </>
                        ) : null}
                      </label>
                    );
                  }

                  return (
                    <div key={field.name} className="space-y-2">
                      <label
                        className="text-[15px] leading-6 font-medium"
                        htmlFor={controlId}
                        id={labelId}
                      >
                        {field.label}
                        {field.required ? (
                          <>
                            <span
                              className="text-destructive ml-0.5"
                              aria-hidden
                            >
                              *
                            </span>
                            <span className="sr-only">
                              {t.humanInput.requiredA11yLabel}
                            </span>
                          </>
                        ) : null}
                      </label>
                      <FormFieldInput
                        controlId={controlId}
                        disabled={isDisabled}
                        errorId={formErrorId}
                        field={field}
                        invalid={invalid}
                        labelId={labelId}
                        selectPlaceholder={t.humanInput.selectPlaceholder}
                        value={fieldValue}
                        onChange={(value) =>
                          handleFormValueChange(field.name, value)
                        }
                      />
                    </div>
                  );
                })
              : null}

            {!isForm && options.length > 0 ? (
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
              <div className="min-w-0" aria-live="polite">
                {error ? (
                  <p
                    className="text-destructive text-sm"
                    id={isForm ? formErrorId : `${textInputId}-error`}
                    role={isForm ? "alert" : undefined}
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
