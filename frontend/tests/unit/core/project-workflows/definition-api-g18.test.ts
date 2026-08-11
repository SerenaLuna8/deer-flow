import { afterEach, describe, expect, it, rs } from "@rstest/core";

import { ProjectWorkflowApiError } from "@/core/project-workflows/api";
import {
  archiveWorkflowDefinition,
  createWorkflowDefinition,
  createWorkflowDefinitionIdempotencyKey,
  deleteWorkflowDraftGrantIntent,
  listWorkflowDefinitions,
  listWorkflowVersions,
  publishWorkflowDraft,
  putWorkflowDraftGrantIntent,
  putWorkflowVersionGrant,
  readWorkflowDefinition,
  readWorkflowDraft,
  readWorkflowVersion,
  revokeWorkflowVersionGrant,
  saveWorkflowDraft,
  validateWorkflowDraft,
} from "@/core/project-workflows/definition-api";
import { workflowDraftSaveRequestV1Schema } from "@/core/project-workflows/definition-contracts";

import publicFixture from "../../../fixtures/workflows/public-projections-v1.json";
import definitionFixture from "../../../fixtures/workflows/workflow-definition-transport-v1.json";

const SCOPE = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const WORKFLOW_ID = definitionFixture.definition.id;
const VERSION_ID = definitionFixture.definition.current_published_version_id;
const IDEMPOTENCY_KEY = "g18-definition-operation-1";
const SIGNAL = new AbortController().signal;

const VERSION = {
  id: VERSION_ID,
  workflow_id: WORKFLOW_ID,
  version_number: 1,
  graph_schema_version: 1,
  canvas_schema_version: 1,
  compiler_contract_version: 1,
  semantic_checksum: definitionFixture.validation.semantic_checksum,
  spec: publicFixture.workflow_spec,
  canvas: publicFixture.canvas_document,
  credential_slots: [],
  missing_required_credential_slot_ids: [],
  executable: true,
  published_at: "2026-08-10T00:02:00Z",
};

const VALIDATION = {
  ...definitionFixture.validation,
  requirements: definitionFixture.requirements,
};

const PUBLISH = {
  request_id: "req-g18-publish",
  workflow_id: WORKFLOW_ID,
  version_id: VERSION_ID,
  version_number: 1,
  graph_schema_version: 1,
  canvas_schema_version: 1,
  compiler_contract_version: 1,
  semantic_checksum: VERSION.semantic_checksum,
  spec: VERSION.spec,
  canvas: VERSION.canvas,
  credential_slots: [],
  missing_required_credential_slot_ids: [],
  executable: true,
  published_at: VERSION.published_at,
};

const DRAFT_SAVE = workflowDraftSaveRequestV1Schema.parse({
  expected_revision: definitionFixture.draft.revision,
  spec: definitionFixture.draft.spec,
  canvas: definitionFixture.draft.canvas,
});

const DRAFT_CAS = {
  expected_revision: definitionFixture.draft.revision,
  expected_draft_checksum: definitionFixture.draft.draft_checksum,
};

const GRANT_BODY = {
  credential_id: definitionFixture.grant.credential_id,
  expected_credential_version_id: definitionFixture.grant.credential_version_id,
  expected_slot_schema_checksum:
    definitionFixture.grant.payload_schema_checksum,
};

function mutationOptions() {
  return { signal: SIGNAL, idempotencyKey: IDEMPOTENCY_KEY };
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function requestBody(init: RequestInit | undefined): string {
  if (typeof init?.body !== "string") {
    throw new TypeError("expected a JSON request body");
  }
  return init.body;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("G18 Workflow Definition authenticated API", () => {
  it("lists Definitions with a canonical bounded query and strict page", async () => {
    const fetchMock = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(
          { items: [definitionFixture.definition], next_cursor: "next-page" },
          { status: 200 },
        ),
    );
    rs.stubGlobal("fetch", fetchMock);

    await expect(
      listWorkflowDefinitions(
        SCOPE,
        {
          query: "订单",
          publication: "published",
          sort: "name_asc",
          limit: 25,
        },
        { signal: SIGNAL },
      ),
    ).resolves.toMatchObject({ next_cursor: "next-page" });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe(
      `/api/projects/${SCOPE.projectId}/workflows?query=${encodeURIComponent("订单")}&lifecycle=active&publication=published&sort=name_asc&limit=25`,
    );
    expect(init).toMatchObject({ credentials: "include", signal: SIGNAL });
    if (url !== undefined)
      expect(requestUrl(url)).not.toContain(SCOPE.accountId);
  });

  it("creates, reads and archives a Definition with CSRF, idempotency and CAS", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=g18-csrf" });
    const fetchMock = rs.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        if (
          init?.method === "POST" &&
          requestUrl(input) === `/api/projects/${SCOPE.projectId}/workflows`
        ) {
          return Response.json(definitionFixture.definition, { status: 201 });
        }
        return Response.json(definitionFixture.definition, { status: 200 });
      },
    );
    rs.stubGlobal("fetch", fetchMock);

    await createWorkflowDefinition(
      SCOPE,
      { name: "订单审核", description: "" },
      mutationOptions(),
    );
    await readWorkflowDefinition(SCOPE, WORKFLOW_ID, { signal: SIGNAL });
    await archiveWorkflowDefinition(
      SCOPE,
      WORKFLOW_ID,
      { expected_revision: definitionFixture.definition.revision },
      mutationOptions(),
    );

    const create = fetchMock.mock.calls[0];
    expect(create?.[0]).toBe(`/api/projects/${SCOPE.projectId}/workflows`);
    expect(create?.[1]?.method).toBe("POST");
    expect(JSON.parse(requestBody(create?.[1]))).toEqual({
      name: "订单审核",
      description: "",
    });
    expect(new Headers(create?.[1]?.headers).get("X-CSRF-Token")).toBe(
      "g18-csrf",
    );
    expect(new Headers(create?.[1]?.headers).get("Idempotency-Key")).toBe(
      IDEMPOTENCY_KEY,
    );

    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      `/api/projects/${SCOPE.projectId}/workflows/${WORKFLOW_ID}`,
    );
    const archive = fetchMock.mock.calls[2];
    expect(archive?.[0]).toBe(
      `/api/projects/${SCOPE.projectId}/workflows/${WORKFLOW_ID}/archive`,
    );
    expect(archive?.[1]?.method).toBe("POST");
    expect(JSON.parse(requestBody(archive?.[1]))).toEqual({
      expected_revision: definitionFixture.definition.revision,
    });
  });

  it("loads, CAS-saves, validates and idempotently publishes one Draft", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=g18-draft" });
    const fetchMock = rs.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.endsWith("/draft") && init?.method === undefined) {
          return Response.json(definitionFixture.draft, { status: 200 });
        }
        if (url.endsWith("/draft") && init?.method === "PUT") {
          return Response.json(definitionFixture.draft, { status: 200 });
        }
        if (url.endsWith("/validate")) {
          return Response.json(VALIDATION, { status: 200 });
        }
        return Response.json(PUBLISH, { status: 201 });
      },
    );
    rs.stubGlobal("fetch", fetchMock);

    await readWorkflowDraft(SCOPE, WORKFLOW_ID, { signal: SIGNAL });
    await saveWorkflowDraft(SCOPE, WORKFLOW_ID, DRAFT_SAVE, mutationOptions());
    await validateWorkflowDraft(SCOPE, WORKFLOW_ID, DRAFT_CAS, {
      signal: SIGNAL,
    });
    await publishWorkflowDraft(
      SCOPE,
      WORKFLOW_ID,
      DRAFT_CAS,
      mutationOptions(),
    );

    const save = fetchMock.mock.calls[1];
    expect(save?.[1]?.method).toBe("PUT");
    expect(JSON.parse(requestBody(save?.[1]))).toEqual(DRAFT_SAVE);
    expect(new Headers(save?.[1]?.headers).get("Idempotency-Key")).toBe(
      IDEMPOTENCY_KEY,
    );
    expect(new Headers(save?.[1]?.headers).get("X-CSRF-Token")).toBe(
      "g18-draft",
    );

    const validate = fetchMock.mock.calls[2];
    expect(validate?.[1]?.method).toBe("POST");
    expect(
      new Headers(validate?.[1]?.headers).get("Idempotency-Key"),
    ).toBeNull();
    expect(new Headers(validate?.[1]?.headers).get("X-CSRF-Token")).toBe(
      "g18-draft",
    );

    const publish = fetchMock.mock.calls[3];
    expect(publish?.[0]).toBe(
      `/api/projects/${SCOPE.projectId}/workflows/${WORKFLOW_ID}/publish`,
    );
    expect(publish?.[1]?.method).toBe("POST");
    expect(new Headers(publish?.[1]?.headers).get("Idempotency-Key")).toBe(
      IDEMPOTENCY_KEY,
    );
  });

  it("reads immutable Versions and keeps grant intent/grant mutations outside Draft bodies", async () => {
    rs.stubGlobal("document", { cookie: "csrf_token=g18-grant" });
    const revokedGrant = {
      ...definitionFixture.grant,
      status: "revoked",
      revision: 2,
      revoked_at: "2026-08-10T00:03:00Z",
    };
    const fetchMock = rs.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.endsWith("/versions?limit=20")) {
          return Response.json({ items: [VERSION], next_cursor: null });
        }
        if (url.endsWith(`/versions/${VERSION_ID}`)) {
          return Response.json(VERSION);
        }
        if (url.includes("/draft/credential-grant-intents/")) {
          return Response.json(
            init?.method === "DELETE"
              ? {
                  workflow_id: WORKFLOW_ID,
                  slot_id: "http_auth",
                  deleted: true,
                }
              : definitionFixture.grant_intent,
          );
        }
        return Response.json(
          init?.method === "DELETE" ? revokedGrant : definitionFixture.grant,
        );
      },
    );
    rs.stubGlobal("fetch", fetchMock);

    await listWorkflowVersions(
      SCOPE,
      WORKFLOW_ID,
      { limit: 20 },
      { signal: SIGNAL },
    );
    await readWorkflowVersion(SCOPE, WORKFLOW_ID, VERSION_ID, {
      signal: SIGNAL,
    });
    await putWorkflowDraftGrantIntent(
      SCOPE,
      WORKFLOW_ID,
      "http_auth",
      GRANT_BODY,
      mutationOptions(),
    );
    await deleteWorkflowDraftGrantIntent(
      SCOPE,
      WORKFLOW_ID,
      "http_auth",
      mutationOptions(),
    );
    await putWorkflowVersionGrant(
      SCOPE,
      WORKFLOW_ID,
      VERSION_ID,
      "http_auth",
      GRANT_BODY,
      mutationOptions(),
    );
    await revokeWorkflowVersionGrant(
      SCOPE,
      WORKFLOW_ID,
      VERSION_ID,
      "http_auth",
      mutationOptions(),
    );

    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      `/api/projects/${SCOPE.projectId}/workflows/${WORKFLOW_ID}/draft/credential-grant-intents/http_auth`,
    );
    expect(JSON.parse(requestBody(fetchMock.mock.calls[2]?.[1]))).toEqual(
      GRANT_BODY,
    );
    expect(fetchMock.mock.calls[3]?.[1]?.body).toBeUndefined();
    expect(fetchMock.mock.calls[4]?.[0]).toBe(
      `/api/projects/${SCOPE.projectId}/workflows/${WORKFLOW_ID}/versions/${VERSION_ID}/credential-grants/http_auth`,
    );
    expect(fetchMock.mock.calls[5]?.[1]?.method).toBe("DELETE");
    for (const call of fetchMock.mock.calls.slice(2)) {
      expect(new Headers(call[1]?.headers).get("Idempotency-Key")).toBe(
        IDEMPOTENCY_KEY,
      );
      expect(new Headers(call[1]?.headers).get("X-CSRF-Token")).toBe(
        "g18-grant",
      );
    }
  });

  it.each(["_Auth", "Auth", "foo:bar"])(
    "accepts Definition slot ID %s in grant paths",
    async (slotId) => {
      const fetchMock = rs.fn(
        async (_input: RequestInfo | URL, _init?: RequestInit) =>
          Response.json({
            ...definitionFixture.grant_intent,
            slot_id: slotId,
          }),
      );
      rs.stubGlobal("fetch", fetchMock);

      await expect(
        putWorkflowDraftGrantIntent(
          SCOPE,
          WORKFLOW_ID,
          slotId,
          GRANT_BODY,
          mutationOptions(),
        ),
      ).resolves.toMatchObject({ slot_id: slotId });

      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0]?.[0]).toBe(
        `/api/projects/${SCOPE.projectId}/workflows/${WORKFLOW_ID}/draft/credential-grant-intents/${encodeURIComponent(slotId)}`,
      );
    },
  );

  it.each([
    "1Auth",
    "-Auth",
    ".Auth",
    ":Auth",
    "Auth!",
    "凭据",
    "A".repeat(129),
  ])("rejects non-Definition slot ID %s before fetch", async (slotId) => {
    const fetchMock = rs.fn();
    rs.stubGlobal("fetch", fetchMock);

    await expect(
      putWorkflowDraftGrantIntent(
        SCOPE,
        WORKFLOW_ID,
        slotId,
        GRANT_BODY,
        mutationOptions(),
      ),
    ).rejects.toMatchObject({
      status: 422,
      code: "WORKFLOW_INPUT_INVALID",
      message: "Workflow input is invalid.",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fails before fetch for non-canonical paths, bodies, queries and idempotency keys", async () => {
    const fetchMock = rs.fn();
    rs.stubGlobal("fetch", fetchMock);

    const attempts = [
      readWorkflowDefinition(SCOPE, "NOT-A-UUID", { signal: SIGNAL }),
      listWorkflowDefinitions(SCOPE, { query: " padded " }, { signal: SIGNAL }),
      saveWorkflowDraft(
        SCOPE,
        WORKFLOW_ID,
        { ...DRAFT_SAVE, owner_id: "server-owned" } as never,
        mutationOptions(),
      ),
      putWorkflowDraftGrantIntent(
        SCOPE,
        WORKFLOW_ID,
        "HTTP AUTH",
        GRANT_BODY,
        mutationOptions(),
      ),
      createWorkflowDefinition(
        SCOPE,
        { name: "订单审核", description: "" },
        { signal: SIGNAL, idempotencyKey: "contains space" },
      ),
    ];

    for (const attempt of attempts) {
      await expect(attempt).rejects.toMatchObject({
        status: 422,
        code: "WORKFLOW_INPUT_INVALID",
        message: "Workflow input is invalid.",
      });
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("enforces exact success/error status and strips untrusted server details", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        Response.json(
          { ...definitionFixture.definition, owner_id: "private-owner" },
          { status: 200 },
        ),
      ),
    );
    const invalidSuccess = await readWorkflowDefinition(SCOPE, WORKFLOW_ID, {
      signal: SIGNAL,
    }).catch((reason: unknown) => reason);
    expect(invalidSuccess).toBeInstanceOf(ProjectWorkflowApiError);
    expect(invalidSuccess).toMatchObject({
      status: 200,
      code: "WORKFLOW_RESPONSE_INVALID",
      message: "Workflow response was invalid.",
    });
    expect(String(invalidSuccess)).not.toContain("private-owner");

    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        Response.json(
          {
            detail: {
              code: "WORKFLOW_DRAFT_CONFLICT",
              message: "postgresql://secret.internal/workflows",
              request_id: "req-g18-conflict",
            },
          },
          { status: 409 },
        ),
      ),
    );
    const conflict = await saveWorkflowDraft(
      SCOPE,
      WORKFLOW_ID,
      DRAFT_SAVE,
      mutationOptions(),
    ).catch((reason: unknown) => reason);
    expect(conflict).toMatchObject({
      status: 409,
      code: "WORKFLOW_DRAFT_CONFLICT",
      message: "Workflow draft conflict.",
    });
    expect(String(conflict)).not.toContain("secret.internal");

    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        Response.json(
          {
            detail: {
              code: "WORKFLOW_DRAFT_CONFLICT",
              message: "private",
              request_id: "req-g18-mismatch",
            },
          },
          { status: 422 },
        ),
      ),
    );
    await expect(
      saveWorkflowDraft(SCOPE, WORKFLOW_ID, DRAFT_SAVE, mutationOptions()),
    ).rejects.toMatchObject({
      status: 422,
      code: "WORKFLOW_RESPONSE_INVALID",
      message: "Workflow request failed.",
    });
  });

  it("preserves abort and maps authentication/network failures without leaking causes", async () => {
    const aborted = new Error("aborted");
    aborted.name = "AbortError";
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => {
        throw aborted;
      }),
    );
    await expect(
      readWorkflowDraft(SCOPE, WORKFLOW_ID, { signal: SIGNAL }),
    ).rejects.toBe(aborted);

    rs.stubGlobal(
      "fetch",
      rs.fn(async () => new Response(null, { status: 401 })),
    );
    await expect(
      readWorkflowDraft(SCOPE, WORKFLOW_ID, { signal: SIGNAL }),
    ).rejects.toMatchObject({ status: 401, code: "AUTH_REQUIRED" });

    rs.stubGlobal(
      "fetch",
      rs.fn(async () => {
        throw new Error("dns secret.internal");
      }),
    );
    const network = await readWorkflowDraft(SCOPE, WORKFLOW_ID, {
      signal: SIGNAL,
    }).catch((reason: unknown) => reason);
    expect(network).toMatchObject({
      status: 0,
      code: "WORKFLOW_NETWORK_ERROR",
      message: "Workflow service is temporarily unavailable.",
    });
    expect(String(network)).not.toContain("secret.internal");
  });

  it("creates a closed printable UUID idempotency key", () => {
    const key = createWorkflowDefinitionIdempotencyKey();
    expect(key).toMatch(/^[!-~]{1,255}$/);
    expect(key).not.toContain(" ");
  });
});
