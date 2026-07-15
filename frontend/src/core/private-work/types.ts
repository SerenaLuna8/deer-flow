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
};
