import type { Client as LangGraphClient } from "@langchain/langgraph-sdk/client";
import { z } from "zod";

export const projectClientScopeSchema = z
  .object({
    // Local auth-disabled mode uses the backend's canonical synthetic
    // account. It is cache identity only; project authority is server-derived.
    accountId: z.union([z.string().uuid(), z.literal("default")]),
    projectId: z.string().uuid(),
  })
  .strict();

export type ProjectClientScope = z.infer<typeof projectClientScopeSchema>;

export type RunMetadataStorage = {
  getItem(key: `lg:stream:${string}`): string | null;
  setItem(key: `lg:stream:${string}`, value: string): void;
  removeItem(key: `lg:stream:${string}`): void;
};

export type PrivateWorkAccess = {
  scope: ProjectClientScope | null;
  client: LangGraphClient;
  apiBaseURL: string;
  queryKeyPrefix: readonly unknown[];
  reconnectOnMount: boolean | (() => RunMetadataStorage);
  runAbortable?<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T>;
  isActive?(): boolean;
};

export type ProjectPrivateWorkScope = Omit<PrivateWorkAccess, "scope"> & {
  scope: ProjectClientScope;
};

export function runPrivateWorkAbortable<T>(
  access: PrivateWorkAccess,
  operation: (signal?: AbortSignal) => Promise<T>,
) {
  return access.runAbortable
    ? access.runAbortable((signal) => operation(signal))
    : operation();
}

export function isPrivateWorkAccessActive(access: PrivateWorkAccess) {
  return access.isActive?.() ?? true;
}
