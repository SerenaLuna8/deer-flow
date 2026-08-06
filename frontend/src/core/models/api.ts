import { AuthRequiredError, fetch as fetchWithAuth } from "../api/fetcher";
import { getBackendBaseURL } from "../config";
import { isStaticWebsiteOnly } from "../static-mode";

import { modelsResponseSchema, type ModelsResponse } from "./types";

const STATIC_MODELS_RESPONSE: ModelsResponse = {
  models: [],
  token_usage: { enabled: false },
};

export class ModelsApiError extends Error {
  readonly status: number;
  readonly code:
    | "AUTH_REQUIRED"
    | "NETWORK_ERROR"
    | "REQUEST_FAILED"
    | "INVALID_RESPONSE";

  constructor(status: number, code: ModelsApiError["code"], message: string) {
    super(message);
    this.name = "ModelsApiError";
    this.status = status;
    this.code = code;
  }
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

export async function loadModels(
  signal?: AbortSignal,
): Promise<ModelsResponse> {
  if (isStaticWebsiteOnly()) {
    return STATIC_MODELS_RESPONSE;
  }

  let response: Response;
  try {
    response = await fetchWithAuth(`${getBackendBaseURL()}/api/models`, {
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    if (error instanceof AuthRequiredError) {
      throw new ModelsApiError(401, "AUTH_REQUIRED", "Authentication required");
    }
    throw new ModelsApiError(
      0,
      "NETWORK_ERROR",
      "Models are temporarily unavailable.",
    );
  }

  if (!response.ok) {
    throw new ModelsApiError(
      response.status,
      "REQUEST_FAILED",
      "Models request failed.",
    );
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new ModelsApiError(
      response.status,
      "INVALID_RESPONSE",
      "Models response was invalid.",
    );
  }
  const parsed = modelsResponseSchema.safeParse(body);
  if (!parsed.success) {
    throw new ModelsApiError(
      response.status,
      "INVALID_RESPONSE",
      "Models response was invalid.",
    );
  }
  return parsed.data;
}
