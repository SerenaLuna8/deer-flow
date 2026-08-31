import { afterEach, describe, expect, rs, test } from "@rstest/core";
import { QueryClient } from "@tanstack/react-query";

import {
  abortAdminKnowledgeSettingsAccount,
  fetchAdminKnowledgeSettings,
  replaceAdminKnowledgeSettings,
} from "@/core/admin-settings/knowledge/api";
import {
  adminKnowledgeSettingsQueryOptions,
  saveAdminKnowledgeSettings,
} from "@/core/admin-settings/knowledge/hooks";
import { adminKnowledgeSettingsRoot } from "@/core/admin-settings/knowledge/query-keys";
import {
  adminKnowledgeSettingsSchema,
  adminKnowledgeSettingsUpdateSchema,
} from "@/core/admin-settings/knowledge/types";

const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const MODEL_ID = "22222222-2222-4222-8222-222222222222";
const SECRET = "fictional-minio-secret-for-contract-test";
const fields = {
  enabled: false,
  worker_concurrency: 2,
  task_timeout_seconds: 900,
  upload_max_bytes: 10_485_760,
  max_knowledge_bases_per_project: 20,
  max_documents_per_knowledge_base: 100,
  max_segments_per_document: 1000,
  minio_endpoint: null,
  minio_bucket: null,
  minio_access_key: null,
  minio_secure: false,
  summary_model_name: null,
  query_cache_enabled: true,
  query_cache_max_entries: 256,
  query_cache_ttl_seconds: 300,
};
const settings = {
  ...fields,
  revision: 1,
  updated_at: "2026-08-31T12:00:00Z",
  secret_key_configured: false,
  summary_model: null,
  request_id: "knowledge-settings-test",
};

afterEach(() => {
  abortAdminKnowledgeSettingsAccount(ACCOUNT_ID);
  rs.unstubAllGlobals();
});

describe("admin knowledge settings boundary", () => {
  test("accepts safe GET metadata and nullable unresolved model references", () => {
    expect(adminKnowledgeSettingsSchema.parse(settings)).toEqual(settings);
    expect(
      adminKnowledgeSettingsSchema.parse({
        ...settings,
        summary_model_name: "historical-unavailable-model",
      }).summary_model,
    ).toBeNull();
    expect(
      adminKnowledgeSettingsSchema.parse({
        ...settings,
        summary_model_name: MODEL_ID,
        summary_model: { model_name: MODEL_ID, display_name: "Summary model" },
      }).summary_model?.model_name,
    ).toBe(MODEL_ID);
  });

  test("rejects secret echoes and unknown GET fields before caching", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        Response.json({
          ...settings,
          minio_secret_key: SECRET,
        }),
      ),
    );
    await expect(fetchAdminKnowledgeSettings(ACCOUNT_ID)).rejects.toMatchObject(
      {
        code: "INVALID_RESPONSE",
      },
    );
    expect(
      adminKnowledgeSettingsSchema.safeParse({ ...settings, id: 1 }).success,
    ).toBe(false);
  });

  test("validates the bounded write contract and rejects blank keys", () => {
    const input = { ...fields, expected_revision: 1 };
    expect(adminKnowledgeSettingsUpdateSchema.safeParse(input).success).toBe(
      true,
    );
    expect(
      adminKnowledgeSettingsUpdateSchema.safeParse({
        ...input,
        minio_endpoint: "storage.example.test:80",
      }).success,
    ).toBe(true);
    expect(
      adminKnowledgeSettingsUpdateSchema.safeParse({
        ...input,
        minio_secret_key: null,
      }).success,
    ).toBe(true);
    for (const invalid of [
      { worker_concurrency: 17 },
      { task_timeout_seconds: 29 },
      { upload_max_bytes: 52_428_801 },
      { max_segments_per_document: 5001 },
      { query_cache_max_entries: 15 },
      { query_cache_ttl_seconds: 4 },
      { expected_revision: 0 },
      { summary_model_name: "not-a-model-id" },
      { minio_secret_key: "" },
      { minio_secret_key: "   " },
      { minio_endpoint: "https://storage.example.test:9000" },
    ]) {
      expect(
        adminKnowledgeSettingsUpdateSchema.safeParse({ ...input, ...invalid })
          .success,
      ).toBe(false);
    }
  });

  test("forwards abort signals through the account-scoped query", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(settings),
    );
    rs.stubGlobal("fetch", fetcher);
    const controller = new AbortController();
    const query = adminKnowledgeSettingsQueryOptions(ACCOUNT_ID);
    expect(query.queryKey).toEqual([
      "account",
      ACCOUNT_ID,
      "admin",
      "settings",
      "knowledge",
    ]);
    await query.queryFn({ signal: controller.signal });
    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({
      signal: controller.signal,
      credentials: "include",
    });
  });

  test("uses authenticated PUT while keeping the secret out of both query and mutation caches", async () => {
    const response = { ...settings, revision: 2, secret_key_configured: true };
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json(response),
    );
    rs.stubGlobal("fetch", fetcher);
    rs.stubGlobal("document", { cookie: "csrf_token=contract-csrf" });
    const client = new QueryClient();
    client.setQueryData(adminKnowledgeSettingsRoot(ACCOUNT_ID), settings);
    client.setQueryData(["unrelated"], { marker: true });
    try {
      await saveAdminKnowledgeSettings(client, ACCOUNT_ID, {
        ...fields,
        expected_revision: 1,
        minio_secret_key: SECRET,
      });
      const request = fetcher.mock.calls[0]![1]!;
      expect(request.method).toBe("PUT");
      expect(request.credentials).toBe("include");
      expect(new Headers(request.headers).get("X-CSRF-Token")).toBe(
        "contract-csrf",
      );
      expect(JSON.parse(request.body as string).minio_secret_key).toBe(SECRET);
      expect(
        client.getQueryData(adminKnowledgeSettingsRoot(ACCOUNT_ID)),
      ).toEqual(response);
      expect(
        client.getQueryState(adminKnowledgeSettingsRoot(ACCOUNT_ID))
          ?.isInvalidated,
      ).toBe(true);
      expect(client.getQueryState(["unrelated"])?.isInvalidated).toBe(false);
      expect(client.getMutationCache().getAll()).toHaveLength(0);
      expect(
        JSON.stringify(
          client
            .getQueryCache()
            .getAll()
            .map((query) => query.state),
        ),
      ).not.toContain(SECRET);
    } finally {
      client.clear();
    }
  });

  test("cancels an in-flight account write and rejects its late response", async () => {
    let finish!: (response: Response) => void;
    rs.stubGlobal(
      "fetch",
      rs.fn(
        () =>
          new Promise<Response>((resolve) => {
            finish = resolve;
          }),
      ),
    );
    const client = new QueryClient();
    const pending = saveAdminKnowledgeSettings(client, ACCOUNT_ID, {
      ...fields,
      expected_revision: 1,
    });
    abortAdminKnowledgeSettingsAccount(ACCOUNT_ID);
    finish(Response.json({ ...settings, revision: 2 }));
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    client.clear();
  });

  test("does not resurrect account data when identity changes during cache cancellation", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => Response.json({ ...settings, revision: 2 })),
    );
    const client = new QueryClient();
    rs.spyOn(client, "cancelQueries").mockImplementationOnce(async () => {
      abortAdminKnowledgeSettingsAccount(ACCOUNT_ID);
    });
    await expect(
      saveAdminKnowledgeSettings(client, ACCOUNT_ID, {
        ...fields,
        expected_revision: 1,
      }),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    client.clear();
  });

  test("preserves safe validation text and never reflects unstructured validation payloads", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        Response.json(
          {
            detail: {
              code: "KNOWLEDGE_SETTINGS_INVALID",
              message: "Storage validation failed.",
              request_id: "safe-error",
            },
          },
          { status: 422 },
        ),
      ),
    );
    await expect(
      replaceAdminKnowledgeSettings(ACCOUNT_ID, {
        ...fields,
        expected_revision: 1,
      }),
    ).rejects.toMatchObject({
      status: 422,
      code: "KNOWLEDGE_SETTINGS_INVALID",
      publicMessage: "Storage validation failed.",
    });
    rs.stubGlobal(
      "fetch",
      rs.fn(async () =>
        Response.json({ detail: [{ input: SECRET }] }, { status: 422 }),
      ),
    );
    try {
      await replaceAdminKnowledgeSettings(ACCOUNT_ID, {
        ...fields,
        expected_revision: 1,
      });
      throw new Error("Expected invalid response");
    } catch (error) {
      expect(JSON.stringify(error)).not.toContain(SECRET);
      expect(String(error)).not.toContain(SECRET);
    }
  });
});
