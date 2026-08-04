import { z } from "zod";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  projectClientScopeSchema,
  type PrivateWorkAccess,
} from "@/core/private-work/types";

import {
  createProjectChannelGroupBindingChallengeInputSchema,
  projectChannelGroupBindingChallengeSchema,
  projectChannelGroupBindingSchema,
  projectChannelGroupBindingsResponseSchema,
  updateProjectChannelGroupBindingInputSchema,
  type CreateProjectChannelGroupBindingChallengeInput,
  type ProjectChannelGroupBinding,
  type ProjectChannelGroupBindingChallenge,
  type ProjectChannelGroupBindingsResponse,
  type UpdateProjectChannelGroupBindingInput,
} from "./types";

export type ProjectChannelGroupBindingAccess = Pick<
  PrivateWorkAccess,
  "apiBaseURL" | "scope"
>;

function projectChannelGroupBindingsBaseURL(
  access: ProjectChannelGroupBindingAccess,
) {
  const scope = projectClientScopeSchema.parse(access.scope);
  const privateSuffix = `/projects/${scope.projectId}/private-work`;
  if (!access.apiBaseURL.endsWith(privateSuffix)) {
    throw new Error(
      "Project channel group bindings require a project-scoped private-work URL",
    );
  }
  return `${access.apiBaseURL.slice(0, -privateSuffix.length)}/projects/${scope.projectId}/channel-group-bindings`;
}

async function readResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
  fallback: string,
) {
  if (!response.ok) await throwGatewayApiError(response, fallback);
  return schema.parse(await response.json());
}

export async function listProjectChannelGroupBindings(
  access: ProjectChannelGroupBindingAccess,
  signal?: AbortSignal,
): Promise<ProjectChannelGroupBindingsResponse> {
  const response = await fetchWithAuth(
    projectChannelGroupBindingsBaseURL(access),
    { signal },
  );
  return readResponse(
    response,
    projectChannelGroupBindingsResponseSchema,
    "Failed to load project channel group bindings",
  );
}

export async function createProjectChannelGroupBindingChallenge(
  access: ProjectChannelGroupBindingAccess,
  input: CreateProjectChannelGroupBindingChallengeInput,
  signal?: AbortSignal,
): Promise<ProjectChannelGroupBindingChallenge> {
  const parsed =
    createProjectChannelGroupBindingChallengeInputSchema.parse(input);
  const response = await fetchWithAuth(
    `${projectChannelGroupBindingsBaseURL(access)}/challenge`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: parsed.provider,
        agent_asset_id: parsed.agentAssetId,
        agent_scope: parsed.agentScope,
      }),
      signal,
    },
  );
  return readResponse(
    response,
    projectChannelGroupBindingChallengeSchema,
    "Failed to create a channel group binding challenge",
  );
}

export async function updateProjectChannelGroupBinding(
  access: ProjectChannelGroupBindingAccess,
  bindingId: string,
  input: UpdateProjectChannelGroupBindingInput,
  signal?: AbortSignal,
): Promise<ProjectChannelGroupBinding> {
  const parsedId = z.string().uuid().parse(bindingId);
  const parsed = updateProjectChannelGroupBindingInputSchema.parse(input);
  const response = await fetchWithAuth(
    `${projectChannelGroupBindingsBaseURL(access)}/${encodeURIComponent(parsedId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_revision: parsed.expectedRevision,
        ...(parsed.enabled === undefined ? {} : { enabled: parsed.enabled }),
        ...(parsed.agentAssetId === undefined
          ? {}
          : {
              agent_asset_id: parsed.agentAssetId,
              agent_scope: parsed.agentScope,
            }),
      }),
      signal,
    },
  );
  return readResponse(
    response,
    projectChannelGroupBindingSchema,
    "Failed to update the channel group binding",
  );
}

export async function deleteProjectChannelGroupBinding(
  access: ProjectChannelGroupBindingAccess,
  bindingId: string,
  expectedRevision: number,
  signal?: AbortSignal,
) {
  const parsedId = z.string().uuid().parse(bindingId);
  const parsedRevision = z.number().int().positive().parse(expectedRevision);
  const query = new URLSearchParams({
    expected_revision: String(parsedRevision),
  });
  const response = await fetchWithAuth(
    `${projectChannelGroupBindingsBaseURL(access)}/${encodeURIComponent(parsedId)}?${query.toString()}`,
    { method: "DELETE", signal },
  );
  if (!response.ok) {
    await throwGatewayApiError(
      response,
      "Failed to delete the channel group binding",
    );
  }
}
