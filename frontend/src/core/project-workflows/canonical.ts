import type { JsonSchema, JsonValue } from "./types";
import { workflowSpecV1Schema } from "./types";

// Changing this identifier or its spelling rules is a checksum migration event.
export const CANONICAL_BINARY64_ALGORITHM =
  "ieee754-binary64-exact-decimal-v1" as const;

const binary64Buffer = new ArrayBuffer(8);
const binary64View = new DataView(binary64Buffer);
const BINARY64_FRACTION_MASK = (1n << 52n) - 1n;
const BINARY64_IMPLICIT_BIT = 1n << 52n;
const MAX_SAFE_INTEGER = (1n << 53n) - 1n;
const CANONICAL_TEXT_CHUNK_SIZE = 1_024;
const utf8Encoder = new TextEncoder();

const normalizeText = (value: string): string => {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const nextCodeUnit = value.charCodeAt(index + 1);
      if (
        index + 1 >= value.length ||
        nextCodeUnit < 0xdc00 ||
        nextCodeUnit > 0xdfff
      ) {
        throw new Error(
          "canonical JSON text contains an unpaired UTF-16 surrogate",
        );
      }
      index += 1;
    } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      throw new Error(
        "canonical JSON text contains an unpaired UTF-16 surrogate",
      );
    }
  }

  return value.normalize("NFC");
};

const compareText = (left: string, right: string): number => {
  const normalizedLeft = normalizeText(left);
  const normalizedRight = normalizeText(right);
  const leftCodePoints = Array.from(normalizedLeft, (character) =>
    character.codePointAt(0),
  );
  const rightCodePoints = Array.from(normalizedRight, (character) =>
    character.codePointAt(0),
  );
  const sharedLength = Math.min(leftCodePoints.length, rightCodePoints.length);
  for (let index = 0; index < sharedLength; index += 1) {
    const leftCodePoint = leftCodePoints[index]!;
    const rightCodePoint = rightCodePoints[index]!;
    if (leftCodePoint < rightCodePoint) return -1;
    if (leftCodePoint > rightCodePoint) return 1;
  }
  if (leftCodePoints.length < rightCodePoints.length) return -1;
  if (leftCodePoints.length > rightCodePoints.length) return 1;
  return 0;
};

const encodeCanonicalNumber = (value: number): string => {
  binary64View.setFloat64(0, value, false);
  const bits = binary64View.getBigUint64(0, false);
  const negative = bits >> 63n === 1n;
  const exponentBits = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & BINARY64_FRACTION_MASK;
  if (exponentBits === 0x7ff) {
    throw new Error("canonical JSON numbers must be finite");
  }
  if (exponentBits === 0 && fraction === 0n) return "0";

  let significand: bigint;
  let binaryExponent: number;
  if (exponentBits === 0) {
    significand = fraction;
    binaryExponent = -1074;
  } else {
    significand = BINARY64_IMPLICIT_BIT | fraction;
    binaryExponent = exponentBits - 1023 - 52;
  }

  if (binaryExponent >= 0) {
    const integer = significand << BigInt(binaryExponent);
    if (integer > MAX_SAFE_INTEGER) {
      throw new Error("canonical JSON integer exceeds safe-integer range");
    }
    return `${negative ? "-" : ""}${integer.toString(10)}`;
  }

  let denominatorPower = -binaryExponent;
  while (denominatorPower > 0 && (significand & 1n) === 0n) {
    significand >>= 1n;
    denominatorPower -= 1;
  }

  if (denominatorPower === 0) {
    if (significand > MAX_SAFE_INTEGER) {
      throw new Error("canonical JSON integer exceeds safe-integer range");
    }
    return `${negative ? "-" : ""}${significand.toString(10)}`;
  }

  const digits = (significand * 5n ** BigInt(denominatorPower)).toString(10);
  const scientificExponent = digits.length - 1 - denominatorPower;
  const coefficient =
    digits.length === 1 ? digits : `${digits[0]}.${digits.slice(1)}`;
  return `${negative ? "-" : ""}${coefficient}e${scientificExponent}`;
};

const canonicalizeJsonValue = (value: JsonValue): JsonValue => {
  if (typeof value === "string") return normalizeText(value);
  if (typeof value === "number") {
    encodeCanonicalNumber(value);
    return Object.is(value, -0) ? 0 : value;
  }
  if (value === null || typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.map(canonicalizeJsonValue);

  const normalizedEntries = Object.entries(value)
    .filter(([, nestedValue]) => nestedValue !== undefined)
    .map(
      ([key, nestedValue]) =>
        [normalizeText(key), canonicalizeJsonValue(nestedValue)] as const,
    )
    .sort(([left], [right]) => compareText(left, right));

  for (let index = 1; index < normalizedEntries.length; index += 1) {
    if (normalizedEntries[index - 1]?.[0] === normalizedEntries[index]?.[0]) {
      throw new Error("Unicode normalization produced duplicate JSON keys");
    }
  }

  return Object.fromEntries(normalizedEntries);
};

const omitFields = (
  value: Record<string, unknown>,
  fields: ReadonlySet<string>,
): Record<string, unknown> =>
  Object.fromEntries(
    Object.entries(value).filter(([field]) => !fields.has(field)),
  );

const nodePresentationFields = new Set(["custom_label", "description"]);
const declarationPresentationFields = new Set(["description"]);

export const canonicalizeWorkflowSemanticValue = (
  input: unknown,
): JsonSchema => {
  const spec = workflowSpecV1Schema.parse(input);
  const semanticProjection = {
    schema_version: spec.schema_version,
    entry_node_id: spec.entry_node_id,
    nodes: [...spec.nodes]
      .sort((left, right) => compareText(left.id, right.id))
      .map((node) =>
        omitFields(
          node as unknown as Record<string, unknown>,
          nodePresentationFields,
        ),
      ),
    transitions: [...spec.transitions].sort((left, right) =>
      compareText(left.id, right.id),
    ),
    workflow_inputs: [...spec.workflow_inputs]
      .sort((left, right) => compareText(left.id, right.id))
      .map((declaration) =>
        omitFields(declaration, declarationPresentationFields),
      ),
    workflow_outputs: [...spec.workflow_outputs]
      .sort((left, right) => compareText(left.id, right.id))
      .map((declaration) =>
        omitFields(declaration, declarationPresentationFields),
      ),
    credential_slots: [...spec.credential_slots].sort((left, right) =>
      compareText(left.id, right.id),
    ),
  } as unknown as JsonValue;

  return canonicalizeJsonValue(semanticProjection) as JsonSchema;
};

export const serializeWorkflowSemanticChecksumInput = (
  input: unknown,
): string =>
  serializeCanonicalJsonValue(canonicalizeWorkflowSemanticValue(input));

const serializeCanonicalValue = (value: JsonValue): string => {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return encodeCanonicalNumber(value);
  if (typeof value === "string") return JSON.stringify(normalizeText(value));
  if (Array.isArray(value)) {
    return `[${value.map(serializeCanonicalValue).join(",")}]`;
  }

  const normalizedEntries = Object.entries(value)
    .filter(([, nestedValue]) => nestedValue !== undefined)
    .map(([key, nestedValue]) => [normalizeText(key), nestedValue] as const)
    .sort(([left], [right]) => compareText(left, right));
  for (let index = 1; index < normalizedEntries.length; index += 1) {
    if (normalizedEntries[index - 1]?.[0] === normalizedEntries[index]?.[0]) {
      throw new Error("Unicode normalization produced duplicate JSON keys");
    }
  }

  return `{${normalizedEntries
    .map(
      ([key, nestedValue]) =>
        `${JSON.stringify(key)}:${serializeCanonicalValue(nestedValue)}`,
    )
    .join(",")}}`;
};

export const serializeCanonicalJsonValue = (value: JsonValue): string =>
  serializeCanonicalValue(value);

export class CanonicalJsonUtf8BudgetExceededError extends Error {
  constructor() {
    super("canonical JSON exceeds the UTF-8 byte budget");
    this.name = "CanonicalJsonUtf8BudgetExceededError";
  }
}

class CanonicalUtf8Writer {
  readonly chunks: string[] = [];
  utf8Bytes = 0;

  constructor(readonly maxUtf8Bytes: number) {
    if (!Number.isSafeInteger(maxUtf8Bytes) || maxUtf8Bytes < 0) {
      throw new Error(
        "canonical JSON UTF-8 byte budget must be a non-negative safe integer",
      );
    }
  }

  append(value: string): void {
    const byteCount = utf8Encoder.encode(value).byteLength;
    if (this.utf8Bytes + byteCount > this.maxUtf8Bytes) {
      throw new CanonicalJsonUtf8BudgetExceededError();
    }
    this.chunks.push(value);
    this.utf8Bytes += byteCount;
  }

  finish(): string {
    return this.chunks.join("");
  }
}

const jsonCharacterEscape = (character: string): string => {
  switch (character) {
    case '"':
      return '\\"';
    case "\\":
      return "\\\\";
    case "\b":
      return "\\b";
    case "\f":
      return "\\f";
    case "\n":
      return "\\n";
    case "\r":
      return "\\r";
    case "\t":
      return "\\t";
    default: {
      const codePoint = character.codePointAt(0)!;
      return codePoint < 0x20
        ? `\\u${codePoint.toString(16).padStart(4, "0")}`
        : character;
    }
  }
};

const serializeCanonicalTextWithinBudget = (
  value: string,
  writer: CanonicalUtf8Writer,
): void => {
  const normalized = normalizeText(value);
  writer.append('"');
  let buffered = "";
  for (const character of normalized) {
    buffered += jsonCharacterEscape(character);
    if (buffered.length >= CANONICAL_TEXT_CHUNK_SIZE) {
      writer.append(buffered);
      buffered = "";
    }
  }
  if (buffered.length > 0) writer.append(buffered);
  writer.append('"');
};

const serializeCanonicalValueWithinBudget = (
  value: JsonValue,
  writer: CanonicalUtf8Writer,
): void => {
  if (value === null) {
    writer.append("null");
    return;
  }
  if (typeof value === "boolean") {
    writer.append(value ? "true" : "false");
    return;
  }
  if (typeof value === "number") {
    writer.append(encodeCanonicalNumber(value));
    return;
  }
  if (typeof value === "string") {
    serializeCanonicalTextWithinBudget(value, writer);
    return;
  }
  if (Array.isArray(value)) {
    writer.append("[");
    let index = 0;
    for (const nestedValue of value) {
      if (index > 0) writer.append(",");
      serializeCanonicalValueWithinBudget(nestedValue, writer);
      index += 1;
    }
    writer.append("]");
    return;
  }

  const normalizedEntries = Object.entries(value)
    .filter(([, nestedValue]) => nestedValue !== undefined)
    .map(([key, nestedValue]) => [normalizeText(key), nestedValue] as const)
    .sort(([left], [right]) => compareText(left, right));
  for (let index = 1; index < normalizedEntries.length; index += 1) {
    if (normalizedEntries[index - 1]?.[0] === normalizedEntries[index]?.[0]) {
      throw new Error("Unicode normalization produced duplicate JSON keys");
    }
  }

  writer.append("{");
  normalizedEntries.forEach(([key, nestedValue], index) => {
    if (index > 0) writer.append(",");
    serializeCanonicalTextWithinBudget(key, writer);
    writer.append(":");
    serializeCanonicalValueWithinBudget(nestedValue, writer);
  });
  writer.append("}");
};

export const serializeCanonicalJsonValueWithinUtf8Budget = (
  value: JsonValue,
  maxUtf8Bytes: number,
): { canonical: string; utf8Bytes: number } => {
  const writer = new CanonicalUtf8Writer(maxUtf8Bytes);
  serializeCanonicalValueWithinBudget(value, writer);
  return { canonical: writer.finish(), utf8Bytes: writer.utf8Bytes };
};
