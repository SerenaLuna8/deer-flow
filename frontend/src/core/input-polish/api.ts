import { throwGatewayApiError } from "@/core/api/errors";
import { fetch } from "@/core/api/fetcher";
import {
  projectClientScopeSchema,
  type ProjectPrivateWorkScope,
} from "@/core/private-work/types";

export type InputPolishRequest = {
  text: string;
  locale?: string;
  thread_id: string;
};

export type InputPolishResponse = {
  rewritten_text: string;
  changed: boolean;
};

export async function polishInputDraft(
  access: Pick<ProjectPrivateWorkScope, "apiBaseURL" | "scope">,
  request: InputPolishRequest,
  options?: { signal?: AbortSignal },
): Promise<InputPolishResponse> {
  const scope = projectClientScopeSchema.parse(access.scope);
  const suffix = `/projects/${scope.projectId}/private-work`;
  if (!access.apiBaseURL.endsWith(suffix)) {
    throw new Error("Input polish requires a project-scoped private-work URL");
  }
  const response = await fetch(`${access.apiBaseURL}/input-polish`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal: options?.signal,
  });

  if (!response.ok) {
    await throwGatewayApiError(response, "Failed to polish input");
  }

  return response.json() as Promise<InputPolishResponse>;
}
