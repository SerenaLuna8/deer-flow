import { z } from "zod";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import type {
  ChannelConnectResponse,
  ChannelConnection,
  ChannelProviderId,
} from "@/core/channels/types";

import { privateWorkQueryKey } from "./query-keys";
import {
  projectClientScopeSchema,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "./types";

const connectionSchema = z
  .object({
    id: z.string().min(1),
    provider: z.string().min(1),
    status: z.string().min(1),
    external_account_id: z.string().nullable().optional(),
    external_account_name: z.string().nullable().optional(),
    workspace_id: z.string().nullable().optional(),
    workspace_name: z.string().nullable().optional(),
    scopes: z.array(z.string()),
    metadata: z.record(z.string(), z.unknown()),
  })
  .strict();

const connectionsResponseSchema = z
  .object({ connections: z.array(connectionSchema) })
  .strict();

const connectResponseSchema = z
  .object({
    provider: z.string().min(1),
    mode: z.string().min(1),
    url: z
      .string()
      .url()
      .refine((value) => {
        const protocol = new URL(value).protocol;
        return protocol === "https:" || protocol === "http:";
      })
      .nullable()
      .optional(),
    code: z.string().min(1),
    instruction: z.string().min(1),
    expires_in: z.number().int().nonnegative(),
  })
  .strict();

type ProjectConnectionAccess = Pick<PrivateWorkAccess, "apiBaseURL" | "scope">;

export type ConnectProjectConnectionInput = {
  agentAssetId: string;
  agentScope: "project" | "system";
  redirectAfter?: string | null;
};

function projectConnectionsBaseURL(access: ProjectConnectionAccess) {
  const scope = projectClientScopeSchema.parse(access.scope);
  const privateSuffix = `/projects/${scope.projectId}/private-work`;
  if (!access.apiBaseURL.endsWith(privateSuffix)) {
    throw new Error(
      "Project connections require a project-scoped private-work URL",
    );
  }
  return `${access.apiBaseURL.slice(0, -privateSuffix.length)}/projects/${scope.projectId}/connections`;
}

async function readResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
  fallback: string,
) {
  if (!response.ok) await throwGatewayApiError(response, fallback);
  return schema.parse(await response.json());
}

export function projectConnectionsQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "connections");
}

export async function listProjectConnections(
  access: ProjectConnectionAccess,
  signal?: AbortSignal,
): Promise<ChannelConnection[]> {
  const response = await fetchWithAuth(projectConnectionsBaseURL(access), {
    signal,
  });
  return (
    await readResponse(
      response,
      connectionsResponseSchema,
      "Failed to load project connections",
    )
  ).connections;
}

export async function connectProjectConnection(
  access: ProjectConnectionAccess,
  provider: ChannelProviderId,
  input: ConnectProjectConnectionInput,
  signal?: AbortSignal,
): Promise<ChannelConnectResponse> {
  const parsedProvider = z.string().min(1).parse(provider);
  const parsedInput = z
    .object({
      agentAssetId: z.string().uuid(),
      agentScope: z.enum(["project", "system"]),
      redirectAfter: z.string().nullable().optional(),
    })
    .strict()
    .parse(input);
  const response = await fetchWithAuth(
    `${projectConnectionsBaseURL(access)}/${encodeURIComponent(parsedProvider)}/connect`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent_asset_id: parsedInput.agentAssetId,
        agent_scope: parsedInput.agentScope,
        ...(parsedInput.redirectAfter === undefined
          ? {}
          : { redirect_after: parsedInput.redirectAfter }),
      }),
      signal,
    },
  );
  return readResponse(
    response,
    connectResponseSchema,
    `Failed to connect ${parsedProvider}`,
  );
}

export async function disconnectProjectConnection(
  access: ProjectConnectionAccess,
  connectionId: string,
  signal?: AbortSignal,
) {
  const response = await fetchWithAuth(
    `${projectConnectionsBaseURL(access)}/${encodeURIComponent(z.string().min(1).parse(connectionId))}`,
    { method: "DELETE", signal },
  );
  if (!response.ok) {
    await throwGatewayApiError(
      response,
      "Failed to disconnect project connection",
    );
  }
}
