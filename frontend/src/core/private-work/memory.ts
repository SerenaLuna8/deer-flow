/**
 * Rolling compatibility facade for Project Memory callers.
 *
 * New code should import from `./memory/api`, `./memory/schemas`,
 * `./memory/types`, `./memory/query-keys`, or `./memory/permissions`.
 */
export * from "./memory/api";
export * from "./memory/permissions";
export * from "./memory/preparation-hooks";
export * from "./memory/query-keys";
export * from "./memory/schemas";
export * from "./memory/types";
