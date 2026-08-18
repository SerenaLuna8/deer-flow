import type { Message } from "@langchain/langgraph-sdk";

export type HumanInputMode =
  | "free_text"
  | "single_choice"
  | "choice_with_other"
  | "form";

export type HumanInputOption = {
  id: string;
  label: string;
  value: string;
};

export type HumanInputFieldType =
  | "text"
  | "textarea"
  | "number"
  | "select"
  | "multi_select"
  | "checkbox"
  | "date";

export type HumanInputField = {
  name: string;
  label: string;
  type: HumanInputFieldType;
  required: boolean;
  placeholder?: string;
  options?: HumanInputOption[];
};

export type HumanInputFormValue = string | number | boolean | string[];

export type HumanInputRequest = {
  version: 1 | 2;
  kind: "human_input_request";
  source: "ask_clarification" | string;
  request_id: string;
  tool_call_id?: string;
  clarification_type?: string;
  title?: string;
  question: string;
  context?: string | null;
  input_mode: HumanInputMode;
  options?: HumanInputOption[];
  fields?: HumanInputField[];
};

export type HumanInputResponse =
  | {
      version: 1;
      kind: "human_input_response";
      source: string;
      request_id: string;
      response_kind: "option";
      option_id: string;
      value: string;
    }
  | {
      version: 1;
      kind: "human_input_response";
      source: string;
      request_id: string;
      response_kind: "text";
      value: string;
      form_values?: Record<string, HumanInputFormValue>;
    };

export type HumanInputThreadState = {
  answeredResponses: Map<string, HumanInputResponse>;
  latestOpenRequestId: string | null;
};

export function shouldClearPendingHumanInputOnThreadError({
  currentError,
  pendingRequestCount,
  previousError,
}: {
  currentError: unknown;
  pendingRequestCount: number;
  previousError: unknown;
}) {
  return (
    pendingRequestCount > 0 &&
    currentError != null &&
    !Object.is(currentError, previousError)
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isHumanInputMode(value: unknown): value is HumanInputMode {
  return (
    value === "free_text" ||
    value === "single_choice" ||
    value === "choice_with_other" ||
    value === "form"
  );
}

const RESERVED_FIELD_NAMES = new Set([
  "__proto__",
  "constructor",
  "prototype",
  "toString",
  "toLocaleString",
  "valueOf",
  "hasOwnProperty",
  "isPrototypeOf",
  "propertyIsEnumerable",
  "__defineGetter__",
  "__defineSetter__",
  "__lookupGetter__",
  "__lookupSetter__",
]);

export function readHumanInputFormValue(
  values: Record<string, HumanInputFormValue>,
  name: string,
): HumanInputFormValue | undefined {
  return Object.prototype.hasOwnProperty.call(values, name)
    ? values[name]
    : undefined;
}

export function buildInitialHumanInputFormValues(
  fields: HumanInputField[],
): Record<string, HumanInputFormValue> {
  const values: Record<string, HumanInputFormValue> = {};
  for (const field of fields) {
    if (field.type === "checkbox") {
      values[field.name] = false;
    }
  }
  return values;
}

function isHumanInputFieldType(value: unknown): value is HumanInputFieldType {
  return (
    value === "text" ||
    value === "textarea" ||
    value === "number" ||
    value === "select" ||
    value === "multi_select" ||
    value === "checkbox" ||
    value === "date"
  );
}

function readOptionalString(value: unknown) {
  return typeof value === "string" ? value : undefined;
}

function parseOptions(value: unknown): HumanInputOption[] | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (!Array.isArray(value)) {
    return undefined;
  }

  const options: HumanInputOption[] = [];
  const seenIds = new Set<string>();
  const seenValues = new Set<string>();
  for (const option of value) {
    if (!isRecord(option)) {
      return undefined;
    }
    const id = option.id;
    const label = option.label;
    const optionValue = option.value;
    if (
      !isNonEmptyString(id) ||
      !isNonEmptyString(label) ||
      !isNonEmptyString(optionValue) ||
      seenIds.has(id) ||
      seenValues.has(optionValue)
    ) {
      return undefined;
    }
    seenIds.add(id);
    seenValues.add(optionValue);
    options.push({ id, label, value: optionValue });
  }
  return options;
}

function parseFields(value: unknown): HumanInputField[] | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (!Array.isArray(value)) {
    return undefined;
  }

  const fields: HumanInputField[] = [];
  const seenNames = new Set<string>();
  for (const field of value) {
    if (!isRecord(field)) {
      return undefined;
    }
    const name = field.name;
    if (typeof name === "string" && seenNames.has(name)) {
      return undefined;
    }
    if (typeof name === "string") {
      seenNames.add(name);
    }
    const label = field.label;
    const type = field.type;
    const required = field.required;
    if (
      !isNonEmptyString(name) ||
      RESERVED_FIELD_NAMES.has(name) ||
      !isNonEmptyString(label) ||
      !isHumanInputFieldType(type) ||
      (required !== undefined && typeof required !== "boolean")
    ) {
      return undefined;
    }
    const options = parseOptions(field.options);
    if (field.options !== undefined && options === undefined) {
      return undefined;
    }
    if (
      (type === "select" || type === "multi_select") &&
      (!options || options.length === 0)
    ) {
      return undefined;
    }
    fields.push({
      name,
      label,
      type,
      required: required === true,
      ...(readOptionalString(field.placeholder)
        ? { placeholder: readOptionalString(field.placeholder) }
        : {}),
      ...(options ? { options } : {}),
    });
  }
  return fields;
}

export function parseHumanInputRequest(
  value: unknown,
): HumanInputRequest | null {
  if (!isRecord(value)) {
    return null;
  }
  if (
    (value.version !== 1 && value.version !== 2) ||
    value.kind !== "human_input_request" ||
    !isNonEmptyString(value.source) ||
    !isNonEmptyString(value.request_id) ||
    !isNonEmptyString(value.question) ||
    !isHumanInputMode(value.input_mode)
  ) {
    return null;
  }
  if (value.version === 1 && value.fields !== undefined) {
    return null;
  }

  const fields = parseFields(value.fields);
  if (value.fields !== undefined && fields === undefined) {
    return null;
  }
  if (value.input_mode === "form" && (!fields || fields.length === 0)) {
    return null;
  }
  if ((value.input_mode === "form") !== (value.version === 2)) {
    return null;
  }

  const options = parseOptions(value.options);
  if (value.options !== undefined && options === undefined) {
    return null;
  }
  if (
    (value.input_mode === "single_choice" ||
      value.input_mode === "choice_with_other") &&
    (!options || options.length === 0)
  ) {
    return null;
  }

  const context = value.context;
  if (
    context !== undefined &&
    context !== null &&
    typeof context !== "string"
  ) {
    return null;
  }

  return {
    version: value.version,
    kind: "human_input_request",
    source: value.source,
    request_id: value.request_id,
    ...(readOptionalString(value.tool_call_id)
      ? { tool_call_id: readOptionalString(value.tool_call_id) }
      : {}),
    ...(readOptionalString(value.clarification_type)
      ? { clarification_type: readOptionalString(value.clarification_type) }
      : {}),
    ...(readOptionalString(value.title)
      ? { title: readOptionalString(value.title) }
      : {}),
    question: value.question,
    ...(context !== undefined ? { context } : {}),
    input_mode: value.input_mode,
    ...(options ? { options } : {}),
    ...(fields ? { fields } : {}),
  };
}

export function parseHumanInputResponse(
  value: unknown,
): HumanInputResponse | null {
  if (!isRecord(value)) {
    return null;
  }
  if (
    value.version !== 1 ||
    value.kind !== "human_input_response" ||
    !isNonEmptyString(value.source) ||
    !isNonEmptyString(value.request_id) ||
    !isNonEmptyString(value.value)
  ) {
    return null;
  }

  if (value.response_kind === "option") {
    if (!isNonEmptyString(value.option_id)) {
      return null;
    }
    return {
      version: 1,
      kind: "human_input_response",
      source: value.source,
      request_id: value.request_id,
      response_kind: "option",
      option_id: value.option_id,
      value: value.value,
    };
  }

  if (value.response_kind === "text") {
    const formValues = parseHumanInputFormValues(value.form_values);
    if (value.form_values !== undefined && formValues === undefined) {
      return null;
    }
    return {
      version: 1,
      kind: "human_input_response",
      source: value.source,
      request_id: value.request_id,
      response_kind: "text",
      value: value.value,
      ...(formValues ? { form_values: formValues } : {}),
    };
  }

  return null;
}

function parseHumanInputFormValues(
  value: unknown,
): Record<string, HumanInputFormValue> | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const entries = Object.entries(value);
  const parsed: Record<string, HumanInputFormValue> = {};
  for (const [name, fieldValue] of entries) {
    if (!isNonEmptyString(name) || RESERVED_FIELD_NAMES.has(name)) {
      return undefined;
    }
    if (
      typeof fieldValue === "string" ||
      typeof fieldValue === "boolean" ||
      (typeof fieldValue === "number" && Number.isFinite(fieldValue))
    ) {
      parsed[name] = fieldValue;
      continue;
    }
    if (
      Array.isArray(fieldValue) &&
      fieldValue.length > 0 &&
      fieldValue.every(isNonEmptyString)
    ) {
      parsed[name] = [...fieldValue];
      continue;
    }
    return undefined;
  }
  return parsed;
}

export function extractHumanInputRequest(
  message: Message,
): HumanInputRequest | null {
  if (message.type !== "tool") {
    return null;
  }
  const artifact = Reflect.get(message, "artifact");
  if (!isRecord(artifact)) {
    return null;
  }
  return parseHumanInputRequest(artifact.human_input);
}

export function extractHumanInputResponse(
  message: Message,
): HumanInputResponse | null {
  if (message.type !== "human") {
    return null;
  }
  const additionalKwargs = message.additional_kwargs;
  if (!isRecord(additionalKwargs)) {
    return null;
  }
  return parseHumanInputResponse(additionalKwargs.human_input_response);
}

function extractPlainMessageText(message: Message): string {
  const content: unknown = message.content;
  if (typeof content === "string") {
    return content.trim();
  }
  if (Array.isArray(content)) {
    return content
      .map((part) =>
        isRecord(part) && part.type === "text" && typeof part.text === "string"
          ? part.text
          : "",
      )
      .join("")
      .trim();
  }
  return "";
}

function isAskClarificationRequestMessage(
  message: Message,
  request: HumanInputRequest,
) {
  if (
    message.type !== "tool" ||
    message.name !== "ask_clarification" ||
    request.source !== "ask_clarification"
  ) {
    return false;
  }
  return (
    request.tool_call_id === undefined ||
    request.tool_call_id === message.tool_call_id
  );
}

function isResponseValidForRequest(
  request: HumanInputRequest,
  response: HumanInputResponse,
) {
  if (
    response.source !== request.source ||
    response.request_id !== request.request_id
  ) {
    return false;
  }
  if (response.response_kind === "option") {
    if (
      request.input_mode !== "single_choice" &&
      request.input_mode !== "choice_with_other"
    ) {
      return false;
    }
    return Boolean(
      request.options?.some(
        (option) =>
          option.id === response.option_id && option.value === response.value,
      ),
    );
  }
  if (response.form_values !== undefined) {
    if (request.input_mode !== "form") {
      return false;
    }
    const fields = new Map(
      (request.fields ?? []).map((field) => [field.name, field]),
    );
    if (
      Object.keys(response.form_values).some((name) => !fields.has(name)) ||
      [...fields.values()].some(
        (field) =>
          field.required &&
          !Object.prototype.hasOwnProperty.call(
            response.form_values,
            field.name,
          ),
      )
    ) {
      return false;
    }
  }
  return (
    request.input_mode === "free_text" ||
    request.input_mode === "choice_with_other" ||
    request.input_mode === "form"
  );
}

/**
 * Finds control replies written before Gateway preserved `hide_from_ui`.
 *
 * Structured metadata alone is not trusted. A legacy reply is hidden only
 * when it is the first, in-order answer to the latest visible
 * `ask_clarification` request, its source and selected option match that
 * request, and its generated control text is intact. This keeps arbitrary
 * user messages carrying forged or stale metadata visible.
 */
export function inferLegacyHumanInputControlMessageIndexes(
  messages: Message[],
): ReadonlySet<number> {
  const inferredIndexes = new Set<number>();
  const seenRequests = new Map<string, HumanInputRequest>();
  const requestOrder: string[] = [];
  const answeredRequestIds = new Set<string>();

  const latestUnansweredRequestId = () =>
    [...requestOrder]
      .reverse()
      .find((requestId) => !answeredRequestIds.has(requestId));

  for (const [messageIndex, message] of messages.entries()) {
    const explicitlyHidden = message.additional_kwargs?.hide_from_ui === true;
    const request = extractHumanInputRequest(message);
    if (
      !explicitlyHidden &&
      request &&
      isAskClarificationRequestMessage(message, request) &&
      !seenRequests.has(request.request_id)
    ) {
      seenRequests.set(request.request_id, request);
      requestOrder.push(request.request_id);
      continue;
    }

    const response = extractHumanInputResponse(message);
    if (response) {
      const matchingRequest = seenRequests.get(response.request_id);
      const isValidResponse =
        matchingRequest !== undefined &&
        isResponseValidForRequest(matchingRequest, response) &&
        !answeredRequestIds.has(response.request_id);

      if (explicitlyHidden) {
        if (isValidResponse) {
          answeredRequestIds.add(response.request_id);
        }
        continue;
      }

      if (
        isValidResponse &&
        matchingRequest !== undefined &&
        latestUnansweredRequestId() === response.request_id &&
        extractPlainMessageText(message) ===
          buildHumanInputResponseText(matchingRequest, response)
      ) {
        inferredIndexes.add(messageIndex);
        answeredRequestIds.add(response.request_id);
      }
      continue;
    }

    if (message.type === "human" && !explicitlyHidden) {
      const latestRequestId = latestUnansweredRequestId();
      if (latestRequestId !== undefined) {
        answeredRequestIds.add(latestRequestId);
      }
    }
  }

  return inferredIndexes;
}

export function deriveHumanInputThreadState(
  messages: Message[],
  isVisibleMessage: (message: Message) => boolean = (message) =>
    message.additional_kwargs?.hide_from_ui !== true,
): HumanInputThreadState {
  const answeredResponses = new Map<string, HumanInputResponse>();
  const seenRequests = new Map<string, HumanInputRequest>();
  const requestOrder: string[] = [];

  for (const message of messages) {
    if (isVisibleMessage(message)) {
      const request = extractHumanInputRequest(message);
      if (request) {
        seenRequests.set(request.request_id, request);
        requestOrder.push(request.request_id);
      }
    }

    const response = extractHumanInputResponse(message);
    if (
      response &&
      seenRequests.has(response.request_id) &&
      !answeredResponses.has(response.request_id)
    ) {
      answeredResponses.set(response.request_id, response);
      continue;
    }

    if (message.type === "human" && isVisibleMessage(message) && !response) {
      const latestUnansweredId = [...requestOrder]
        .reverse()
        .find((requestId) => !answeredResponses.has(requestId));
      const request =
        latestUnansweredId === undefined
          ? undefined
          : seenRequests.get(latestUnansweredId);
      if (latestUnansweredId !== undefined && request) {
        answeredResponses.set(latestUnansweredId, {
          version: 1,
          kind: "human_input_response",
          source: request.source,
          request_id: latestUnansweredId,
          response_kind: "text",
          value: extractPlainMessageText(message) || "-",
        });
      }
    }
  }

  const latestOpenRequestId =
    [...requestOrder]
      .reverse()
      .find((requestId) => !answeredResponses.has(requestId)) ?? null;

  return { answeredResponses, latestOpenRequestId };
}

export function hasOpenHumanInputRequest(
  messages: Message[],
  isVisibleMessage?: (message: Message) => boolean,
) {
  return (
    deriveHumanInputThreadState(messages, isVisibleMessage)
      .latestOpenRequestId !== null
  );
}

export function createHumanInputOptionResponse(
  request: HumanInputRequest,
  option: HumanInputOption,
): HumanInputResponse {
  return {
    version: 1,
    kind: "human_input_response",
    source: request.source,
    request_id: request.request_id,
    response_kind: "option",
    option_id: option.id,
    value: option.value,
  };
}

function formatFormValue(value: HumanInputFormValue) {
  if (Array.isArray(value)) {
    return value.join(", ");
  }
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  return String(value);
}

function isEmptyFormValue(value: HumanInputFormValue | undefined) {
  if (value === undefined) {
    return true;
  }
  if (typeof value === "string") {
    return value.trim().length === 0;
  }
  if (Array.isArray(value)) {
    return value.length === 0;
  }
  return false;
}

export function buildHumanInputFormSummary(
  request: HumanInputRequest,
  values: Record<string, HumanInputFormValue>,
) {
  const parts: string[] = [];
  for (const field of request.fields ?? []) {
    const value = readHumanInputFormValue(values, field.name);
    if (isEmptyFormValue(value)) {
      continue;
    }
    parts.push(`${field.label}: ${formatFormValue(value!)}`);
  }
  return parts.join("; ");
}

export function createHumanInputFormResponse(
  request: HumanInputRequest,
  values: Record<string, HumanInputFormValue>,
): Extract<HumanInputResponse, { response_kind: "text" }> {
  const record: Record<string, HumanInputFormValue> = {};
  for (const field of request.fields ?? []) {
    const value = readHumanInputFormValue(values, field.name);
    if (isEmptyFormValue(value)) {
      continue;
    }
    record[field.name] = value!;
  }
  return {
    version: 1,
    kind: "human_input_response",
    source: request.source,
    request_id: request.request_id,
    response_kind: "text",
    value: buildHumanInputFormSummary(request, values) || "-",
    form_values: record,
  };
}

export function createHumanInputTextResponse(
  request: HumanInputRequest,
  value: string,
): HumanInputResponse {
  return {
    version: 1,
    kind: "human_input_response",
    source: request.source,
    request_id: request.request_id,
    response_kind: "text",
    value,
  };
}

export function buildHumanInputResponseText(
  request: HumanInputRequest,
  response: HumanInputResponse,
) {
  const structuredValues =
    response.response_kind === "text" && response.form_values !== undefined
      ? ` [values: ${JSON.stringify(response.form_values)}]`
      : "";
  return `For your clarification "${request.question}", my answer is: ${response.value}${structuredValues}`;
}

export function humanInputResponseDisplayValue(
  request: HumanInputRequest,
  response: HumanInputResponse,
) {
  if (
    request.input_mode !== "form" ||
    response.response_kind !== "text" ||
    response.form_values !== undefined
  ) {
    return response.value;
  }

  const marker = " [values: ";
  const markerIndex = response.value.lastIndexOf(marker);
  if (markerIndex < 0 || !response.value.endsWith("]")) {
    return response.value;
  }

  const summary = response.value.slice(0, markerIndex) || "-";
  const serializedValues = response.value.slice(
    markerIndex + marker.length,
    -1,
  );
  try {
    const formValues = parseHumanInputFormValues(JSON.parse(serializedValues));
    if (
      formValues === undefined ||
      !isResponseValidForRequest(request, {
        ...response,
        value: summary,
        form_values: formValues,
      })
    ) {
      return response.value;
    }
  } catch {
    return response.value;
  }
  return summary;
}
