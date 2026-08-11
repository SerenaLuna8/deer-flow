import { z } from "zod";

export const containsOnlyUnicodeScalars = (value: string): boolean => {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const nextCodeUnit = value.charCodeAt(index + 1);
      if (
        index + 1 >= value.length ||
        nextCodeUnit < 0xdc00 ||
        nextCodeUnit > 0xdfff
      ) {
        return false;
      }
      index += 1;
    } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      return false;
    }
  }
  return true;
};

export const utf8ByteLength = (value: string): number =>
  new TextEncoder().encode(value).byteLength;

export const codePointBoundedString = (minimum: number, maximum: number) =>
  z.string().refine((value) => {
    const length = Array.from(value).length;
    return (
      containsOnlyUnicodeScalars(value) &&
      length >= minimum &&
      length <= maximum
    );
  }, `string must contain between ${minimum} and ${maximum} Unicode code points`);

export const utf8ByteBoundedString = (minimum: number, maximum: number) =>
  z
    .string()
    .refine(
      (value) =>
        containsOnlyUnicodeScalars(value) &&
        utf8ByteLength(value) >= minimum &&
        utf8ByteLength(value) <= maximum,
      `string must contain between ${minimum} and ${maximum} UTF-8 bytes`,
    );

export const addUnicodeScalarIssues = (
  value: unknown,
  context: z.RefinementCtx,
  path: Array<string | number> = [],
): void => {
  if (typeof value === "string") {
    if (!containsOnlyUnicodeScalars(value)) {
      context.addIssue({
        code: "custom",
        message: "Workflow strings must contain only Unicode scalar values",
        path,
      });
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      addUnicodeScalarIssues(item, context, [...path, index]),
    );
    return;
  }
  if (value !== null && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      if (!containsOnlyUnicodeScalars(key)) {
        context.addIssue({
          code: "custom",
          message:
            "Workflow object keys must contain only Unicode scalar values",
          path: [...path, key],
        });
      }
      addUnicodeScalarIssues(item, context, [...path, key]);
    }
  }
};
