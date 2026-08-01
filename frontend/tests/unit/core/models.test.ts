import { beforeEach, describe, expect, test, rs } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));
rs.mock("@/core/static-mode", () => ({ isStaticWebsiteOnly: () => false }));

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { loadModels } from "@/core/models/api";
import { modelsResponseSchema } from "@/core/models/types";

const mockedFetch = rs.mocked(fetchWithAuth);

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("public models contract", () => {
  const response = {
    models: [
      {
        name: "analysis-pro",
        model: "analysis-pro",
        display_name: "分析模型 Pro",
        description: "",
        supports_thinking: true,
        supports_reasoning_effort: true,
        supports_vision: false,
        is_default: true,
      },
    ],
    token_usage: { enabled: true },
  };

  test("matches the Gateway response exactly and rejects undocumented fields", () => {
    expect(modelsResponseSchema.parse(response)).toEqual(response);
    expect(
      modelsResponseSchema.safeParse({
        ...response,
        models: [{ ...response.models[0], id: "not-in-public-contract" }],
      }).success,
    ).toBe(false);
    for (const forbidden of [
      { provider_adapter: "langchain_openai:ChatOpenAI" },
      { provider_model: "gpt-5.2" },
      { settings: { temperature: 0.2 } },
      { credential_id: "33333333-3333-4333-8333-333333333333" },
    ]) {
      expect(
        modelsResponseSchema.safeParse({
          ...response,
          models: [{ ...response.models[0], ...forbidden }],
        }).success,
      ).toBe(false);
    }
    expect(
      modelsResponseSchema.safeParse({
        ...response,
        models: [{ ...response.models[0], model: "provider-private-name" }],
      }).success,
    ).toBe(false);
    expect(
      modelsResponseSchema.safeParse({
        ...response,
        token_usage: { ...response.token_usage, billing_secret: "no" },
      }).success,
    ).toBe(false);
  });

  test("uses authenticated fetch, forwards cancellation and checks status", async () => {
    const controller = new AbortController();
    mockedFetch.mockResolvedValueOnce(jsonResponse(200, response));

    await expect(loadModels(controller.signal)).resolves.toEqual(response);
    expect(mockedFetch).toHaveBeenCalledWith("/backend/api/models", {
      signal: controller.signal,
    });

    mockedFetch.mockResolvedValueOnce(jsonResponse(503, { detail: "private" }));
    await expect(loadModels()).rejects.toMatchObject({
      code: "REQUEST_FAILED",
      status: 503,
    });
  });

  test("fails closed on malformed successful responses", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        models: [{ name: "missing-provider-model" }],
        token_usage: { enabled: false },
      }),
    );

    await expect(loadModels()).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });
});
