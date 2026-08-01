import { z } from "zod";

const CANONICAL_NONNEGATIVE_DECIMAL = /^(?:0|[1-9][0-9]*)$/u;
const POSTGRES_SIGNED_BIGINT_MAX = "9223372036854775807";

export function compareEventSequences(left: string, right: string): number {
  if (left.length !== right.length) {
    return left.length < right.length ? -1 : 1;
  }
  if (left === right) {
    return 0;
  }
  return left < right ? -1 : 1;
}

export const eventSequenceSchema = z
  .string()
  .regex(CANONICAL_NONNEGATIVE_DECIMAL)
  .refine(
    (value) => compareEventSequences(value, POSTGRES_SIGNED_BIGINT_MAX) <= 0,
    "Event sequence exceeds PostgreSQL BIGINT",
  );

export type EventSequence = z.infer<typeof eventSequenceSchema>;
