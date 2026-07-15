import type { Client as LangGraphClient } from "@langchain/langgraph-sdk/client";
import { z } from "zod";

export const projectClientScopeSchema = z
  .object({
    accountId: z.string().uuid(),
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
