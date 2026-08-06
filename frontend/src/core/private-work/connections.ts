import { z } from "zod";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import type {
  ChannelConnectResponse,
  ChannelConnection,
  ChannelProviderId,
} from "@/core/private-work/connection-types";

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

const projectConnectionProviderSchema = z
  .object({
    provider: z.string().min(1),
    display_name: z.string().min(1),
    enabled: z.boolean(),
    configured: z.boolean(),
    connectable: z.boolean(),
    unavailable_reason: z.string().nullable().optional(),
    auth_mode: z.enum(["deep_link", "binding_code"]),
    connection_status: z.string().min(1),
  })
  .strict();

const projectConnectionProvidersResponseSchema = z
  .object({
    enabled: z.boolean(),
    providers: z.array(projectConnectionProviderSchema),
  })
  .strict();

export type ProjectConnectionProvider = z.infer<
  typeof projectConnectionProviderSchema
>;
export type ProjectConnectionProvidersResponse = z.infer<
  typeof projectConnectionProvidersResponseSchema
>;

export const PROJECT_CHANNEL_INSTANCE_STATUSES = [
  "unconfigured",
  "disabled",
  "stopped",
  "starting",
  "running",
  "error",
] as const;

const PROJECT_CHANNEL_PUBLIC_CONFIG_FIELDS: Readonly<
  Record<string, readonly string[]>
> = {
  dingtalk: ["client_id"],
  discord: [],
  feishu: ["app_id", "domain"],
  slack: [],
  telegram: ["bot_username"],
  wechat: [],
  wecom: ["bot_id"],
};

const projectChannelInstanceSchema = z
  .object({
    id: z.string().uuid().nullable(),
    provider: z.string().min(1),
    display_name: z.string().min(1),
    status: z.enum(PROJECT_CHANNEL_INSTANCE_STATUSES),
    enabled: z.boolean(),
    configured: z.boolean(),
    credential_configured: z.boolean(),
    public_config: z.record(z.string(), z.string()),
    updated_at: z.string().datetime({ offset: true }).nullable(),
    last_error: z.string().min(1).nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    const allowed = new Set(
      PROJECT_CHANNEL_PUBLIC_CONFIG_FIELDS[value.provider] ?? [],
    );
    for (const key of Object.keys(value.public_config)) {
      if (!allowed.has(key)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["public_config", key],
          message: "Unsupported public channel configuration field",
        });
      }
    }
  });

const projectChannelInstancesResponseSchema = z
  .object({
    instances: z.array(projectChannelInstanceSchema),
  })
  .strict();

const projectChannelInstanceResponseSchema = projectChannelInstanceSchema;

const configureProjectChannelInstanceInputSchema = z
  .object({
    publicConfig: z.record(z.string(), z.string()),
    credentials: z.record(z.string(), z.string()).optional(),
    enabled: z.boolean(),
  })
  .strict();

export type ProjectChannelInstance = z.infer<
  typeof projectChannelInstanceSchema
>;
export type ProjectChannelInstanceStatus =
  (typeof PROJECT_CHANNEL_INSTANCE_STATUSES)[number];
export type ProjectChannelInstancesResponse = z.infer<
  typeof projectChannelInstancesResponseSchema
>;
export type ConfigureProjectChannelInstanceInput = z.infer<
  typeof configureProjectChannelInstanceInputSchema
>;

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

function projectBaseURL(access: ProjectConnectionAccess) {
  const scope = projectClientScopeSchema.parse(access.scope);
  const privateSuffix = `/projects/${scope.projectId}/private-work`;
  if (!access.apiBaseURL.endsWith(privateSuffix)) {
    throw new Error(
      "Project connections require a project-scoped private-work URL",
    );
  }
  return `${access.apiBaseURL.slice(0, -privateSuffix.length)}/projects/${scope.projectId}`;
}

function projectConnectionsBaseURL(access: ProjectConnectionAccess) {
  return `${projectBaseURL(access)}/connections`;
}

function projectChannelInstancesBaseURL(access: ProjectConnectionAccess) {
  return `${projectBaseURL(access)}/channel-instances`;
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

export function projectConnectionProvidersQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "connection-providers");
}

export function projectChannelInstancesQueryKey(scope: ProjectClientScope) {
  return privateWorkQueryKey(scope, "channel-instances");
}

export async function listProjectChannelInstances(
  access: ProjectConnectionAccess,
  signal?: AbortSignal,
): Promise<ProjectChannelInstancesResponse> {
  const response = await fetchWithAuth(projectChannelInstancesBaseURL(access), {
    signal,
  });
  return readResponse(
    response,
    projectChannelInstancesResponseSchema,
    "Failed to load project channel instances",
  );
}

export async function configureProjectChannelInstance(
  access: ProjectConnectionAccess,
  provider: ChannelProviderId,
  input: ConfigureProjectChannelInstanceInput,
  signal?: AbortSignal,
): Promise<ProjectChannelInstance> {
  const parsedProvider = z.string().min(1).parse(provider);
  const parsedInput = configureProjectChannelInstanceInputSchema.parse(input);
  const response = await fetchWithAuth(
    `${projectChannelInstancesBaseURL(access)}/${encodeURIComponent(parsedProvider)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        public_config: parsedInput.publicConfig,
        ...(parsedInput.credentials === undefined
          ? {}
          : { credentials: parsedInput.credentials }),
        enabled: parsedInput.enabled,
      }),
      signal,
    },
  );
  return readResponse(
    response,
    projectChannelInstanceResponseSchema,
    `Failed to configure ${parsedProvider}`,
  );
}

export async function setProjectChannelInstanceEnabled(
  access: ProjectConnectionAccess,
  provider: ChannelProviderId,
  enabled: boolean,
  signal?: AbortSignal,
): Promise<ProjectChannelInstance> {
  const parsedProvider = z.string().min(1).parse(provider);
  const response = await fetchWithAuth(
    `${projectChannelInstancesBaseURL(access)}/${encodeURIComponent(parsedProvider)}/${enabled ? "enable" : "disable"}`,
    {
      method: "POST",
      signal,
    },
  );
  return readResponse(
    response,
    projectChannelInstanceResponseSchema,
    `Failed to ${enabled ? "enable" : "disable"} ${parsedProvider}`,
  );
}

export async function deleteProjectChannelInstance(
  access: ProjectConnectionAccess,
  provider: ChannelProviderId,
  signal?: AbortSignal,
) {
  const parsedProvider = z.string().min(1).parse(provider);
  const response = await fetchWithAuth(
    `${projectChannelInstancesBaseURL(access)}/${encodeURIComponent(parsedProvider)}`,
    {
      method: "DELETE",
      signal,
    },
  );
  if (!response.ok) {
    await throwGatewayApiError(response, `Failed to delete ${parsedProvider}`);
  }
}

export async function listProjectConnectionProviders(
  access: ProjectConnectionAccess,
  signal?: AbortSignal,
): Promise<ProjectConnectionProvidersResponse> {
  const response = await fetchWithAuth(
    `${projectConnectionsBaseURL(access)}/providers`,
    { signal },
  );
  return readResponse(
    response,
    projectConnectionProvidersResponseSchema,
    "Failed to load project connection providers",
  );
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
