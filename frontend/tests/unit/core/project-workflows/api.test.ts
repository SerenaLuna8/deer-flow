import { afterEach, describe, expect, it, rs } from "@rstest/core";

import {
  ProjectWorkflowApiError,
  readProjectWorkflowNodeCatalog,
  readProjectWorkflowReadiness,
} from "@/core/project-workflows/api";
import { workflowNodeRegistryV1 } from "@/core/project-workflows/catalog";

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};

const READY = {
  status: "ready",
  code: "WORKFLOW_CONTROL_PLANE_READY",
  workflow_enabled: true,
  schema_ready: true,
  admission_ready: false,
  request_id: "req-g16-api",
} as const;

const NODE_CATALOG = {
  schema_version: 1,
  catalog_generation: "a".repeat(64),
  availability_generation: "b".repeat(64),
  entries: workflowNodeRegistryV1.map((definition) => ({
    definition,
    availability: { state: "enabled" as const },
    ...(definition.type === "http_request"
      ? {
          http_authoring: {
            endpoints: [
              {
                id: "public-api",
                origin: "https://api.example.com",
                allowed_methods: ["GET", "POST"],
                write_idempotency: "server_derived_key",
                injection_profiles: [
                  {
                    id: "api-key-v1",
                    scheme: "api_key",
                    target_header: "x-api-key",
                    credential_payload_contract: "api_key_v1",
                  },
                ],
              },
            ],
          },
        }
      : {}),
  })),
} as const;

const CLOSED_SERVER_ERRORS = [
  ["WORKFLOW_NOT_FOUND", 404, "Workflow was not found."],
  ["WORKFLOW_FORBIDDEN", 403, "Workflow action is forbidden."],
  ["WORKFLOW_DRAFT_CONFLICT", 409, "Workflow draft conflict."],
  ["WORKFLOW_DRAFT_INVALID", 422, "Workflow draft is invalid."],
  [
    "WORKFLOW_VERSION_NOT_EXECUTABLE",
    409,
    "Workflow version is not executable.",
  ],
  ["WORKFLOW_RUN_CONFLICT", 409, "Workflow Run conflict."],
  ["WORKFLOW_RUN_NOT_RESUMABLE", 409, "Workflow Run is not resumable."],
  ["WORKFLOW_RUN_RETRY_FORBIDDEN", 409, "Workflow Run cannot be retried."],
  ["WORKFLOW_INPUT_INVALID", 422, "Workflow input is invalid."],
  ["WORKFLOW_OUTPUT_INVALID", 422, "Workflow output is invalid."],
  ["SIDE_EFFECT_STATE_UNKNOWN", 409, "Workflow side-effect state is unknown."],
  ["WORKFLOW_UNAVAILABLE", 503, "Workflow is temporarily unavailable."],
] as const;

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("authenticated Project Workflow readiness API", () => {
  it("uses only the project UUID in the path and forwards the exact signal", async () => {
    const signal = new AbortController().signal;
    const fetchMock = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(READY, { status: 200 }),
    );
    rs.stubGlobal("fetch", fetchMock);

    await expect(
      readProjectWorkflowReadiness(SCOPE, { signal }),
    ).resolves.toEqual(READY);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `/api/projects/${SCOPE.projectId}/workflows/readiness`,
    );
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      credentials: "include",
      signal,
    });
    const calledInput = fetchMock.mock.calls[0]?.[0];
    expect(typeof calledInput).toBe("string");
    if (typeof calledInput === "string") {
      expect(calledInput).not.toContain(SCOPE.accountId);
    }
  });

  it("rejects contradictory or private success payloads with a content-free error", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        Response.json(
          { ...READY, provider_id: "private-provider", raw_error: "secret" },
          { status: 200 },
        ),
      ),
    );

    const error = await readProjectWorkflowReadiness(SCOPE, {
      signal: new AbortController().signal,
    }).catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(ProjectWorkflowApiError);
    expect(error).toMatchObject({
      status: 200,
      code: "WORKFLOW_RESPONSE_INVALID",
      message: "Workflow response was invalid.",
    });
    expect(String(error)).not.toContain("private-provider");
    expect(String(error)).not.toContain("secret");
  });

  it("maps every closed server error/status pair to fixed safe copy", async () => {
    for (const [code, status, safeMessage] of CLOSED_SERVER_ERRORS) {
      rs.stubGlobal(
        "fetch",
        rs.fn(async () =>
          Response.json(
            {
              detail: {
                code,
                message: "postgresql://private-host/secret",
                request_id: `req-${code}`,
              },
            },
            { status },
          ),
        ),
      );

      const error = await readProjectWorkflowReadiness(SCOPE, {
        signal: new AbortController().signal,
      }).catch((reason: unknown) => reason);
      expect(error).toMatchObject({ status, code, message: safeMessage });
      expect(String(error)).not.toContain("private-host");
    }
  });

  it("rejects a valid error code paired with the wrong HTTP status", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        Response.json(
          {
            detail: {
              code: "WORKFLOW_NOT_FOUND",
              message: "private database locator",
              request_id: "req-status-mismatch",
            },
          },
          { status: 503 },
        ),
      ),
    );

    const error = await readProjectWorkflowReadiness(SCOPE, {
      signal: new AbortController().signal,
    }).catch((reason: unknown) => reason);
    expect(error).toMatchObject({
      status: 503,
      code: "WORKFLOW_RESPONSE_INVALID",
      message: "Workflow request failed.",
    });
    expect(String(error)).not.toContain("database locator");
  });

  it("maps malformed errors and network failures without leaking their causes", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(
        async () =>
          new Response("proxy locator: internal.example", {
            status: 502,
            headers: { "content-type": "text/plain" },
          }),
      ),
    );
    const malformed = await readProjectWorkflowReadiness(SCOPE, {
      signal: new AbortController().signal,
    }).catch((reason: unknown) => reason);
    expect(malformed).toMatchObject({
      status: 502,
      code: "WORKFLOW_RESPONSE_INVALID",
      message: "Workflow request failed.",
    });
    expect(String(malformed)).not.toContain("internal.example");

    rs.stubGlobal(
      "fetch",
      rs.fn(async () => {
        throw new Error("dns secret.internal");
      }),
    );
    const network = await readProjectWorkflowReadiness(SCOPE, {
      signal: new AbortController().signal,
    }).catch((reason: unknown) => reason);
    expect(network).toMatchObject({
      status: 0,
      code: "WORKFLOW_NETWORK_ERROR",
      message: "Workflow service is temporarily unavailable.",
    });
    expect(String(network)).not.toContain("secret.internal");
  });

  it("preserves AbortError instead of relabeling teardown as an availability failure", async () => {
    const abortError = new Error("aborted");
    abortError.name = "AbortError";
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => {
        throw abortError;
      }),
    );

    await expect(
      readProjectWorkflowReadiness(SCOPE, {
        signal: new AbortController().signal,
      }),
    ).rejects.toBe(abortError);
  });

  it("fails closed for invalid scope and authentication without issuing an unsafe request", async () => {
    const fetchMock = rs.fn(async () => new Response(null, { status: 401 }));
    rs.stubGlobal("fetch", fetchMock);

    const invalidScope = await readProjectWorkflowReadiness(
      { ...SCOPE, projectId: "not-a-project" },
      { signal: new AbortController().signal },
    ).catch((reason: unknown) => reason);
    expect(invalidScope).toMatchObject({
      status: 422,
      code: "WORKFLOW_INPUT_INVALID",
      message: "Workflow input is invalid.",
    });
    expect(fetchMock).not.toHaveBeenCalled();

    const auth = await readProjectWorkflowReadiness(SCOPE, {
      signal: new AbortController().signal,
    }).catch((reason: unknown) => reason);
    expect(auth).toMatchObject({
      status: 401,
      code: "AUTH_REQUIRED",
      message: "Authentication required.",
    });
  });
});

describe("authenticated Project Workflow Node Catalog API", () => {
  it("reads the exact project Catalog with the caller AbortSignal", async () => {
    const signal = new AbortController().signal;
    const fetchMock = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(NODE_CATALOG),
    );
    rs.stubGlobal("fetch", fetchMock);

    await expect(
      readProjectWorkflowNodeCatalog(SCOPE, { signal }),
    ).resolves.toEqual(NODE_CATALOG);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `/api/projects/${SCOPE.projectId}/workflows/node-catalog`,
    );
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      credentials: "include",
      signal,
    });
  });

  it("rejects a private or contradictory Catalog projection", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        Response.json({ ...NODE_CATALOG, worker_id: "private-worker" }),
      ),
    );

    const error = await readProjectWorkflowNodeCatalog(SCOPE, {
      signal: new AbortController().signal,
    }).catch((reason: unknown) => reason);
    expect(error).toMatchObject({
      status: 200,
      code: "WORKFLOW_RESPONSE_INVALID",
      message: "Workflow response was invalid.",
    });
    expect(String(error)).not.toContain("private-worker");
  });
});
