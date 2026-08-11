import { serializeCanonicalJsonValue } from "./canonical";
import { sha256Utf8 } from "./sha256";
import type { JsonSchema, JsonValue, WorkflowValueType } from "./types";

export const INLINE_SCHEMA_REF_PREFIX =
  "inline-json-schema-v1:sha256:" as const;
export const MAX_JSON_SCHEMA_DEPTH = 16;
export const MAX_JSON_SCHEMA_PROPERTIES = 256;
export const MAX_JSON_SCHEMA_ITEMS = 10_000;
export const MAX_JSON_SCHEMA_ENUM_VALUES = 256;

const primitiveTypes = new Set([
  "null",
  "boolean",
  "object",
  "array",
  "number",
  "integer",
  "string",
]);
const allowedKeywords = new Set([
  "type",
  "properties",
  "required",
  "additionalProperties",
  "items",
  "minItems",
  "maxItems",
  "minLength",
  "maxLength",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "enum",
  "const",
  "default",
  "title",
  "description",
  "anyOf",
]);

const objectValue = (value: JsonValue | undefined): JsonSchema | undefined =>
  value !== undefined &&
  value !== null &&
  typeof value === "object" &&
  !Array.isArray(value)
    ? value
    : undefined;

const typeNames = (schema: JsonSchema): string[] => {
  const declared = schema.type;
  let names: string[];
  if (typeof declared === "string") names = [declared];
  else if (
    Array.isArray(declared) &&
    declared.length > 0 &&
    declared.every((item) => typeof item === "string")
  )
    names = declared;
  else if (Array.isArray(schema.anyOf) && schema.anyOf.length > 0) {
    names = schema.anyOf.flatMap((alternative) => {
      const child = objectValue(alternative);
      if (child === undefined)
        throw new Error("anyOf alternatives must be object schemas");
      return typeNames(child);
    });
  } else throw new Error("a closed output schema must declare one type");
  if (names.some((name) => !primitiveTypes.has(name)))
    throw new Error("schema type is outside the frozen primitive set");
  if (new Set(names).size !== names.length)
    throw new Error("schema type alternatives must be unique");
  return names;
};

const typeShape = (schema: JsonSchema) => {
  const names = typeNames(schema);
  const nonNull = names
    .filter((name) => name !== "null")
    .map((name) => (name === "integer" ? "number" : name));
  if (new Set(nonNull).size !== 1 || nonNull.length !== 1)
    throw new Error("schema must contain exactly one non-null top-level type");
  return { nonNullType: nonNull[0]!, nullable: names.includes("null") };
};

const validateSchemaNode = (schema: JsonSchema, depth: number): void => {
  if (depth > MAX_JSON_SCHEMA_DEPTH)
    throw new Error("JSON Schema nesting exceeds the compiler limit");
  const unknown = Object.keys(schema).filter(
    (keyword) => !allowedKeywords.has(keyword),
  );
  if (unknown.length > 0)
    throw new Error(`unsupported JSON Schema keyword: ${unknown.sort()[0]}`);
  const names = typeNames(schema);
  const nonNullTypes = names
    .filter((name) => name !== "null")
    .map((name) => (name === "integer" ? "number" : name));
  if (new Set(nonNullTypes).size > 1)
    throw new Error("multiple non-null schema alternatives are unsupported");
  const nonNullType = nonNullTypes[0];
  if (Array.isArray(schema.anyOf)) {
    const siblings = Object.keys(schema).filter(
      (key) => !["anyOf", "title", "description", "default"].includes(key),
    );
    if (siblings.length > 0)
      throw new Error(
        "anyOf cannot be combined with sibling validation keywords",
      );
    for (const alternative of schema.anyOf) {
      const child = objectValue(alternative);
      if (child === undefined)
        throw new Error("anyOf alternatives must be object schemas");
      validateSchemaNode(child, depth + 1);
    }
    return;
  }

  const properties = objectValue(schema.properties);
  if (schema.properties !== undefined && properties === undefined)
    throw new Error("properties must be an object");
  if (properties !== undefined) {
    if (nonNullType !== "object")
      throw new Error("properties is allowed only on object schemas");
    if (Object.keys(properties).length > MAX_JSON_SCHEMA_PROPERTIES)
      throw new Error("JSON Schema properties exceed the compiler limit");
    for (const child of Object.values(properties)) {
      const childSchema = objectValue(child);
      if (childSchema === undefined)
        throw new Error("property schemas must be objects");
      validateSchemaNode(childSchema, depth + 1);
    }
  }
  if (schema.required !== undefined) {
    if (
      nonNullType !== "object" ||
      !Array.isArray(schema.required) ||
      !schema.required.every((item) => typeof item === "string")
    )
      throw new Error("required must be a string array on an object schema");
    const required = schema.required;
    if (
      new Set(required).size !== required.length ||
      required.some((key) => properties?.[key] === undefined)
    )
      throw new Error("required entries must be unique declared properties");
  }
  if (
    schema.additionalProperties !== undefined &&
    (nonNullType !== "object" ||
      typeof schema.additionalProperties !== "boolean")
  )
    throw new Error("additionalProperties must be boolean on an object schema");
  if (schema.items !== undefined) {
    const items = objectValue(schema.items);
    if (nonNullType !== "array" || items === undefined)
      throw new Error("items must be one object schema on an array");
    validateSchemaNode(items, depth + 1);
  }
  for (const [minimumKey, maximumKey, expectedType] of [
    ["minItems", "maxItems", "array"],
    ["minLength", "maxLength", "string"],
  ] as const) {
    const minimum = schema[minimumKey];
    const maximum = schema[maximumKey];
    for (const value of [minimum, maximum]) {
      if (
        value !== undefined &&
        (typeof value !== "number" ||
          !Number.isSafeInteger(value) ||
          value < 0 ||
          value > MAX_JSON_SCHEMA_ITEMS)
      )
        throw new Error(`${minimumKey}/${maximumKey} exceeds the limit`);
    }
    if (
      (minimum !== undefined || maximum !== undefined) &&
      nonNullType !== expectedType
    )
      throw new Error(`${minimumKey}/${maximumKey} has the wrong type`);
    if (
      typeof minimum === "number" &&
      typeof maximum === "number" &&
      minimum > maximum
    )
      throw new Error(`${minimumKey}/${maximumKey} bounds must be ordered`);
  }
  for (const key of [
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
  ] as const) {
    const value = schema[key];
    if (value !== undefined) {
      if (nonNullType !== "number")
        throw new Error(`${key} is allowed only on number schemas`);
      if (typeof value !== "number" || !Number.isFinite(value))
        throw new Error(`${key} must be a finite number`);
    }
  }
  if (
    typeof schema.minimum === "number" &&
    typeof schema.maximum === "number" &&
    schema.minimum > schema.maximum
  )
    throw new Error("numeric bounds must be ordered");
  if (
    schema.enum !== undefined &&
    (!Array.isArray(schema.enum) ||
      schema.enum.length === 0 ||
      schema.enum.length > MAX_JSON_SCHEMA_ENUM_VALUES)
  )
    throw new Error("enum must contain a bounded non-empty value set");
  if (Array.isArray(schema.enum)) {
    const canonical = schema.enum.map(serializeCanonicalJsonValue);
    if (new Set(canonical).size !== canonical.length)
      throw new Error("enum values must be unique");
  }
  for (const key of ["title", "description"] as const) {
    if (schema[key] !== undefined && typeof schema[key] !== "string")
      throw new Error(`${key} must be a string`);
  }
};

const valueMatchesSchema = (value: JsonValue, schema: JsonSchema): boolean => {
  if (Array.isArray(schema.anyOf)) {
    return schema.anyOf.some((alternative) => {
      const child = objectValue(alternative);
      return child !== undefined && valueMatchesSchema(value, child);
    });
  }
  const names = typeNames(schema);
  const matchesDeclaredType = names.some((name) => {
    if (name === "null") return value === null;
    if (name === "boolean") return typeof value === "boolean";
    if (name === "string") return typeof value === "string";
    if (name === "number")
      return typeof value === "number" && Number.isFinite(value);
    if (name === "integer")
      return typeof value === "number" && Number.isSafeInteger(value);
    if (name === "array") return Array.isArray(value);
    return value !== null && typeof value === "object" && !Array.isArray(value);
  });
  if (!matchesDeclaredType) return false;
  const encoded = serializeCanonicalJsonValue(value);
  if (
    Array.isArray(schema.enum) &&
    !schema.enum.some(
      (candidate) => serializeCanonicalJsonValue(candidate) === encoded,
    )
  )
    return false;
  if (
    schema.const !== undefined &&
    serializeCanonicalJsonValue(schema.const) !== encoded
  )
    return false;
  if (typeof value === "string") {
    if (typeof schema.minLength === "number" && value.length < schema.minLength)
      return false;
    if (typeof schema.maxLength === "number" && value.length > schema.maxLength)
      return false;
  }
  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum)
      return false;
    if (typeof schema.maximum === "number" && value > schema.maximum)
      return false;
    if (
      typeof schema.exclusiveMinimum === "number" &&
      value <= schema.exclusiveMinimum
    )
      return false;
    if (
      typeof schema.exclusiveMaximum === "number" &&
      value >= schema.exclusiveMaximum
    )
      return false;
  }
  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems)
      return false;
    if (typeof schema.maxItems === "number" && value.length > schema.maxItems)
      return false;
    const items = objectValue(schema.items);
    if (
      items !== undefined &&
      !value.every((item) => valueMatchesSchema(item, items))
    )
      return false;
  } else if (value !== null && typeof value === "object") {
    const properties = objectValue(schema.properties) ?? {};
    const required = Array.isArray(schema.required)
      ? schema.required.filter((key): key is string => typeof key === "string")
      : [];
    if (required.some((key) => !(key in value))) return false;
    for (const [key, childValue] of Object.entries(value)) {
      const childSchema = objectValue(properties[key]);
      if (childSchema !== undefined) {
        if (!valueMatchesSchema(childValue, childSchema)) return false;
      } else if (schema.additionalProperties === false) return false;
    }
  }
  return true;
};

export const validateStrictJsonSchema = (schema: JsonSchema): void => {
  validateSchemaNode(schema, 1);
  typeShape(schema);
  if (
    schema.default !== undefined &&
    !valueMatchesSchema(schema.default, schema)
  )
    throw new Error("schema default does not satisfy the schema");
  if (schema.const !== undefined && !valueMatchesSchema(schema.const, schema))
    throw new Error("schema const does not satisfy the schema");
};

export const inlineJsonSchemaRef = (schema: JsonSchema): string => {
  validateStrictJsonSchema(schema);
  return `${INLINE_SCHEMA_REF_PREFIX}${sha256Utf8(
    serializeCanonicalJsonValue(schema),
  )}`;
};

export const valueTypeFromJsonSchema = (
  schema: JsonSchema,
  requirement: "any" | "object" = "any",
): WorkflowValueType => {
  validateStrictJsonSchema(schema);
  const shape = typeShape(schema);
  if (
    requirement === "object" &&
    (shape.nonNullType !== "object" || shape.nullable)
  )
    throw new Error("this node output requires a non-null object schema");
  const kind =
    shape.nonNullType === "string"
      ? "string"
      : shape.nonNullType === "number"
        ? "number"
        : shape.nonNullType === "boolean"
          ? "boolean"
          : "json";
  return {
    kind,
    collection: shape.nonNullType === "array",
    nullable: shape.nullable,
    schema_ref: inlineJsonSchemaRef(schema),
  };
};
