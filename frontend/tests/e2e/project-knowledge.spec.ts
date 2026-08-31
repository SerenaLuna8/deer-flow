import { expect, test, type Page, type Route } from "@playwright/test";

import type { Capability, Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const MODEL_ID = "30000000-0000-4000-8000-000000000001";
const RERANK_MODEL_ID = "30000000-0000-4000-8000-000000000002";
const TIMESTAMP = "2026-08-29T00:00:00Z";

const READ_CAPABILITIES: Capability[] = [
  "project.read",
  "project.enter",
  "shared_assets.read",
];
const EDIT_CAPABILITIES: Capability[] = [
  ...READ_CAPABILITIES,
  "shared_assets.edit",
];

function projectFixture(capabilities: Capability[]): Project {
  return {
    id: PROJECT_ID,
    slug: "alpha",
    display_name: "Alpha Project",
    description: "Knowledge browser acceptance",
    icon: "folder",
    role: "admin",
    capabilities,
    is_pinned: false,
    created_at: "2026-07-01T00:00:00Z",
    last_entered_at: null,
    member_count: 1,
    agent_count: 1,
    skill_count: 0,
    mcp_count: 0,
    quota_summary: {
      members: { used: 1, reserved: 0, limit: 20 },
      storage_bytes: { used: 0, reserved: 0, limit: 5_368_709_120 },
      concurrent_runs: { used: 0, reserved: 0, limit: 3 },
      mcp_calls_daily: { used: 0, reserved: 0, limit: 10_000 },
    },
    status: "active",
    is_suspended: false,
    membership_version: 1,
    request_id: "request-alpha",
  };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function knowledgeError(
  route: Route,
  status: number,
  code: string,
  message: string,
) {
  return json(
    route,
    { detail: { code, message, request_id: "req-err" } },
    status,
  );
}

/** Read one text field out of a multipart/form-data request body. */
function multipartField(body: string, name: string): string | null {
  const pattern = new RegExp(`name="${name}"\\r\\n\\r\\n([^\\r]*)\\r\\n`, "u");
  return pattern.exec(body)?.[1] ?? null;
}

type MockBase = {
  id: string;
  name: string;
  description: string;
  status: "active" | "disabled" | "deleting";
  document_count: number;
  delete_error: string | null;
  /** Omitted seeded bindings stay configured; explicit null is an empty base. */
  embedding_model_id?: string | null;
  default_top_k?: number;
  default_score_threshold?: number;
  reranker_model_id?: string | null;
  retrieval_mode?: "semantic" | "hybrid";
  /** deleting bases disappear after this many further list polls */
  pollsUntilGone?: number;
};

type MockDocument = {
  id: string;
  knowledge_base_id: string;
  name: string;
  original_name: string;
  status:
    | "uploading"
    | "queued"
    | "processing"
    | "ready"
    | "failed"
    | "deleting";
  segment_count: number;
  enabled?: boolean;
  word_count?: number;
  chunk_separator?: string;
  remove_extra_spaces?: boolean;
  remove_urls_emails?: boolean;
  chunking_mode?: "general" | "parent_child";
  child_chunk_size?: number;
  child_chunk_separator?: string;
  doc_metadata?: Record<string, string | number>;
  error_message: string | null;
  delete_error: string | null;
  /** execution generation; reparse bumps it and stale confirmations 409 */
  version?: number;
  /** current-generation indexing task progress rendered by the status cell */
  task_progress?: Record<string, unknown> | null;
  /** statuses the document walks through on subsequent list polls */
  progression?: MockDocument["status"][];
};

type MockMetadataField = {
  id: string;
  knowledge_base_id: string;
  name: string;
  field_type: "string" | "number" | "time";
};

type MockSegment = {
  id: string;
  position: number;
  content: string;
  enabled: boolean;
  source_position: Record<string, unknown>;
};

type MockQuery = {
  id: string;
  query: string;
  source: "agent" | "retrieval_test";
  result_count: number;
  top_score: number | null;
};

function baseView(base: MockBase) {
  return {
    id: base.id,
    project_id: PROJECT_ID,
    name: base.name,
    description: base.description,
    embedding_model_id:
      base.embedding_model_id === undefined
        ? MODEL_ID
        : base.embedding_model_id,
    reranker_model_id: base.reranker_model_id ?? null,
    retrieval_mode: base.retrieval_mode ?? "semantic",
    status: base.status,
    document_count: base.document_count,
    default_top_k: base.default_top_k ?? 4,
    default_score_threshold: base.default_score_threshold ?? 0,
    delete_error: base.delete_error,
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
  };
}

function documentView(document: MockDocument) {
  return {
    id: document.id,
    project_id: PROJECT_ID,
    knowledge_base_id: document.knowledge_base_id,
    name: document.name,
    original_name: document.original_name,
    media_type: "text/plain",
    size_bytes: 2048,
    status: document.status,
    enabled: document.enabled ?? true,
    version: document.version ?? 1,
    chunk_size: 1000,
    chunk_overlap: 100,
    chunk_separator: document.chunk_separator ?? "\\n\\n",
    remove_extra_spaces: document.remove_extra_spaces ?? false,
    remove_urls_emails: document.remove_urls_emails ?? false,
    chunking_mode: document.chunking_mode ?? "general",
    child_chunk_size: document.child_chunk_size ?? 500,
    child_chunk_separator: document.child_chunk_separator ?? "\\n",
    segment_count: document.segment_count,
    // Deliberately not equal to segment_count so exact-text assertions on the
    // segments column never collide with the characters column.
    word_count: document.word_count ?? document.segment_count * 500,
    hit_count: 0,
    doc_metadata: document.doc_metadata ?? {},
    error_message: document.error_message,
    delete_error: document.delete_error,
    task_progress: document.task_progress ?? null,
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
  };
}

function metadataFieldView(field: MockMetadataField) {
  return {
    id: field.id,
    knowledge_base_id: field.knowledge_base_id,
    name: field.name,
    field_type: field.field_type,
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
  };
}

function segmentView(segment: MockSegment) {
  return {
    id: segment.id,
    document_version: 1,
    position: segment.position,
    content: segment.content,
    word_count: segment.content.length,
    enabled: segment.enabled,
    hit_count: 0,
    source_position: segment.source_position,
    created_at: TIMESTAMP,
  };
}

function queryView(item: MockQuery, baseId: string) {
  return {
    id: item.id,
    knowledge_base_ids: [baseId],
    query: item.query,
    source: item.source,
    result_count: item.result_count,
    top_score: item.top_score,
    created_at: TIMESTAMP,
  };
}

function listPayload(items: unknown[]) {
  return {
    items,
    total: items.length,
    page: 1,
    page_size: 100,
    request_id: "req-list",
  };
}

type KnowledgeMockOptions = {
  capabilities?: Capability[];
  featureDisabled?: boolean;
  bases?: MockBase[];
  documents?: MockDocument[];
  /** Keep the create response in flight after the mock server accepted it. */
  createBaseResponseGate?: Promise<void>;
  /** Keep initial model configuration pending after the server accepted it. */
  baseUpdateResponseGate?: Promise<void>;
  /** Keep an upload response in flight after the mock server accepted it. */
  uploadResponseGate?: Promise<void>;
  documentListFailure?: {
    baseId?: string;
    afterRequest: number;
    status: number;
    code: string;
    message: string;
  };
  /**
   * Serve truncated pagination: every documents page after the first comes
   * back empty while total still reports the full count, so the client's
   * completeness check must fail instead of publishing a partial list.
   */
  documentListTruncated?: boolean;
  /** explicit stateful segments per document id (enables segment CRUD) */
  segments?: Record<string, MockSegment[]>;
  /** child chunks served by the segment-detail endpoint, per segment id */
  segmentChildren?: Record<
    string,
    Array<{ id: string; position: number; content: string }>
  >;
  /** seeded query log, newest first; searches prepend to it */
  queries?: MockQuery[];
  /** seeded metadata field definitions */
  metadataFields?: MockMetadataField[];
};

/**
 * Stateful Gateway mock for the knowledge routes. Documents advance along
 * their `progression` on every list poll, which is how the specs observe
 * queued → processing → ready without a real worker.
 */
async function mockKnowledgeRoutes(
  page: Page,
  options: KnowledgeMockOptions = {},
) {
  const routedProject = projectFixture(
    options.capabilities ?? EDIT_CAPABILITIES,
  );
  const state = {
    bases: options.bases ?? [],
    documents: options.documents ?? [],
    segments: new Map(Object.entries(options.segments ?? {})),
    segmentChildren: new Map(Object.entries(options.segmentChildren ?? {})),
    // The digest/version the detail endpoint currently serves; tests bump
    // these to simulate the document changing after a search was scored.
    detailDigest: "d".repeat(64),
    detailVersion: 1,
    queries: options.queries ?? [],
    metadataFields: options.metadataFields ?? [],
    uploadCounter: 0,
    segmentCounter: 0,
    queryCounter: 0,
    fieldCounter: 0,
    documentListRequests: 0,
    modelOptionsRequests: 0,
    documentListFailure: options.documentListFailure ?? null,
    searchRequests: [] as Array<Record<string, unknown>>,
    previewRequests: [] as Array<Record<string, string>>,
    baseCreates: [] as Array<Record<string, unknown>>,
    baseUpdates: [] as Array<Record<string, unknown>>,
    uploadRequests: [] as Array<{
      baseId: string;
      fileName: string;
      displayName: string | null;
    }>,
    baseUpdateFailure: null as null | {
      status: number;
      code: string;
      message: string;
    },
    rebuildRequests: [] as Array<Record<string, unknown>>,
    metadataUpdates: [] as Array<Record<string, unknown>>,
    batchMetadataRequests: [] as Array<{
      document_ids: string[];
      values: Record<string, string | number | null>;
    }>,
    // One-shot failure for the next batch metadata patch (all-or-nothing).
    batchMetadataFailure: null as null | {
      status: number;
      code: string;
      message: string;
    },
    reparsePreviewRequests: [] as Array<Record<string, unknown>>,
    reparseRequests: [] as Array<Record<string, unknown>>,
    // Uploads whose filename contains "reject" fail until a test flips this.
    acceptRejectedUploads: false,
    // A "slowrace" search parks here until the test releases it, so late
    // responses land at a deterministic point in the scenario.
    releaseSlowSearch: null as null | (() => void),
  };

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/auth/me") {
      return json(route, {
        id: ACCOUNT_ID,
        email: "owner@example.test",
        username: "owner",
        system_role: "user",
        needs_setup: false,
        oauth_provider: null,
      });
    }
    if (path === "/api/v1/auth/setup-status") {
      return json(route, { needs_setup: false, registration_enabled: true });
    }
    if (path === "/api/projects" && method === "GET") {
      return json(route, { items: [routedProject], next_cursor: null });
    }
    if (path === `/api/projects/${PROJECT_ID}/enter` && method === "POST") {
      return json(route, routedProject);
    }

    const knowledgeBase = `/api/projects/${PROJECT_ID}/knowledge`;
    if (!path.startsWith(knowledgeBase)) {
      return json(route, { detail: "not found" }, 404);
    }
    if (options.featureDisabled) {
      return knowledgeError(
        route,
        404,
        "KNOWLEDGE_DISABLED",
        "Knowledge 功能未启用",
      );
    }

    if (path === `${knowledgeBase}/health` && method === "GET") {
      return json(route, {
        enabled: true,
        database_ok: true,
        storage_ok: true,
        message: "",
        request_id: "req-health",
      });
    }
    if (path === `${knowledgeBase}/model-options` && method === "GET") {
      state.modelOptionsRequests += 1;
      return json(route, {
        embedding_models: [
          {
            id: MODEL_ID,
            provider_name: "SiliconFlow",
            model_name: "BAAI/bge-m3",
            embedding_dimension: 1024,
          },
        ],
        reranker_models: [
          {
            id: RERANK_MODEL_ID,
            provider_name: "SiliconFlow",
            model_name: "BAAI/bge-reranker-v2-m3",
            embedding_dimension: null,
          },
        ],
        request_id: "req-options",
      });
    }

    if (path === `${knowledgeBase}/bases` && method === "GET") {
      for (const base of [...state.bases]) {
        if (base.status === "deleting" && !base.delete_error) {
          base.pollsUntilGone = (base.pollsUntilGone ?? 1) - 1;
          if (base.pollsUntilGone < 0) {
            state.bases = state.bases.filter((item) => item.id !== base.id);
          }
        }
      }
      return json(route, listPayload(state.bases.map(baseView)));
    }
    if (path === `${knowledgeBase}/bases` && method === "POST") {
      const body = request.postDataJSON() as {
        name: string;
        description?: string;
        embedding_model_id?: string;
        retrieval_mode?: "semantic" | "hybrid";
        reranker_model_id?: string;
      };
      state.baseCreates.push(body);
      const created: MockBase = {
        id: `40000000-0000-4000-8000-00000000000${state.bases.length + 1}`,
        name: body.name,
        description: body.description ?? "",
        embedding_model_id: body.embedding_model_id ?? null,
        status: "active",
        document_count: 0,
        delete_error: null,
        retrieval_mode: body.retrieval_mode ?? "semantic",
        reranker_model_id: body.reranker_model_id ?? null,
      };
      state.bases.push(created);
      await options.createBaseResponseGate;
      return json(route, { item: baseView(created), request_id: "req-create" });
    }

    const baseMatch = /\/bases\/([0-9a-f-]{36})$/u.exec(path);
    if (baseMatch && method === "PATCH") {
      const base = state.bases.find((item) => item.id === baseMatch[1]);
      if (!base)
        return knowledgeError(
          route,
          404,
          "KNOWLEDGE_NOT_FOUND",
          "知识库不存在",
        );
      const body = request.postDataJSON() as {
        name?: string;
        description?: string;
        status?: "active" | "disabled";
        default_top_k?: number;
        default_score_threshold?: number;
        embedding_model_id?: string;
        retrieval_mode?: "semantic" | "hybrid";
        reranker_model_id?: string;
        clear_reranker_model?: boolean;
      };
      state.baseUpdates.push(body);
      if (state.baseUpdateFailure) {
        const failure = state.baseUpdateFailure;
        state.baseUpdateFailure = null;
        return knowledgeError(
          route,
          failure.status,
          failure.code,
          failure.message,
        );
      }
      if (body.embedding_model_id !== undefined) {
        base.embedding_model_id = body.embedding_model_id;
      }
      if (body.name !== undefined) base.name = body.name;
      if (body.description !== undefined) base.description = body.description;
      if (body.status !== undefined) base.status = body.status;
      if (body.retrieval_mode !== undefined)
        base.retrieval_mode = body.retrieval_mode;
      if (body.default_top_k !== undefined) {
        base.default_top_k = body.default_top_k;
      }
      if (body.default_score_threshold !== undefined) {
        base.default_score_threshold = body.default_score_threshold;
      }
      if (body.clear_reranker_model) {
        base.reranker_model_id = null;
      } else if (body.reranker_model_id !== undefined) {
        base.reranker_model_id = body.reranker_model_id;
      }
      await options.baseUpdateResponseGate;
      return json(route, { item: baseView(base), request_id: "req-update" });
    }
    if (baseMatch && method === "DELETE") {
      const base = state.bases.find((item) => item.id === baseMatch[1]);
      if (!base)
        return knowledgeError(
          route,
          404,
          "KNOWLEDGE_NOT_FOUND",
          "知识库不存在",
        );
      // Re-deleting a parked base clears the error and lets it finish.
      base.delete_error =
        base.delete_error === null && base.name.includes("stuck")
          ? "MinIO 对象删除失败"
          : null;
      base.status = "deleting";
      base.pollsUntilGone = base.delete_error ? undefined : 1;
      return json(route, { item: baseView(base), request_id: "req-delete" });
    }

    const queriesMatch = /\/bases\/([0-9a-f-]{36})\/queries$/u.exec(path);
    if (queriesMatch && method === "GET") {
      const pageNumber = Number.parseInt(
        url.searchParams.get("page") ?? "1",
        10,
      );
      const pageSize = Number.parseInt(
        url.searchParams.get("page_size") ?? "10",
        10,
      );
      const start = (pageNumber - 1) * pageSize;
      return json(route, {
        items: state.queries
          .slice(start, start + pageSize)
          .map((item) => queryView(item, queriesMatch[1]!)),
        total: state.queries.length,
        page: pageNumber,
        page_size: pageSize,
        request_id: "req-queries",
      });
    }

    const fieldsMatch = /\/bases\/([0-9a-f-]{36})\/metadata-fields$/u.exec(
      path,
    );
    if (fieldsMatch && method === "GET") {
      return json(route, {
        items: state.metadataFields
          .filter((item) => item.knowledge_base_id === fieldsMatch[1])
          .map(metadataFieldView),
        request_id: "req-fields",
      });
    }
    if (fieldsMatch && method === "POST") {
      const body = request.postDataJSON() as {
        name: string;
        field_type: MockMetadataField["field_type"];
      };
      const duplicate = state.metadataFields.some(
        (item) =>
          item.knowledge_base_id === fieldsMatch[1] && item.name === body.name,
      );
      if (duplicate) {
        return knowledgeError(
          route,
          409,
          "KNOWLEDGE_NAME_CONFLICT",
          "同一 Knowledge Base 内已存在同名元数据字段",
        );
      }
      state.fieldCounter += 1;
      const created: MockMetadataField = {
        id: `80000000-0000-4000-8000-00000000000${state.fieldCounter}`,
        knowledge_base_id: fieldsMatch[1]!,
        name: body.name,
        field_type: body.field_type,
      };
      state.metadataFields.push(created);
      return json(route, {
        item: metadataFieldView(created),
        request_id: "req-field-create",
      });
    }

    const fieldMatch = /\/metadata-fields\/([0-9a-f-]{36})$/u.exec(path);
    if (fieldMatch && (method === "PATCH" || method === "DELETE")) {
      const field = state.metadataFields.find(
        (item) => item.id === fieldMatch[1],
      );
      if (!field)
        return knowledgeError(
          route,
          404,
          "KNOWLEDGE_NOT_FOUND",
          "元数据字段不存在",
        );
      const owned = state.documents.filter(
        (item) => item.knowledge_base_id === field.knowledge_base_id,
      );
      if (method === "PATCH") {
        const body = request.postDataJSON() as { name: string };
        for (const item of owned) {
          const value = item.doc_metadata?.[field.name];
          if (item.doc_metadata && value !== undefined) {
            delete item.doc_metadata[field.name];
            item.doc_metadata[body.name] = value;
          }
        }
        field.name = body.name;
        return json(route, {
          item: metadataFieldView(field),
          request_id: "req-field-rename",
        });
      }
      for (const item of owned) {
        if (item.doc_metadata) delete item.doc_metadata[field.name];
      }
      state.metadataFields = state.metadataFields.filter(
        (item) => item.id !== field.id,
      );
      return json(route, { request_id: "req-field-delete" });
    }

    const rebuildMatch = /\/bases\/([0-9a-f-]{36})\/rebuild$/u.exec(path);
    if (rebuildMatch && method === "POST") {
      const base = state.bases.find((item) => item.id === rebuildMatch[1]);
      if (!base)
        return knowledgeError(
          route,
          404,
          "KNOWLEDGE_NOT_FOUND",
          "知识库不存在",
        );
      state.rebuildRequests.push(
        request.postDataJSON() as Record<string, unknown>,
      );
      let acceptedCount = 0;
      const skippedIds: string[] = [];
      for (const item of state.documents) {
        if (item.knowledge_base_id !== base.id) continue;
        // Never-published failed documents stay failed: re-embedding has no
        // current content to work on, re-parsing is a separate action.
        if (item.status === "failed" && item.segment_count === 0) {
          skippedIds.push(item.id);
          continue;
        }
        item.status = "queued";
        item.progression = ["processing", "ready"];
        acceptedCount += 1;
      }
      return json(route, {
        item: baseView(base),
        accepted_document_count: acceptedCount,
        skipped_document_ids: skippedIds,
        request_id: "req-rebuild",
      });
    }

    const batchMetadataMatch =
      /\/bases\/([0-9a-f-]{36})\/documents\/metadata$/u.exec(path);
    if (batchMetadataMatch && method === "PATCH") {
      const body = request.postDataJSON() as {
        document_ids: string[];
        values: Record<string, string | number | null>;
      };
      state.batchMetadataRequests.push(body);
      if (state.batchMetadataFailure) {
        const failure = state.batchMetadataFailure;
        state.batchMetadataFailure = null;
        return knowledgeError(
          route,
          failure.status,
          failure.code,
          failure.message,
        );
      }
      const targets: MockDocument[] = [];
      for (const documentId of body.document_ids) {
        const target = state.documents.find((item) => item.id === documentId);
        // All-or-nothing: one missing document rejects the whole batch.
        if (!target || target.knowledge_base_id !== batchMetadataMatch[1]) {
          return knowledgeError(
            route,
            404,
            "KNOWLEDGE_NOT_FOUND",
            "文档不存在",
          );
        }
        targets.push(target);
      }
      for (const target of targets) {
        const merged = { ...(target.doc_metadata ?? {}) };
        for (const [name, value] of Object.entries(body.values)) {
          if (value === null) delete merged[name];
          else merged[name] = value;
        }
        target.doc_metadata = merged;
      }
      return json(route, {
        items: targets.map(documentView),
        request_id: "req-batch-metadata",
      });
    }

    const reparsePreviewMatch =
      /\/documents\/([0-9a-f-]{36})\/reparse-preview$/u.exec(path);
    if (reparsePreviewMatch && method === "POST") {
      const target = state.documents.find(
        (item) => item.id === reparsePreviewMatch[1],
      );
      if (!target)
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "文档不存在");
      const body = request.postDataJSON() as Record<string, unknown>;
      state.reparsePreviewRequests.push(body);
      // The real preview re-reads the version after computing: a stale
      // expected_version never returns a preview, it conflicts.
      if (body.expected_version !== (target.version ?? 1)) {
        return knowledgeError(
          route,
          409,
          "KNOWLEDGE_CONFLICT",
          "文档已被其他操作修改",
        );
      }
      const childContents =
        body.chunking_mode === "parent_child" ? ["子块甲", "子块乙"] : [];
      return json(route, {
        document_version: target.version ?? 1,
        items: [
          {
            position: 1,
            content: `重解析预览首段 · chunk_size=${String(body.chunk_size)}`,
            word_count: 42,
            child_contents: childContents,
          },
          {
            position: 2,
            content: "重解析预览次段",
            word_count: 36,
            child_contents: childContents,
          },
        ],
        total: 5,
        request_id: "req-reparse-preview",
      });
    }

    const reparseMatch = /\/documents\/([0-9a-f-]{36})\/reparse$/u.exec(path);
    if (reparseMatch && method === "POST") {
      const target = state.documents.find(
        (item) => item.id === reparseMatch[1],
      );
      if (!target)
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "文档不存在");
      const body = request.postDataJSON() as {
        expected_version: number;
      } & Record<string, unknown>;
      state.reparseRequests.push(body);
      if (body.expected_version !== (target.version ?? 1)) {
        return knowledgeError(
          route,
          409,
          "KNOWLEDGE_CONFLICT",
          "文档已被其他操作修改",
        );
      }
      target.version = (target.version ?? 1) + 1;
      target.status = "queued";
      target.error_message = null;
      target.progression = ["processing", "ready"];
      return json(route, {
        item: documentView(target),
        request_id: "req-reparse",
      });
    }

    const documentMetadataMatch =
      /\/documents\/([0-9a-f-]{36})\/metadata$/u.exec(path);
    if (documentMetadataMatch && method === "PATCH") {
      const target = state.documents.find(
        (item) => item.id === documentMetadataMatch[1],
      );
      if (!target)
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "文档不存在");
      const body = request.postDataJSON() as {
        values: Record<string, string | number | null>;
      };
      state.metadataUpdates.push(body);
      const merged = { ...(target.doc_metadata ?? {}) };
      for (const [name, value] of Object.entries(body.values)) {
        if (value === null) delete merged[name];
        else merged[name] = value;
      }
      target.doc_metadata = merged;
      return json(route, {
        item: documentView(target),
        request_id: "req-doc-metadata",
      });
    }

    const documentsMatch = /\/bases\/([0-9a-f-]{36})\/documents$/u.exec(path);
    if (documentsMatch && method === "GET") {
      state.documentListRequests += 1;
      if (
        state.documentListFailure &&
        (state.documentListFailure.baseId === undefined ||
          state.documentListFailure.baseId === documentsMatch[1]) &&
        state.documentListRequests >= state.documentListFailure.afterRequest
      ) {
        return knowledgeError(
          route,
          state.documentListFailure.status,
          state.documentListFailure.code,
          state.documentListFailure.message,
        );
      }
      const items = state.documents.filter(
        (item) => item.knowledge_base_id === documentsMatch[1],
      );
      for (const item of items) {
        const nextStatus = item.progression?.shift();
        if (nextStatus !== undefined) {
          item.status = nextStatus;
          if (item.status === "ready") item.segment_count = 4;
          if (item.status === "failed") {
            item.error_message = "Embedding 请求连续失败已耗尽重试";
          }
        }
      }
      // Real backend pagination: the client must stitch pages itself.
      const pageNumber = Number.parseInt(
        url.searchParams.get("page") ?? "1",
        10,
      );
      const pageSize = Number.parseInt(
        url.searchParams.get("page_size") ?? "100",
        10,
      );
      const pageItems =
        options.documentListTruncated && pageNumber > 1
          ? []
          : items.slice((pageNumber - 1) * pageSize, pageNumber * pageSize);
      return json(route, {
        items: pageItems.map(documentView),
        total: items.length,
        page: pageNumber,
        page_size: pageSize,
        request_id: "req-documents",
      });
    }
    if (documentsMatch && method === "POST") {
      const form = request.postData() ?? "";
      const uploadFileName =
        /name="file"; filename="([^"]+)"/u.exec(form)?.[1] ?? "";
      // Count every attempt, including rejected files, so retry coverage can
      // distinguish a failed-only retry from silently re-uploading successes.
      state.uploadRequests.push({
        baseId: documentsMatch[1]!,
        fileName: uploadFileName,
        displayName: multipartField(form, "name"),
      });
      if (uploadFileName.includes("reject") && !state.acceptRejectedUploads) {
        return knowledgeError(
          route,
          413,
          "KNOWLEDGE_QUOTA_EXCEEDED",
          "文件超出配额",
        );
      }
      state.uploadCounter += 1;
      const uploadedMode = multipartField(form, "chunking_mode");
      const uploadedChildSize = multipartField(form, "child_chunk_size");
      const uploaded: MockDocument = {
        id: `50000000-0000-4000-8000-00000000000${state.uploadCounter}`,
        knowledge_base_id: documentsMatch[1]!,
        name: `handbook-${state.uploadCounter}.txt`,
        original_name: `handbook-${state.uploadCounter}.txt`,
        status: "queued",
        segment_count: 0,
        chunk_separator: multipartField(form, "chunk_separator") ?? "\\n\\n",
        remove_extra_spaces:
          multipartField(form, "remove_extra_spaces") === "true",
        remove_urls_emails:
          multipartField(form, "remove_urls_emails") === "true",
        chunking_mode:
          uploadedMode === "parent_child" ? "parent_child" : "general",
        child_chunk_size:
          uploadedChildSize === null
            ? undefined
            : Number.parseInt(uploadedChildSize, 10),
        child_chunk_separator:
          multipartField(form, "child_chunk_separator") ?? undefined,
        error_message: null,
        delete_error: null,
        progression: ["processing", "ready"],
      };
      state.documents.push(uploaded);
      const base = state.bases.find((item) => item.id === documentsMatch[1]);
      if (base) base.document_count += 1;
      await options.uploadResponseGate;
      return json(route, {
        item: documentView(uploaded),
        request_id: "req-upload",
      });
    }

    if (path === `${knowledgeBase}/chunk-preview` && method === "POST") {
      const form = request.postData() ?? "";
      const fields = {
        file: /name="file"; filename="([^"]+)"/u.exec(form)?.[1] ?? "",
        chunk_size: multipartField(form, "chunk_size") ?? "",
        chunk_overlap: multipartField(form, "chunk_overlap") ?? "",
        chunk_separator: multipartField(form, "chunk_separator") ?? "",
        remove_extra_spaces: multipartField(form, "remove_extra_spaces") ?? "",
        remove_urls_emails: multipartField(form, "remove_urls_emails") ?? "",
        chunking_mode: multipartField(form, "chunking_mode") ?? "",
        child_chunk_size: multipartField(form, "child_chunk_size") ?? "",
        child_chunk_separator:
          multipartField(form, "child_chunk_separator") ?? "",
      };
      state.previewRequests.push(fields);
      // Sentinel separator lets tests exercise the error surface.
      if (fields.chunk_separator === "BOOM") {
        return knowledgeError(
          route,
          422,
          "KNOWLEDGE_PARSE_FAILED",
          "文件没有可提取的文本",
        );
      }
      // Slow files let tests race a replaced preview against its winner.
      if (fields.file.startsWith("slow")) {
        await new Promise((resolve) => setTimeout(resolve, 700));
      }
      const parentChild = fields.chunking_mode === "parent_child";
      const contents = [
        `预览分段一 size=${fields.chunk_size} sep=${fields.chunk_separator}`,
        `预览分段二 spaces=${fields.remove_extra_spaces} urls=${fields.remove_urls_emails}`,
        `预览来源 ${fields.file}`,
      ];
      return json(route, {
        items: contents.map((content, index) => ({
          position: index + 1,
          content,
          word_count: content.length,
          child_contents: parentChild
            ? [
                `父块${index + 1}子块一 child=${fields.child_chunk_size} csep=${fields.child_chunk_separator}`,
                `父块${index + 1}子块二`,
              ]
            : [],
        })),
        total: 7,
        request_id: "req-preview",
      });
    }

    if (
      path === `${knowledgeBase}/documents/batch-status` &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as {
        document_ids: string[];
        enabled: boolean;
      };
      const targets = state.documents.filter((item) =>
        body.document_ids.includes(item.id),
      );
      if (targets.length !== body.document_ids.length) {
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "文档不存在");
      }
      for (const item of targets) item.enabled = body.enabled;
      return json(route, {
        items: targets.map(documentView),
        request_id: "req-batch-status",
      });
    }
    if (
      path === `${knowledgeBase}/documents/batch-delete` &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as { document_ids: string[] };
      const targets = state.documents.filter((item) =>
        body.document_ids.includes(item.id),
      );
      if (targets.length !== body.document_ids.length) {
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "文档不存在");
      }
      state.documents = state.documents.filter(
        (item) => !body.document_ids.includes(item.id),
      );
      for (const target of targets) {
        const base = state.bases.find(
          (item) => item.id === target.knowledge_base_id,
        );
        if (base) base.document_count -= 1;
      }
      return json(route, {
        items: targets.map((item) =>
          documentView({ ...item, status: "deleting" }),
        ),
        request_id: "req-batch-delete",
      });
    }

    const documentMatch = /\/documents\/([0-9a-f-]{36})$/u.exec(path);
    if (documentMatch && method === "PATCH") {
      const target = state.documents.find(
        (item) => item.id === documentMatch[1],
      );
      if (!target)
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "文档不存在");
      const body = request.postDataJSON() as { name: string };
      target.name = body.name;
      return json(route, {
        item: documentView(target),
        request_id: "req-doc-rename",
      });
    }
    if (documentMatch && method === "DELETE") {
      const target = state.documents.find(
        (item) => item.id === documentMatch[1],
      );
      if (!target)
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "文档不存在");
      // A "stuck" document parks with a recorded delete_error on the first
      // delete; an explicit re-delete clears the error and completes.
      if (target.name.includes("stuck") && target.delete_error === null) {
        target.status = "deleting";
        target.delete_error = "MinIO 对象删除失败";
        return json(route, {
          item: documentView(target),
          request_id: "req-doc-delete",
        });
      }
      state.documents = state.documents.filter((item) => item.id !== target.id);
      const base = state.bases.find(
        (item) => item.id === target.knowledge_base_id,
      );
      if (base) base.document_count -= 1;
      return json(route, {
        item: documentView({ ...target, status: "deleting" }),
        request_id: "req-doc-delete",
      });
    }
    const retryMatch = /\/documents\/([0-9a-f-]{36})\/retry$/u.exec(path);
    if (retryMatch && method === "POST") {
      const target = state.documents.find((item) => item.id === retryMatch[1]);
      if (!target)
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "文档不存在");
      target.status = "queued";
      target.error_message = null;
      target.progression = ["processing", "ready"];
      return json(route, {
        item: documentView(target),
        request_id: "req-retry",
      });
    }
    // Single-segment detail: validates the base/document/segment lineage so
    // cross-base id combinations and deleted resources answer 404.
    const segmentDetailMatch =
      /\/bases\/([0-9a-f-]{36})\/documents\/([0-9a-f-]{36})\/segments\/([0-9a-f-]{36})$/u.exec(
        path,
      );
    if (segmentDetailMatch && method === "GET") {
      const [, baseId, documentId, segmentId] = segmentDetailMatch;
      const owner = state.documents.find(
        (item) => item.id === documentId && item.knowledge_base_id === baseId,
      );
      const segment = (state.segments.get(documentId!) ?? []).find(
        (item) => item.id === segmentId,
      );
      if (!owner || !segment) {
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "分段不存在");
      }
      // A version/digest pin that no longer matches is a conflict: the
      // caller's score belongs to content this endpoint no longer serves.
      const expectedVersion = url.searchParams.get("expected_document_version");
      const expectedDigest = url.searchParams.get("expected_content_digest");
      if (
        (expectedVersion !== null &&
          Number(expectedVersion) !== state.detailVersion) ||
        (expectedDigest !== null && expectedDigest !== state.detailDigest)
      ) {
        return knowledgeError(
          route,
          409,
          "KNOWLEDGE_CONFLICT",
          "文档内容已更新",
        );
      }
      const childList = state.segmentChildren.get(segmentId!) ?? [];
      const childPage = Number(url.searchParams.get("child_page") ?? "1");
      const pageChildren = childList.slice(
        (childPage - 1) * 50,
        childPage * 50,
      );
      return json(route, {
        segment: segmentView(segment),
        knowledge_base_id: baseId,
        document_id: documentId,
        document_name: owner.name,
        content_state: owner.status === "ready" ? "current" : "stale",
        stored_content_version: state.detailVersion,
        current_document_version: state.detailVersion,
        children_total: childList.length,
        child_page: childPage,
        children: pageChildren.map((child) => ({
          id: child.id,
          position: child.position,
          content: child.content,
          word_count: child.content.length,
        })),
        request_id: "req-segment-detail",
      });
    }

    const segmentsMatch = /\/documents\/([0-9a-f-]{36})\/segments$/u.exec(path);
    if (segmentsMatch && method === "GET") {
      const documentId = segmentsMatch[1]!;
      const target = state.documents.find((item) => item.id === documentId);
      // Documents without explicit segment state serve generated read-only
      // segments derived from segment_count (the pre-governance behavior).
      const segments =
        state.segments.get(documentId) ??
        (target
          ? Array.from({ length: target.segment_count }, (_, index) => ({
              id: `60000000-0000-4000-8000-00000000000${index + 1}`,
              position: index + 1,
              content: `分段 ${index + 1} 的内容`,
              enabled: true,
              source_position: { page: index + 1 },
            }))
          : []);
      return json(route, {
        items: segments.map(segmentView),
        total: segments.length,
        page: 1,
        page_size: 20,
        request_id: "req-segments",
      });
    }
    if (segmentsMatch && method === "POST") {
      const documentId = segmentsMatch[1]!;
      const target = state.documents.find((item) => item.id === documentId);
      if (!target)
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "文档不存在");
      const body = request.postDataJSON() as { content: string };
      const list = state.segments.get(documentId) ?? [];
      state.segmentCounter += 1;
      const created: MockSegment = {
        id: `61000000-0000-4000-8000-00000000000${state.segmentCounter}`,
        position: Math.max(0, ...list.map((item) => item.position)) + 1,
        content: body.content,
        enabled: true,
        source_position: { manual: true },
      };
      list.push(created);
      state.segments.set(documentId, list);
      target.segment_count += 1;
      target.word_count = (target.word_count ?? 0) + body.content.length;
      return json(route, {
        item: segmentView(created),
        request_id: "req-segment-create",
      });
    }

    const segmentMatch = /\/segments\/([0-9a-f-]{36})$/u.exec(path);
    if (segmentMatch && (method === "PATCH" || method === "DELETE")) {
      let owner: MockDocument | undefined;
      let segment: MockSegment | undefined;
      for (const [documentId, list] of state.segments) {
        const found = list.find((item) => item.id === segmentMatch[1]);
        if (found) {
          owner = state.documents.find((item) => item.id === documentId);
          segment = found;
          break;
        }
      }
      if (!owner || !segment)
        return knowledgeError(route, 404, "KNOWLEDGE_NOT_FOUND", "分段不存在");
      if (method === "PATCH") {
        const body = request.postDataJSON() as {
          content?: string;
          enabled?: boolean;
        };
        if (body.content !== undefined) {
          owner.word_count =
            (owner.word_count ?? 0) -
            segment.content.length +
            body.content.length;
          segment.content = body.content;
        }
        if (body.enabled !== undefined) segment.enabled = body.enabled;
        return json(route, {
          item: segmentView(segment),
          request_id: "req-segment-update",
        });
      }
      const list = state.segments.get(owner.id)!;
      state.segments.set(
        owner.id,
        list.filter((item) => item.id !== segment.id),
      );
      owner.segment_count -= 1;
      owner.word_count = (owner.word_count ?? 0) - segment.content.length;
      return json(route, {
        item: documentView(owner),
        request_id: "req-segment-delete",
      });
    }
    const downloadMatch = /\/documents\/([0-9a-f-]{36})\/download$/u.exec(path);
    if (downloadMatch && method === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "text/plain",
        headers: {
          "Content-Disposition": 'attachment; filename="handbook-1.txt"',
        },
        body: "knowledge download body",
      });
    }

    if (path === `${knowledgeBase}/search` && method === "POST") {
      const body = request.postDataJSON() as {
        query?: string;
        score_threshold?: number;
      } & Record<string, unknown>;
      state.searchRequests.push(body);
      const query = body.query ?? "";
      const debugDiagnostics = (
        emptyReason: string | null,
        citations: Array<Record<string, unknown>>,
        thresholdFiltered: number,
      ) =>
        body.debug === true
          ? {
              strategy_version: "m10-v1",
              lexical_version: 1,
              target_base_count: 1,
              effective_top_k: (body.top_k as number | undefined) ?? 10,
              per_base_route_budget: 50,
              retrieval_mode:
                (body.retrieval_mode as string | undefined) ?? "semantic",
              counts: {
                semantic_candidates: 3,
                lexical_candidates: body.retrieval_mode === "hybrid" ? 2 : 0,
                parents_deduplicated: 3,
                threshold_filtered: thresholdFiltered,
                stale_filtered: 0,
                returned: citations.length,
              },
              timings: {
                query_embedding_ms: 12,
                recall_ms: 34,
                rerank_ms: 56,
                final_validation_ms: 7,
              },
              model_ids: ["10000000-0000-4000-8000-00000000000e"],
              ranking_method: citations.length > 0 ? "rerank" : null,
              empty_reason: emptyReason,
              heterogeneous_without_lexical_evidence: false,
              hit_diagnostics: citations.map((citation) => ({
                segment_id: citation.segment_id,
                local_score: citation.score,
                local_score_kind: "rerank",
                score_domain: "reranker:10000000-0000-4000-8000-00000000000e",
                ranking_method: "rerank",
                ranking_score: citation.score,
                matched_children:
                  citation.segment_id === "60000000-0000-4000-8000-000000000011"
                    ? [
                        {
                          child_id: "61000000-0000-4000-8000-0000000000c2",
                          position: 2,
                          route: "lexical",
                          score: 0.91,
                        },
                      ]
                    : [],
              })),
            }
          : null;
      if (query.includes("rerank-down")) {
        return knowledgeError(
          route,
          502,
          "KNOWLEDGE_MODEL_UNAVAILABLE",
          "Reranker 服务暂不可用，请稍后重试",
        );
      }
      if (query.includes("slowrace")) {
        await new Promise<void>((resolve) => {
          state.releaseSlowSearch = resolve;
        });
        return json(route, {
          citations: [
            {
              knowledge_base_id: "40000000-0000-4000-8000-000000000001",
              knowledge_base_name: "产品手册",
              document_id: "50000000-0000-4000-8000-000000000001",
              document_name: "发布说明.pdf",
              segment_id: "60000000-0000-4000-8000-000000000011",
              segment_position: 7,
              snippet: "慢响应旧结果不得回流",
              score: 0.99,
              source_position: { page: 7 },
              document_version: 1,
              content_digest: "d".repeat(64),
              score_kind: "rerank",
            },
          ],
          diagnostics: null,
          request_id: "req-search-slow",
        });
      }
      const emptyKeyword = (
        [
          ["unrelated", "no_candidates"],
          ["notready", "not_ready"],
          ["staleconflict", "stale_candidates"],
        ] as const
      ).find(([keyword]) => query.includes(keyword));
      if (emptyKeyword) {
        state.queryCounter += 1;
        state.queries.unshift({
          id: `70000000-0000-4000-8000-00000000000${state.queryCounter}`,
          query,
          source: "retrieval_test",
          result_count: 0,
          top_score: null,
        });
        return json(route, {
          citations: [],
          diagnostics: debugDiagnostics(emptyKeyword[1], [], 0),
          request_id: "req-search-empty",
        });
      }
      // Reranked order deliberately differs from vector order: the page must
      // render exactly this order and these scores.
      const rerankedCitations = [
        {
          knowledge_base_id: "40000000-0000-4000-8000-000000000001",
          knowledge_base_name: "产品手册",
          document_id: "50000000-0000-4000-8000-000000000001",
          document_name: "发布说明.pdf",
          segment_id: "60000000-0000-4000-8000-000000000011",
          segment_position: 7,
          snippet: "重排后应当排在第一位的内容",
          score: 0.93,
          source_position: { page: 7 },
          document_version: 1,
          content_digest: "d".repeat(64),
          score_kind: "rerank",
        },
        {
          knowledge_base_id: "40000000-0000-4000-8000-000000000001",
          knowledge_base_name: "产品手册",
          document_id: "50000000-0000-4000-8000-000000000001",
          document_name: "发布说明.pdf",
          segment_id: "60000000-0000-4000-8000-000000000012",
          segment_position: 2,
          snippet: "向量召回更靠前、但重排后次序在后的内容",
          score: 0.41,
          source_position: { row: 12 },
          document_version: 1,
          content_digest: "e".repeat(64),
          score_kind: "rerank",
        },
        // Cross-encoder rerankers legally emit negative scores; the panel
        // must render them verbatim instead of clamping or hiding the hit.
        {
          knowledge_base_id: "40000000-0000-4000-8000-000000000001",
          knowledge_base_name: "产品手册",
          document_id: "50000000-0000-4000-8000-000000000001",
          document_name: "发布说明.pdf",
          segment_id: "60000000-0000-4000-8000-000000000013",
          segment_position: 5,
          snippet: "重排给出负分、阈值为 0 时仍需展示的内容",
          score: -0.12,
          source_position: { page: 9 },
          document_version: 1,
          content_digest: "f".repeat(64),
          score_kind: "rerank",
        },
      ];
      // Mirrors the backend contract: a positive threshold drops segments
      // scoring below it after reranking, while 0 disables filtering
      // entirely so negative rerank scores still pass through.
      const citations =
        typeof body.score_threshold === "number" && body.score_threshold > 0
          ? rerankedCitations.filter(
              (citation) => citation.score >= body.score_threshold!,
            )
          : rerankedCitations;
      state.queryCounter += 1;
      state.queries.unshift({
        id: `70000000-0000-4000-8000-00000000000${state.queryCounter}`,
        query,
        source: "retrieval_test",
        result_count: citations.length,
        top_score: citations[0]?.score ?? null,
      });
      return json(route, {
        citations,
        diagnostics: debugDiagnostics(
          citations.length === 0 ? "filtered_out" : null,
          citations,
          rerankedCitations.length - citations.length,
        ),
        request_id: "req-search",
      });
    }

    return json(route, { detail: "not found" }, 404);
  });

  return state;
}

async function openDocumentActions(page: Page, documentName: string) {
  const row = page
    .getByTestId("knowledge-document-rows")
    .getByRole("row")
    .filter({ hasText: documentName });
  await row
    .getByRole("button", { name: `Actions for ${documentName}` })
    .click();
  return page.getByRole("menu");
}

test("hides the Knowledge navigation entry when the feature is disabled", async ({
  page,
}) => {
  await mockKnowledgeRoutes(page, { featureDisabled: true });
  await page.goto("/projects/alpha");

  const navigation = page.getByRole("navigation", {
    name: "Project navigation",
  });
  await expect(navigation.getByRole("link", { name: "Agent" })).toBeVisible();
  await expect(navigation.getByRole("link", { name: "Knowledge" })).toHaveCount(
    0,
  );
});

test("shows the Knowledge navigation entry when the module is enabled", async ({
  page,
}) => {
  await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha");

  const navigation = page.getByRole("navigation", {
    name: "Project navigation",
  });
  await expect(
    navigation.getByRole("link", { name: "Knowledge" }).first(),
  ).toBeVisible();
});

test("creates a base through the wizard and watches the upload reach ready", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha/knowledge");

  // The empty state offers the document-first wizard as the primary entry.
  await expect(
    page.getByText("Create your first knowledge base"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Create from documents" }).click();

  // Step 1: pick files.
  await page.getByLabel("File").setInputFiles({
    name: "handbook.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("知识库验收文档内容"),
  });
  await expect(page.getByText("1 file selected")).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();

  // Step 2: chunk settings and model; the name prefills from the file.
  await expect(page.getByLabel("Name")).toHaveValue("handbook");

  // Entering step 2 generates the initial preview once.
  const previewPanel = page.getByTestId("chunk-preview-panel");
  await expect(
    previewPanel.getByText("Previewing: handbook.txt"),
  ).toBeVisible();
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=\\n\\n"),
  ).toBeVisible();
  await expect(previewPanel.getByText("Showing 3 of 7 chunks")).toBeVisible();
  expect(state.previewRequests).toHaveLength(1);

  // Returning to the same file and unchanged settings keeps the first result;
  // step navigation is not an implicit retry of a full-file preview request.
  await page.getByRole("button", { name: "Previous" }).click();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=\\n\\n"),
  ).toBeVisible();
  expect(state.previewRequests).toHaveLength(1);

  // Invalid values never trigger a request and keep refresh disabled. Revert
  // to the submitted value and the existing preview is authoritative again.
  const chunkSize = page.getByLabel("Chunk size (characters)");
  await chunkSize.fill("100");
  await expect(
    previewPanel.getByText(
      "Fix the invalid chunk settings before refreshing the preview.",
    ),
  ).toBeVisible();
  await expect(
    previewPanel.getByRole("button", { name: "Refresh preview" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Previous" }).click();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=\\n\\n"),
  ).toBeVisible();
  await expect(
    previewPanel.getByText(
      "Fix the invalid chunk settings before refreshing the preview.",
    ),
  ).toBeVisible();
  expect(state.previewRequests).toHaveLength(1);
  await chunkSize.fill("1000");
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toHaveCount(0);

  // Editing parameters keeps the last preview visible but marks it stale;
  // the complete file is not uploaded again until the user asks.
  await page.getByLabel("Delimiter").fill("。");
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Previous" }).click();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=\\n\\n"),
  ).toBeVisible();
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toBeVisible();
  await page.waitForTimeout(600);
  expect(state.previewRequests).toHaveLength(1);
  await previewPanel.getByRole("button", { name: "Refresh preview" }).click();
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=。"),
  ).toBeVisible();
  await page
    .getByLabel("Replace consecutive spaces, newlines and tabs")
    .check();
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toBeVisible();
  expect(state.previewRequests).toHaveLength(2);
  await previewPanel.getByRole("button", { name: "Refresh preview" }).click();
  await expect(
    previewPanel.getByText("预览分段二 spaces=true urls=false"),
  ).toBeVisible();

  // A requested backend failure surfaces inline; correcting the parameter
  // marks that failed request stale and explicit refresh recovers.
  await page.getByLabel("Delimiter").fill("BOOM");
  await previewPanel.getByRole("button", { name: "Refresh preview" }).click();
  await expect(previewPanel.getByText("文件没有可提取的文本")).toBeVisible();
  await page.getByLabel("Delimiter").fill("。");
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toBeVisible();
  await previewPanel.getByRole("button", { name: "Refresh preview" }).click();
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=。"),
  ).toBeVisible();

  await page.getByLabel("Name").fill("产品手册");
  await page.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  const reranker = page.getByRole("combobox", { name: "Reranker model" });
  await expect(reranker).toHaveText("No reranking");
  await reranker.click();
  await page
    .getByRole("option", { name: "SiliconFlow · BAAI/bge-reranker-v2-m3" })
    .click();
  // Reranking is available for either retrieval route; switching routes must
  // not discard the independently chosen model.
  const retrievalRoute = page.getByRole("radiogroup", {
    name: "Default retrieval route",
  });
  await retrievalRoute
    .getByRole("radio", { name: "Hybrid", exact: true })
    .check();
  await expect(reranker).toHaveText("SiliconFlow · BAAI/bge-reranker-v2-m3");
  await retrievalRoute
    .getByRole("radio", { name: "Vector search", exact: true })
    .check();
  await expect(reranker).toHaveText("SiliconFlow · BAAI/bge-reranker-v2-m3");
  await page.getByRole("button", { name: "Save & process" }).click();

  // Step 3: the base exists and embedding progress advances to ready.
  await expect(page.getByText("Knowledge base created")).toBeVisible();
  expect(state.baseCreates).toHaveLength(1);
  expect(state.baseCreates[0]).toMatchObject({
    name: "产品手册",
    embedding_model_id: MODEL_ID,
    retrieval_mode: "semantic",
    reranker_model_id: RERANK_MODEL_ID,
  });
  expect(state.bases[0]?.reranker_model_id).toBe(RERANK_MODEL_ID);
  const summary = page.getByRole("heading", { name: "Settings" }).locator("..");
  await expect(summary).toContainText("SiliconFlow · BAAI/bge-reranker-v2-m3");
  const statusList = page.getByTestId("wizard-document-status");
  await expect(statusList.getByText("handbook-1.txt")).toBeVisible();
  // The stateful mock advances queued → processing → ready on each poll.
  await expect(statusList.getByText("Ready")).toBeVisible({ timeout: 15_000 });

  // Finishing lands directly on the documents view of the new base.
  await page.getByRole("button", { name: "Go to documents" }).click();
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("handbook-1.txt").first()).toBeVisible();
  await expect(rows.getByText("Ready")).toBeVisible();
  await expect(rows.getByText("4", { exact: true })).toBeVisible();

  // The upload froze the parameters tuned in step 2.
  expect(state.documents.at(-1)?.chunk_separator).toBe("。");
  expect(state.documents.at(-1)?.remove_extra_spaces).toBe(true);
  expect(state.documents.at(-1)?.remove_urls_emails).toBe(false);
  expect(state.documents.at(-1)?.chunking_mode).toBe("general");
  expect(state.previewRequests.at(-1)).toEqual({
    file: "handbook.txt",
    chunk_size: "1000",
    chunk_overlap: "100",
    chunk_separator: "。",
    remove_extra_spaces: "true",
    remove_urls_emails: "false",
    chunking_mode: "general",
    child_chunk_size: "",
    child_chunk_separator: "",
  });
});

test("freezes wizard controls and submitted settings while base creation is pending", async ({
  page,
}) => {
  let releaseCreate!: () => void;
  const createBaseResponseGate = new Promise<void>((resolve) => {
    releaseCreate = resolve;
  });
  let releaseUpload!: () => void;
  const uploadResponseGate = new Promise<void>((resolve) => {
    releaseUpload = resolve;
  });
  const state = await mockKnowledgeRoutes(page, {
    createBaseResponseGate,
    uploadResponseGate,
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "Create from documents" }).click();
  await page.getByLabel("File").setInputFiles({
    name: "frozen.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("冻结提交参数验收内容"),
  });
  await page.getByRole("button", { name: "Next" }).click();

  await page.getByRole("radio", { name: /Parent-child/u }).check();
  await page.getByLabel("Chunk overlap (characters)").fill("0");
  await page.getByLabel(/^Delimiter/u).fill("。");
  await page.getByLabel("Child chunk size (characters)").fill("300");
  await page
    .getByLabel("Replace consecutive spaces, newlines and tabs")
    .check();
  await page.getByLabel("Name").fill("冻结设置");
  await page.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  const retrievalRoute = page.getByRole("radiogroup", {
    name: "Default retrieval route",
  });
  await retrievalRoute
    .getByRole("radio", { name: "Hybrid", exact: true })
    .check();
  const reranker = page.getByRole("combobox", { name: "Reranker model" });
  await reranker.click();
  await page
    .getByRole("option", { name: "SiliconFlow · BAAI/bge-reranker-v2-m3" })
    .click();
  await page.getByRole("button", { name: "Save & process" }).click();

  // The mock has accepted the create, but the browser still awaits its reply.
  await expect.poll(() => state.bases.length).toBe(1);
  try {
    expect(state.baseCreates).toHaveLength(1);
    expect(state.baseCreates[0]).toMatchObject({
      name: "冻结设置",
      embedding_model_id: MODEL_ID,
      retrieval_mode: "hybrid",
      reranker_model_id: RERANK_MODEL_ID,
    });
    await expect(page.getByRole("button", { name: "Back" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Previous" })).toBeDisabled();
    await expect(page.getByLabel("Name")).toBeDisabled();
    await expect(page.getByLabel("Description")).toBeDisabled();
    await expect(page.getByLabel("Embedding model")).toBeDisabled();
    await expect(reranker).toBeDisabled();
    await expect(reranker).toHaveText("SiliconFlow · BAAI/bge-reranker-v2-m3");
    await expect(
      retrievalRoute.getByRole("radio", { name: "Hybrid", exact: true }),
    ).toBeDisabled();
    await expect(
      retrievalRoute.getByRole("radio", { name: "Vector search", exact: true }),
    ).toBeDisabled();
    await expect(page.getByRole("radio", { name: /General/u })).toBeDisabled();
    await expect(
      page.getByRole("radio", { name: /Parent-child/u }),
    ).toBeDisabled();
    await expect(page.getByLabel(/^Chunk size \(characters\)/u)).toBeDisabled();
    await expect(page.getByLabel("Chunk overlap (characters)")).toBeDisabled();
    await expect(page.getByLabel(/^Delimiter/u)).toBeDisabled();
    await expect(
      page.getByLabel("Child chunk size (characters)"),
    ).toBeDisabled();
    await expect(page.getByLabel("Child delimiter")).toBeDisabled();
    await expect(
      page.getByLabel("Replace consecutive spaces, newlines and tabs"),
    ).toBeDisabled();
    await expect(
      page.getByLabel("Delete all URLs and email addresses"),
    ).toBeDisabled();
    releaseCreate();
    await expect.poll(() => state.uploadCounter).toBe(1);
    await expect(page.getByRole("button", { name: "Back" })).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "Go to documents" }),
    ).toBeDisabled();
  } finally {
    releaseCreate();
    releaseUpload();
  }

  await expect(page.getByText("Knowledge base created")).toBeVisible();
  expect(state.documents.at(-1)).toMatchObject({
    chunk_separator: "。",
    remove_extra_spaces: true,
    chunking_mode: "parent_child",
    child_chunk_size: 300,
  });
  const summary = page.getByRole("heading", { name: "Settings" }).locator("..");
  await expect(summary).toContainText("Parent-child");
  await expect(summary).toContainText("300");
  await expect(summary).toContainText("。");
  await expect(summary).toContainText("SiliconFlow · BAAI/bge-reranker-v2-m3");
  expect(state.bases[0]?.reranker_model_id).toBe(RERANK_MODEL_ID);
});

test("does not continue document uploads after the create wizard unmounts", async ({
  page,
}) => {
  let releaseCreate!: () => void;
  const createBaseResponseGate = new Promise<void>((resolve) => {
    releaseCreate = resolve;
  });
  const state = await mockKnowledgeRoutes(page, { createBaseResponseGate });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "Create from documents" }).click();
  await page.getByLabel("File").setInputFiles({
    name: "leave-before-upload.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("离开向导后不得继续上传"),
  });
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Name").fill("卸载保护");
  await page.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  await page.getByRole("button", { name: "Save & process" }).click();
  await expect.poll(() => state.bases.length).toBe(1);

  try {
    await page
      .getByRole("navigation", { name: "Project navigation" })
      .getByRole("link", { name: "Overview" })
      .first()
      .click();
    await expect(page).toHaveURL(/\/projects\/alpha$/u);
  } finally {
    releaseCreate();
  }
  // Give the accepted create response and its async continuation time to run.
  await page.waitForTimeout(500);
  expect(state.uploadCounter).toBe(0);
});

test("creates an unconfigured empty base using only name and description", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha/knowledge");

  await page.getByRole("button", { name: "New base" }).click();
  await page.getByRole("button", { name: "Create an empty base" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name").fill("空知识库");
  await dialog.getByLabel("Description").fill("稍后配置并上传文档");
  await expect(dialog.getByRole("combobox")).toHaveCount(0);
  await expect(dialog.getByRole("radiogroup")).toHaveCount(0);
  expect(state.modelOptionsRequests).toBe(0);
  await dialog.getByRole("button", { name: "Create", exact: true }).click();

  const baseList = page.getByTestId("knowledge-base-list");
  await expect(baseList.getByText("空知识库")).toBeVisible();
  await expect(baseList.getByText("0 documents")).toBeVisible();
  await expect(baseList.getByText("Not configured")).toBeVisible();
  expect(state.baseCreates).toEqual([
    { name: "空知识库", description: "稍后配置并上传文档" },
  ]);
  expect(state.bases[0]).toMatchObject({
    embedding_model_id: null,
    retrieval_mode: "semantic",
    reranker_model_id: null,
    document_count: 0,
  });
  expect(state.modelOptionsRequests).toBe(0);
  expect(state.uploadCounter).toBe(0);
});

test("an existing empty base configures in wizard step two before uploading", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  let releaseConfiguration!: () => void;
  const baseUpdateResponseGate = new Promise<void>((resolve) => {
    releaseConfiguration = resolve;
  });
  const state = await mockKnowledgeRoutes(page, {
    baseUpdateResponseGate,
    bases: [
      {
        id: BASE_ID,
        name: "Awaiting configuration",
        description: "",
        status: "active",
        document_count: 0,
        delete_error: null,
        embedding_model_id: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await expect(
    page.getByTestId("knowledge-base-list").getByText("Not configured"),
  ).toBeVisible();
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Upload document" }).click();
  const wizard = page.getByTestId("knowledge-create-wizard");
  await expect(wizard).toBeVisible();
  await expect(
    page.getByRole("dialog", { name: "Configure knowledge base" }),
  ).toHaveCount(0);
  await wizard.getByLabel("File", { exact: true }).setInputFiles({
    name: "configured-first-upload.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Upload only after the initial configuration succeeds"),
  });
  await wizard.getByRole("button", { name: "Next", exact: true }).click();
  await expect(wizard.getByTestId("chunk-preview-panel")).toContainText(
    "Previewing: configured-first-upload.txt",
  );
  await wizard.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  await wizard.getByRole("radio", { name: "Hybrid", exact: true }).check();
  await wizard.getByLabel("Reranker model").click();
  await page
    .getByRole("option", { name: "SiliconFlow · BAAI/bge-reranker-v2-m3" })
    .click();

  try {
    await wizard
      .getByRole("button", { name: "Upload & process", exact: true })
      .click();
    await expect.poll(() => state.baseUpdates.length).toBe(1);
    expect(state.baseUpdates[0]).toEqual({
      embedding_model_id: MODEL_ID,
      retrieval_mode: "hybrid",
      reranker_model_id: RERANK_MODEL_ID,
    });
    // Persisting the mock row is not a successful PATCH response. The wizard
    // must remain in step two without admitting any file until it settles.
    await expect(wizard.getByTestId("chunk-preview-panel")).toBeVisible();
    await expect(wizard.getByTestId("wizard-document-status")).toHaveCount(0);
    expect(state.uploadRequests).toHaveLength(0);
    expect(state.documents).toHaveLength(0);
    expect(state.baseCreates).toHaveLength(0);
  } finally {
    releaseConfiguration();
  }

  await expect(
    wizard.getByTestId("wizard-document-status").getByText("handbook-1.txt"),
  ).toBeVisible();
  expect(state.bases[0]).toMatchObject({
    embedding_model_id: MODEL_ID,
    retrieval_mode: "hybrid",
    reranker_model_id: RERANK_MODEL_ID,
  });
  expect(state.uploadRequests).toHaveLength(1);
  expect(state.uploadRequests[0]).toMatchObject({
    baseId: BASE_ID,
    fileName: "configured-first-upload.txt",
  });
  expect(state.baseCreates).toHaveLength(0);
  expect(state.baseUpdates).toHaveLength(1);
  await wizard.getByRole("button", { name: "Go to documents" }).click();
  await expect(
    page.getByTestId("knowledge-document-rows").getByText("handbook-1.txt"),
  ).toBeVisible();
});

test("failed initial configuration stays in wizard step two and preserves upload choices", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "Configuration failure",
        description: "",
        status: "active",
        document_count: 0,
        delete_error: null,
        embedding_model_id: null,
        retrieval_mode: "semantic",
        reranker_model_id: null,
      },
    ],
  });
  state.baseUpdateFailure = {
    status: 503,
    code: "KNOWLEDGE_UNAVAILABLE",
    message: "Model binding temporarily unavailable.",
  };
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Upload document" }).click();
  const wizard = page.getByTestId("knowledge-create-wizard");
  await wizard.getByLabel("File", { exact: true }).setInputFiles({
    name: "preserved.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(
      "Preserve this selected file across a failed configuration",
    ),
  });
  await wizard.getByRole("button", { name: "Next", exact: true }).click();
  await expect(wizard.getByTestId("chunk-preview-panel")).toContainText(
    "Previewing: preserved.txt",
  );
  await wizard
    .getByLabel("Chunk size (characters)", { exact: true })
    .fill("1200");
  await wizard
    .getByLabel("Display name (optional)", { exact: true })
    .fill("Preserved display name");
  await wizard.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  await wizard.getByRole("radio", { name: "Hybrid", exact: true }).check();
  await wizard.getByLabel("Reranker model").click();
  await page
    .getByRole("option", { name: "SiliconFlow · BAAI/bge-reranker-v2-m3" })
    .click();
  await wizard
    .getByRole("button", { name: "Upload & process", exact: true })
    .click();

  await expect(wizard.getByRole("alert")).toHaveText(
    "Model binding temporarily unavailable.",
  );
  await expect(wizard.getByLabel("Embedding model")).toHaveText(
    "SiliconFlow · BAAI/bge-m3",
  );
  await expect(
    wizard.getByRole("radio", { name: "Hybrid", exact: true }),
  ).toBeChecked();
  await expect(wizard.getByLabel("Reranker model")).toHaveText(
    "SiliconFlow · BAAI/bge-reranker-v2-m3",
  );
  await expect(
    wizard.getByLabel("Chunk size (characters)", { exact: true }),
  ).toHaveValue("1200");
  await expect(
    wizard.getByLabel("Display name (optional)", { exact: true }),
  ).toHaveValue("Preserved display name");
  await expect(wizard.getByTestId("chunk-preview-panel")).toContainText(
    "Previewing: preserved.txt",
  );
  await expect(
    wizard.getByRole("button", { name: "Upload & process", exact: true }),
  ).toBeEnabled();
  await expect(wizard.getByTestId("wizard-document-status")).toHaveCount(0);
  expect(state.baseUpdates).toHaveLength(1);
  expect(state.baseCreates).toHaveLength(0);
  expect(state.bases[0]).toMatchObject({
    embedding_model_id: null,
    retrieval_mode: "semantic",
    reranker_model_id: null,
    document_count: 0,
  });
  expect(state.uploadRequests).toHaveLength(0);
  expect(state.documents).toHaveLength(0);

  // A configuration failure may retry PATCH; the same retained file follows
  // only its successful response, without creating a replacement base.
  await wizard
    .getByRole("button", { name: "Upload & process", exact: true })
    .click();
  await expect(
    wizard.getByTestId("wizard-document-status").getByText("handbook-1.txt"),
  ).toBeVisible();
  expect(state.baseUpdates).toHaveLength(2);
  expect(state.baseCreates).toHaveLength(0);
  expect(state.uploadRequests).toEqual([
    {
      baseId: BASE_ID,
      fileName: "preserved.txt",
      displayName: "Preserved display name",
    },
  ]);
});

test("read-only members cannot configure an unconfigured base", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    capabilities: READ_CAPABILITIES,
    bases: [
      {
        id: BASE_ID,
        name: "Unconfigured read-only base",
        description: "",
        status: "active",
        document_count: 0,
        delete_error: null,
        embedding_model_id: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await expect(
    page.getByTestId("knowledge-base-list").getByText("Not configured"),
  ).toBeVisible();
  await page.getByRole("button", { name: "View documents" }).click();
  await expect(
    page.getByRole("button", { name: "Upload document" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Configure models" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Settings", exact: true }),
  ).toHaveCount(0);

  // A manually entered settings URL cannot grant the missing edit capability.
  await page.goto(`/projects/alpha/knowledge?kb=${BASE_ID}&view=settings`);
  await expect(
    page.getByRole("button", { name: "Configure models" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("dialog", { name: "Configure knowledge base" }),
  ).toHaveCount(0);
  await expect(page.getByTestId("knowledge-create-wizard")).toHaveCount(0);
  expect(state.baseCreates).toHaveLength(0);
  expect(state.baseUpdates).toHaveLength(0);
  expect(state.uploadRequests).toHaveLength(0);
  expect(state.modelOptionsRequests).toBe(0);
  expect(state.uploadCounter).toBe(0);
});

test("settings configure an empty base without opening an upload wizard", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "Configure from settings",
        description: "",
        status: "active",
        document_count: 0,
        delete_error: null,
        embedding_model_id: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Re-embed documents" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Configure models" }).click();
  const configuration = page.getByRole("dialog", {
    name: "Configure knowledge base",
  });
  await configuration.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  await expect(
    configuration.getByRole("radio", { name: "Vector search", exact: true }),
  ).toBeChecked();
  await expect(configuration.getByLabel("Reranker model")).toHaveText(
    "No reranking",
  );
  await configuration
    .getByRole("button", { name: "Save configuration" })
    .click();

  await expect(configuration).toHaveCount(0);
  expect(state.baseUpdates).toEqual([
    { embedding_model_id: MODEL_ID, retrieval_mode: "semantic" },
  ]);
  expect(state.bases[0]?.embedding_model_id).toBe(MODEL_ID);
  await expect(
    page.getByRole("button", { name: "Configure models" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Re-embed documents" }),
  ).toBeVisible();
  await expect(page.getByTestId("knowledge-create-wizard")).toHaveCount(0);
  expect(state.rebuildRequests).toHaveLength(0);
  expect(state.uploadRequests).toHaveLength(0);
  expect(state.uploadCounter).toBe(0);
  expect(state.documents).toHaveLength(0);
});

test("persists the base retrieval route at creation and in settings without changing it for a search override", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "Create from documents" }).click();
  await page.getByLabel("File").setInputFiles({
    name: "persistent-route.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("Keep the chosen retrieval route after creation"),
  });
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Name").fill("Persistent retrieval route");
  await page.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  await page
    .getByRole("radiogroup", { name: "Default retrieval route" })
    .getByRole("radio", { name: "Hybrid", exact: true })
    .check();
  await page.getByRole("button", { name: "Save & process" }).click();
  await expect(page.getByText("Knowledge base created")).toBeVisible();
  expect(state.bases[0]?.retrieval_mode).toBe("hybrid");

  await page.getByRole("button", { name: "Go to documents" }).click();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const route = page.getByRole("radiogroup", {
    name: "Default retrieval route",
  });
  await expect(
    route.getByRole("radio", { name: "Hybrid", exact: true }),
  ).toBeChecked();
  await route
    .getByRole("radio", { name: "Vector search", exact: true })
    .check();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  expect(state.baseUpdates.at(-1)).toMatchObject({
    retrieval_mode: "semantic",
  });
  await page.reload();
  await expect(
    route.getByRole("radio", { name: "Vector search", exact: true }),
  ).toBeChecked();

  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("single search override");
  await page
    .getByRole("combobox", { name: "Retrieval route", exact: true })
    .click();
  await page.getByRole("option", { name: "Hybrid (this search)" }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect.poll(() => state.searchRequests.length).toBe(1);
  expect(state.searchRequests[0]).toMatchObject({ retrieval_mode: "hybrid" });
  expect(state.bases[0]?.retrieval_mode).toBe("semantic");
});

test("the document creation wizard persists its chosen base retrieval route", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "Create from documents" }).click();
  await page.getByLabel("File").setInputFiles({
    name: "hybrid-handbook.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("A handbook for hybrid retrieval"),
  });
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  const route = page.getByRole("radiogroup", {
    name: "Default retrieval route",
  });
  await expect(
    route.getByRole("radio", { name: "Vector search", exact: true }),
  ).toBeChecked();
  await route.getByRole("radio", { name: "Hybrid", exact: true }).check();
  await page.getByRole("button", { name: "Save & process" }).click();
  await expect(page.getByText("Knowledge base created")).toBeVisible();
  expect(state.bases[0]?.retrieval_mode).toBe("hybrid");
  expect(state.baseCreates[0]).not.toHaveProperty("reranker_model_id");
  const summary = page.getByRole("heading", { name: "Settings" }).locator("..");
  await expect(summary).toContainText("No reranking");
});

test("wizard omits a cleared reranker when creating a hybrid base", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "Create from documents" }).click();
  await page.getByLabel("File").setInputFiles({
    name: "no-reranker.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("A hybrid base without a reranking model"),
  });
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  const retrievalRoute = page.getByRole("radiogroup", {
    name: "Default retrieval route",
  });
  await retrievalRoute
    .getByRole("radio", { name: "Hybrid", exact: true })
    .check();
  const reranker = page.getByRole("combobox", { name: "Reranker model" });
  await reranker.click();
  await page
    .getByRole("option", { name: "SiliconFlow · BAAI/bge-reranker-v2-m3" })
    .click();
  await expect(reranker).toHaveText("SiliconFlow · BAAI/bge-reranker-v2-m3");
  await reranker.click();
  await page.getByRole("option", { name: "No reranking", exact: true }).click();
  await expect(reranker).toHaveText("No reranking");
  await page.getByRole("button", { name: "Save & process" }).click();

  await expect(page.getByText("Knowledge base created")).toBeVisible();
  expect(state.baseCreates).toHaveLength(1);
  expect(state.baseCreates[0]).toMatchObject({ retrieval_mode: "hybrid" });
  expect(state.baseCreates[0]).not.toHaveProperty("reranker_model_id");
  expect(state.baseCreates[0]).not.toHaveProperty("clear_reranker_model");
  expect(state.bases[0]?.reranker_model_id).toBeNull();
  const summary = page.getByRole("heading", { name: "Settings" }).locator("..");
  await expect(summary).toContainText("No reranking");
});

test("failed document shows the error, retry re-queues it, and the segment browser opens when ready", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000009",
        knowledge_base_id: BASE_ID,
        name: "broken.pdf",
        original_name: "broken.pdf",
        status: "failed",
        segment_count: 0,
        error_message: "Embedding 请求连续失败已耗尽重试",
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Failed")).toBeVisible();
  await expect(
    rows.getByText("Embedding 请求连续失败已耗尽重试"),
  ).toBeVisible();

  await (await openDocumentActions(page, "broken.pdf"))
    .getByRole("menuitem", { name: "Retry" })
    .click();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 15_000 });

  // The segment browser replaces the documents table in place.
  await (await openDocumentActions(page, "broken.pdf"))
    .getByRole("menuitem", { name: "View segments" })
    .click();
  const browser = page.getByTestId("knowledge-segment-browser");
  await expect(
    browser.getByTestId("knowledge-segment-list").getByText("分段 1 的内容"),
  ).toBeVisible();
  await expect(browser.getByText("Page 1 of 1 · 4 total")).toBeVisible();

  // The back entry returns to the documents table of the same base.
  await browser.getByRole("button", { name: "Documents" }).click();
  await expect(page.getByTestId("knowledge-document-rows")).toBeVisible();
});

test("keeps document actions reachable at 1280px and reveals a bounded error in full", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const longDisplayName =
    "broken-reference-manual-with-a-long-visible-name.pdf";
  const longOriginalName =
    "original-broken-reference-manual-source-file-name.pdf";
  const longError =
    "Embedding provider rejected this document after every retry because the configured model is temporarily unavailable for this project.";
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 2,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000001",
        knowledge_base_id: BASE_ID,
        name: "guide.txt",
        original_name: "guide.txt",
        status: "ready",
        segment_count: 2,
        error_message: null,
        delete_error: null,
      },
      {
        id: "50000000-0000-4000-8000-000000000002",
        knowledge_base_id: BASE_ID,
        name: longDisplayName,
        original_name: longOriginalName,
        status: "failed",
        segment_count: 0,
        error_message: longError,
        delete_error: null,
      },
    ],
  });
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  const table = page.getByTestId("knowledge-documents-table");
  const guideRow = table.getByRole("row").filter({ hasText: "guide.txt" });
  const actions = guideRow.getByRole("button", {
    name: "Actions for guide.txt",
  });
  await expect(actions).toBeVisible();
  const tableWidth = await table.evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(tableWidth.scroll).toBe(tableWidth.client);
  expect(await table.evaluate((element) => element.scrollLeft)).toBe(0);

  await actions.click();
  await expect(
    page.getByRole("menuitem", { name: "View segments" }),
  ).toBeVisible();
  await expect(
    page.getByRole("menuitem", { name: "Download original" }),
  ).toBeVisible();
  expect(await table.evaluate((element) => element.scrollLeft)).toBe(0);
  await page.keyboard.press("Escape");

  const failedRow = table.getByRole("row").filter({ hasText: longDisplayName });
  await expect(failedRow.getByText(longDisplayName)).toHaveAttribute(
    "title",
    longDisplayName,
  );
  await expect(failedRow.getByText(longOriginalName)).toHaveAttribute(
    "title",
    longOriginalName,
  );
  const errorStatus = failedRow.getByRole("status");
  await expect(errorStatus).toHaveAttribute("aria-live", "polite");
  await expect(errorStatus).toContainText(longError);
  const boundedError = failedRow.getByText(longError);
  const errorBox = await boundedError.boundingBox();
  expect(errorBox?.height).toBeLessThanOrEqual(36);
  await boundedError.hover();
  await expect(page.getByRole("tooltip")).toHaveText(longError);
});

test("keeps cached document rows visible when a background refresh fails and retries", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000003",
        knowledge_base_id: BASE_ID,
        name: "cached.txt",
        original_name: "cached.txt",
        status: "ready",
        segment_count: 2,
        error_message: null,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("cached.txt")).toBeVisible();
  state.documentListFailure = {
    afterRequest: state.documentListRequests + 1,
    status: 500,
    code: "KNOWLEDGE_UNAVAILABLE",
    message: "Document refresh is temporarily unavailable.",
  };
  await rows.getByRole("switch", { name: "Disable cached.txt" }).click();

  const refreshAlert = page.getByRole("alert").filter({
    hasText: "Document refresh is temporarily unavailable.",
  });
  await expect(refreshAlert).toBeVisible({ timeout: 15_000 });
  await expect(rows.getByText("cached.txt")).toBeVisible();

  state.documentListFailure = null;
  await refreshAlert.getByRole("button", { name: "Retry" }).click();
  await expect(refreshAlert).toHaveCount(0);
  await expect(rows.getByText("cached.txt")).toBeVisible();
});

test("hides an open segment browser after authority loss and recovers on another base", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const OTHER_BASE_ID = "40000000-0000-4000-8000-000000000002";
  const REVOKED_DOCUMENT_ID = "50000000-0000-4000-8000-000000000004";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册 A",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
      {
        id: OTHER_BASE_ID,
        name: "产品手册 B",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: REVOKED_DOCUMENT_ID,
        knowledge_base_id: BASE_ID,
        name: "revoked.txt",
        original_name: "revoked.txt",
        status: "ready",
        segment_count: 2,
        error_message: null,
        delete_error: null,
      },
      {
        id: "50000000-0000-4000-8000-000000000005",
        knowledge_base_id: OTHER_BASE_ID,
        name: "other-base.txt",
        original_name: "other-base.txt",
        status: "ready",
        segment_count: 1,
        error_message: null,
        delete_error: null,
      },
    ],
    segments: {
      [REVOKED_DOCUMENT_ID]: [
        {
          id: "60000000-0000-4000-8000-000000000004",
          position: 1,
          content: "撤权前可见的第一段",
          enabled: true,
          source_position: { page: 1 },
        },
        {
          id: "60000000-0000-4000-8000-000000000005",
          position: 2,
          content: "撤权前可见的第二段",
          enabled: true,
          source_position: { page: 2 },
        },
      ],
    },
  });
  await page.goto("/projects/alpha/knowledge");
  const baseList = page.getByTestId("knowledge-base-list");
  await baseList
    .getByRole("listitem")
    .filter({ hasText: "产品手册 A" })
    .getByRole("button", { name: "View documents" })
    .click();

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("revoked.txt")).toBeVisible();
  await (await openDocumentActions(page, "revoked.txt"))
    .getByRole("menuitem", { name: "View segments" })
    .click();
  const browser = page.getByTestId("knowledge-segment-browser");
  await expect(browser).toBeVisible();

  const requestsBeforeRevocation = state.documentListRequests;
  state.documentListFailure = {
    baseId: BASE_ID,
    afterRequest: requestsBeforeRevocation + 1,
    status: 403,
    code: "KNOWLEDGE_FORBIDDEN",
    message: "Document access was revoked.",
  };
  await browser.getByRole("switch", { name: "Disable segment #1" }).click();
  await expect(
    page.getByRole("alert").filter({
      hasText: "Document access was revoked.",
    }),
  ).toBeVisible({ timeout: 15_000 });
  await expect(browser).toHaveCount(0);
  await expect(page.getByTestId("knowledge-document-rows")).toHaveCount(0);
  expect(state.documentListRequests).toBe(requestsBeforeRevocation + 1);
  const requestsAfterAuthorityBoundary = state.documentListRequests;
  // Once authority is revoked this exact scoped list is terminal. It must not
  // poll, retry, or recreate a removed active query in the background.
  await page.waitForTimeout(2_500);
  expect(state.documentListRequests).toBe(requestsAfterAuthorityBoundary);

  // The block belongs only to base A. A different base gets a fresh request
  // and renders its own scoped rows.
  await page.getByRole("button", { name: "Back" }).click();
  await baseList
    .getByRole("listitem")
    .filter({ hasText: "产品手册 B" })
    .getByRole("button", { name: "View documents" })
    .click();
  await expect(
    page.getByTestId("knowledge-document-rows").getByText("other-base.txt"),
  ).toBeVisible();
  expect(state.documentListRequests).toBeGreaterThan(
    requestsAfterAuthorityBoundary,
  );
  const requestsAfterOtherBase = state.documentListRequests;
  await page.waitForTimeout(2_500);
  expect(state.documentListRequests).toBe(requestsAfterOtherBase);
});

test("deletes ready and failed documents after confirmation", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 2,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000008",
        knowledge_base_id: BASE_ID,
        name: "old.txt",
        original_name: "old.txt",
        status: "ready",
        segment_count: 2,
        error_message: null,
        delete_error: null,
      },
      {
        id: "50000000-0000-4000-8000-000000000007",
        knowledge_base_id: BASE_ID,
        name: "bad.pdf",
        original_name: "bad.pdf",
        status: "failed",
        segment_count: 0,
        error_message: "文件解析失败",
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("old.txt")).toBeVisible();
  const downloadLink = (await openDocumentActions(page, "old.txt")).getByRole(
    "menuitem",
    { name: "Download original" },
  );
  await expect(downloadLink).toHaveAttribute("download", "old.txt");
  await page.keyboard.press("Escape");

  // A failed document is deletable without a prior retry.
  const failedRow = rows.getByRole("row").filter({ hasText: "bad.pdf" });
  await expect(failedRow.getByText("文件解析失败")).toBeVisible();
  await (await openDocumentActions(page, "bad.pdf"))
    .getByRole("menuitem", { name: "Delete" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(rows.getByText("bad.pdf")).toHaveCount(0);
  await expect(rows.getByText("old.txt")).toBeVisible();

  await (await openDocumentActions(page, "old.txt"))
    .getByRole("menuitem", { name: "Delete" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(
    page.getByText("No documents yet", { exact: false }),
  ).toBeVisible();
});

test("parks a failed document delete with the reason, stops polling, and re-delete completes it", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000006",
        knowledge_base_id: BASE_ID,
        name: "stuck-notes.txt",
        original_name: "stuck-notes.txt",
        status: "ready",
        segment_count: 2,
        error_message: null,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  const rows = page.getByTestId("knowledge-document-rows");
  const row = rows.getByRole("row").filter({ hasText: "stuck-notes.txt" });
  await (await openDocumentActions(page, "stuck-notes.txt"))
    .getByRole("menuitem", { name: "Delete" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();

  // The row parks with the recorded reason instead of vanishing.
  await expect(row.getByText("MinIO 对象删除失败")).toBeVisible();
  await expect(row.getByText("Deleting")).toBeVisible();

  // Parked rows stop the 2s list polling entirely.
  const requestsAfterPark = state.documentListRequests;
  await page.waitForTimeout(4500);
  expect(state.documentListRequests).toBe(requestsAfterPark);

  // An explicit re-delete clears the error and completes.
  await (await openDocumentActions(page, "stuck-notes.txt"))
    .getByRole("menuitem", { name: "Delete" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(
    page.getByText("No documents yet", { exact: false }),
  ).toBeVisible();
});

test("renders reranked search results, backend errors, and the empty state", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  // The retrieval test lives in the in-base menu, not at the page level.
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Retrieval test" }).click();

  // Reranked order and relevance wording (never "vector similarity").
  await page.getByLabel("Query").fill("发布流程");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const results = page.getByTestId("knowledge-search-results");
  await expect(results.getByRole("listitem")).toHaveCount(3);
  await expect(results.getByRole("listitem").first()).toContainText(
    "重排后应当排在第一位的内容",
  );
  await expect(results.getByRole("listitem").first()).toContainText(
    "Retrieval score 0.930",
  );
  await expect(results.getByRole("listitem").first()).toContainText("Page 7");
  await expect(results.getByRole("listitem").nth(1)).toContainText("Row 12");
  // A negative rerank score renders as-is at the end of the ranking.
  await expect(results.getByRole("listitem").nth(2)).toContainText(
    "Retrieval score -0.120",
  );

  // The scoped panel always narrows the request to the open base.
  expect(state.searchRequests.at(-1)?.knowledge_base_ids).toEqual([BASE_ID]);

  // Reranker outage surfaces the backend message and hides results: the
  // page must not fall back to showing cosine-only (previous) results.
  await page.getByLabel("Query").fill("rerank-down 状况");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page.getByText("Reranker 服务暂不可用，请稍后重试"),
  ).toBeVisible();
  await expect(page.getByTestId("knowledge-search-results")).toHaveCount(0);

  // An unrelated query lands in the explicit empty state.
  await page.getByLabel("Query").fill("unrelated question");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("knowledge-search-empty")).toBeVisible();
});

test("sends the score threshold and shows the empty state when it filters every match", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: "40000000-0000-4000-8000-000000000001",
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Retrieval test" }).click();

  // A mid threshold keeps only the segment scoring above it (0.93 > 0.5 > 0.41).
  await page.getByLabel("Query").fill("发布流程");
  await page.getByLabel("Score threshold").fill("0.5");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const results = page.getByTestId("knowledge-search-results");
  await expect(results.getByRole("listitem")).toHaveCount(1);
  await expect(results.getByRole("listitem").first()).toContainText(
    "Retrieval score 0.930",
  );
  expect(state.searchRequests.at(-1)?.score_threshold).toBe(0.5);

  // An explicit 0 threshold means "no filtering": negative rerank scores
  // must come back and render instead of being silently dropped.
  await page.getByLabel("Score threshold").fill("0");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(results.getByRole("listitem")).toHaveCount(3);
  await expect(results.getByRole("listitem").nth(2)).toContainText(
    "Retrieval score -0.120",
  );
  expect(state.searchRequests.at(-1)?.score_threshold).toBe(0);

  // A strict threshold filters everything: the page must land in the explicit
  // empty state rather than an error or stale results.
  await page.getByLabel("Score threshold").fill("0.95");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("knowledge-search-empty")).toBeVisible();
  await expect(page.getByTestId("knowledge-search-results")).toHaveCount(0);
  expect(state.searchRequests.at(-1)?.score_threshold).toBe(0.95);
});

test("deletes a base, polls the deleting status away, and parks delete failures with the reason", async ({
  page,
}) => {
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: "40000000-0000-4000-8000-000000000001",
        name: "临时资料",
        description: "",
        status: "active",
        document_count: 0,
        delete_error: null,
      },
      {
        id: "40000000-0000-4000-8000-000000000002",
        name: "stuck-资料库",
        description: "",
        status: "active",
        document_count: 0,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  const baseList = page.getByTestId("knowledge-base-list");

  // Clean delete: the deleting row disappears after polling.
  const cleanRow = baseList
    .getByRole("listitem")
    .filter({ hasText: "临时资料" });
  await cleanRow.getByRole("button", { name: "Delete", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(baseList.getByText("临时资料")).toHaveCount(0, {
    timeout: 15_000,
  });

  // Failed delete: the row parks with the recorded reason and can be retried.
  const stuckRow = baseList
    .getByRole("listitem")
    .filter({ hasText: "stuck-资料库" });
  await stuckRow.getByRole("button", { name: "Delete", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(
    stuckRow.getByText("MinIO 对象删除失败", { exact: false }),
  ).toBeVisible({
    timeout: 15_000,
  });
});

test("read-only members see lists but no write controls", async ({ page }) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    capabilities: READ_CAPABILITIES,
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000001",
        knowledge_base_id: BASE_ID,
        name: "guide.txt",
        original_name: "guide.txt",
        status: "ready",
        segment_count: 2,
        error_message: null,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");

  await expect(
    page.getByTestId("knowledge-base-list").getByText("产品手册"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "New base" })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Edit", exact: true }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "View documents" }).click();
  await expect(
    page.getByRole("button", { name: "Upload document" }),
  ).toHaveCount(0);

  // No selection column, and the enabled state renders as a locked switch.
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("guide.txt")).toBeVisible();
  await expect(rows.getByRole("checkbox")).toHaveCount(0);
  await expect(rows.getByRole("switch")).toBeDisabled();
  const readOnlyMenu = await openDocumentActions(page, "guide.txt");
  await expect(
    readOnlyMenu.getByRole("menuitem", { name: "Rename" }),
  ).toHaveCount(0);
  await expect(
    readOnlyMenu.getByRole("menuitem", { name: "Delete" }),
  ).toHaveCount(0);

  // The segment browser is reachable but exposes no mutation controls.
  await readOnlyMenu.getByRole("menuitem", { name: "View segments" }).click();
  const browser = page.getByTestId("knowledge-segment-browser");
  await expect(
    browser.getByTestId("knowledge-segment-list").getByText("分段 1 的内容"),
  ).toBeVisible();
  await expect(
    browser.getByRole("button", { name: "Add segment" }),
  ).toHaveCount(0);
  await expect(browser.getByRole("switch")).toHaveCount(0);
  await expect(browser.getByRole("button", { name: "Edit" })).toHaveCount(0);
  await expect(browser.getByRole("button", { name: "Delete" })).toHaveCount(0);
  await expect(page.getByTestId("knowledge-create-wizard")).toHaveCount(0);
  expect(state.baseCreates).toHaveLength(0);
  expect(state.baseUpdates).toHaveLength(0);
  expect(state.uploadRequests).toHaveLength(0);
});

test("toggles a document's retrieval switch and renames it from the list", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000001",
        knowledge_base_id: BASE_ID,
        name: "guide.txt",
        original_name: "guide.txt",
        status: "ready",
        segment_count: 3,
        word_count: 1500,
        error_message: null,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  // The characters column renders the aggregated word count.
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("1,500")).toBeVisible();

  // Disabling flips the switch state after the server confirms.
  await rows.getByRole("switch", { name: "Disable guide.txt" }).click();
  await expect(
    rows.getByRole("switch", { name: "Enable guide.txt" }),
  ).toBeVisible();

  // Rename keeps the original file name visible as secondary text.
  await (await openDocumentActions(page, "guide.txt"))
    .getByRole("menuitem", { name: "Rename" })
    .click();
  await page.getByLabel("Display name").fill("规范手册");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Save", exact: true })
    .click();
  await expect(rows.getByText("规范手册")).toBeVisible();
  await expect(rows.getByText("guide.txt")).toBeVisible();
});

test("batch selection disables, re-enables, and deletes documents", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const documents: MockDocument[] = [1, 2].map((index) => ({
    id: `50000000-0000-4000-8000-00000000000${index}`,
    knowledge_base_id: BASE_ID,
    name: `doc-${index}.txt`,
    original_name: `doc-${index}.txt`,
    status: "ready",
    segment_count: 2,
    error_message: null,
    delete_error: null,
  }));
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: documents.length,
        delete_error: null,
      },
    ],
    documents,
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("doc-1.txt")).toBeVisible();

  // Select-all surfaces the batch bar with the selection count.
  await page.getByRole("checkbox", { name: "Select all documents" }).check();
  const batchBar = page.getByTestId("knowledge-batch-bar");
  await expect(batchBar.getByText("2 documents selected")).toBeVisible();

  // Batch disable flips every switch; the selection survives for re-enable.
  await batchBar.getByRole("button", { name: "Disable", exact: true }).click();
  await expect(
    rows.getByRole("switch", { name: "Enable doc-1.txt" }),
  ).toBeVisible();
  await expect(
    rows.getByRole("switch", { name: "Enable doc-2.txt" }),
  ).toBeVisible();
  await batchBar.getByRole("button", { name: "Enable", exact: true }).click();
  await expect(
    rows.getByRole("switch", { name: "Disable doc-1.txt" }),
  ).toBeVisible();

  // Batch delete confirms once and removes every selected row.
  await batchBar.getByRole("button", { name: "Delete", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByText("This deletes 2 documents", { exact: false }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(
    page.getByText("No documents yet", { exact: false }),
  ).toBeVisible();
});

test("wizard parent-child mode nests preview children and freezes child params on upload", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "Create from documents" }).click();

  await page.getByLabel("File").setInputFiles({
    name: "manual.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("父子分块验收文档内容"),
  });
  await page.getByRole("button", { name: "Next" }).click();

  // General mode renders a flat preview without child chips.
  const previewPanel = page.getByTestId("chunk-preview-panel");
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=\\n\\n"),
  ).toBeVisible();
  await expect(previewPanel.getByText(/child chunks/u)).toHaveCount(0);
  expect(state.previewRequests).toHaveLength(1);

  // Switching to parent-child reveals the child parameters with defaults.
  await page.getByRole("radio", { name: /Parent-child/u }).check();
  const childSize = page.getByLabel("Child chunk size (characters)");
  await expect(childSize).toHaveValue("500");
  await expect(page.getByLabel("Child delimiter")).toHaveValue("\\n");

  // Mode changes mark the old preview stale without uploading the file again.
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toBeVisible();
  expect(state.previewRequests).toHaveLength(1);
  await previewPanel.getByRole("button", { name: "Refresh preview" }).click();

  // Explicit refresh nests children per parent.
  await expect(
    previewPanel.getByText("父块1子块一 child=500 csep=\\n"),
  ).toBeVisible();
  await expect(
    previewPanel.getByText("2 child chunks · ", { exact: false }).first(),
  ).toBeVisible();

  // Tuning the child size also waits for an explicit refresh.
  await childSize.fill("300");
  await expect(
    previewPanel.getByText(
      "Preview is out of date. Refresh to apply the current settings.",
    ),
  ).toBeVisible();
  expect(state.previewRequests).toHaveLength(2);
  await previewPanel.getByRole("button", { name: "Refresh preview" }).click();
  await expect(
    previewPanel.getByText("父块1子块一 child=300 csep=\\n"),
  ).toBeVisible();
  expect(state.previewRequests.at(-1)).toMatchObject({
    chunking_mode: "parent_child",
    child_chunk_size: "300",
    child_chunk_separator: "\\n",
  });

  await page.getByLabel("Name").fill("父子手册");
  await page.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  await page.getByRole("button", { name: "Save & process" }).click();

  // The summary reports the mode and the child parameters.
  await expect(page.getByText("Knowledge base created")).toBeVisible();
  await expect(page.getByText("Chunking mode")).toBeVisible();
  await expect(page.getByText("Parent-child", { exact: true })).toBeVisible();
  await expect(page.getByText("Child chunk size (characters)")).toBeVisible();

  // The upload froze the parent-child parameters.
  expect(state.documents.at(-1)?.chunking_mode).toBe("parent_child");
  expect(state.documents.at(-1)?.child_chunk_size).toBe(300);
  expect(state.documents.at(-1)?.child_chunk_separator).toBe("\\n");
});

test("an existing configured base uploads through the wizard without changing its configuration", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "Existing base description",
        status: "active",
        document_count: 1,
        delete_error: null,
        embedding_model_id: MODEL_ID,
        reranker_model_id: RERANK_MODEL_ID,
        retrieval_mode: "hybrid",
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000099",
        knowledge_base_id: BASE_ID,
        name: "guide.txt",
        original_name: "guide.txt",
        status: "ready",
        segment_count: 2,
        error_message: null,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Upload document" }).click();
  const wizard = page.getByTestId("knowledge-create-wizard");
  await expect(wizard).toBeVisible();
  await expect(
    page.getByRole("dialog", { name: "Upload document", exact: true }),
  ).toHaveCount(0);
  await wizard.getByLabel("File", { exact: true }).setInputFiles({
    name: "guide.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("已有知识库通过完整向导上传父子分块内容"),
  });
  await wizard.getByRole("button", { name: "Next", exact: true }).click();
  const preview = wizard.getByTestId("chunk-preview-panel");
  await expect(preview).toContainText("Previewing: guide.txt");

  // Existing library settings are informational, while the single document's
  // optional display name remains editable and is a multipart file property.
  await expect(wizard.getByLabel("Name", { exact: true })).toHaveCount(0);
  await expect(wizard.getByLabel("Description", { exact: true })).toHaveCount(
    0,
  );
  await expect(
    wizard.getByRole("combobox", { name: "Embedding model", exact: true }),
  ).toHaveCount(0);
  await expect(
    wizard.getByRole("combobox", { name: "Reranker model", exact: true }),
  ).toHaveCount(0);
  await expect(
    wizard.getByRole("radiogroup", { name: "Default retrieval route" }),
  ).toHaveCount(0);
  await wizard
    .getByLabel("Display name (optional)", { exact: true })
    .fill("Uploaded guide");
  await wizard
    .getByRole("radio", { name: /Parent-child/u })
    .check();
  await wizard
    .getByLabel("Child chunk size (characters)", { exact: true })
    .fill("200");
  await preview.getByRole("button", { name: "Refresh preview" }).click();
  await expect(
    preview.getByText("父块1子块一 child=200 csep=\\n"),
  ).toBeVisible();
  await wizard
    .getByRole("button", { name: "Upload & process", exact: true })
    .click();

  await expect(
    wizard.getByRole("heading", { name: "Processing documents", exact: true }),
  ).toBeVisible();
  const statusList = wizard.getByTestId("wizard-document-status");
  await expect(statusList.getByText("handbook-1.txt")).toBeVisible();
  // The old document shares the selected filename. Only returned upload IDs,
  // not all base documents or filename matches, belong to this batch.
  await expect(statusList.getByRole("listitem")).toHaveCount(1);
  await expect(statusList.getByText("guide.txt", { exact: true })).toHaveCount(
    0,
  );
  expect(state.baseCreates).toHaveLength(0);
  expect(state.baseUpdates).toHaveLength(0);
  expect(state.uploadRequests).toEqual([
    { baseId: BASE_ID, fileName: "guide.txt", displayName: "Uploaded guide" },
  ]);
  expect(state.documents.at(-1)).toMatchObject({
    knowledge_base_id: BASE_ID,
    chunking_mode: "parent_child",
    child_chunk_size: 200,
    child_chunk_separator: "\\n",
  });
  expect(state.bases[0]).toMatchObject({
    name: "产品手册",
    description: "Existing base description",
    embedding_model_id: MODEL_ID,
    reranker_model_id: RERANK_MODEL_ID,
    retrieval_mode: "hybrid",
  });
  await wizard.getByRole("button", { name: "Go to documents" }).click();
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("guide.txt", { exact: true })).toBeVisible();
  await expect(rows.getByText("handbook-1.txt")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`kb=${BASE_ID}`));
});

test("cancelling an existing-base upload returns to the original documents view without mutations", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "Keep this base",
        description: "",
        status: "active",
        document_count: 0,
        delete_error: null,
      },
    ],
  });
  await page.goto(`/projects/alpha/knowledge?kb=${BASE_ID}`);
  await expect(
    page.getByRole("button", { name: "Upload document" }),
  ).toBeVisible();
  const originalUrl = page.url();
  const wizard = page.getByTestId("knowledge-create-wizard");

  // Both file selection and parameter editing can exit without creating or
  // changing a library. Back returns to the same base, never the base catalog.
  await page.getByRole("button", { name: "Upload document" }).click();
  await wizard.getByRole("button", { name: "Back", exact: true }).click();
  await expect(wizard).toHaveCount(0);
  await expect(page).toHaveURL(originalUrl);
  await expect(
    page.getByRole("button", { name: "Upload document" }),
  ).toBeVisible();

  await page.getByRole("button", { name: "Upload document" }).click();
  await wizard.getByLabel("File", { exact: true }).setInputFiles({
    name: "cancelled.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("This file must not be admitted after cancelling"),
  });
  await wizard.getByRole("button", { name: "Next", exact: true }).click();
  await expect(wizard.getByTestId("chunk-preview-panel")).toContainText(
    "Previewing: cancelled.txt",
  );
  await wizard
    .getByLabel("Chunk size (characters)", { exact: true })
    .fill("1200");
  await wizard.getByRole("button", { name: "Back", exact: true }).click();
  await expect(wizard).toHaveCount(0);
  await expect(page).toHaveURL(originalUrl);
  await expect(
    page.getByRole("button", { name: "Upload document" }),
  ).toBeVisible();
  expect(state.baseCreates).toHaveLength(0);
  expect(state.baseUpdates).toHaveLength(0);
  expect(state.uploadRequests).toHaveLength(0);
  expect(state.documents).toHaveLength(0);
});

test("browser back clears an existing-base upload before reopening the same base", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "Return to base A",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000099",
        knowledge_base_id: BASE_ID,
        name: "existing-guide.txt",
        original_name: "existing-guide.txt",
        status: "ready",
        segment_count: 2,
        error_message: null,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await expect(page).toHaveURL(new RegExp(`kb=${BASE_ID}`));
  await page.getByRole("button", { name: "Upload document" }).click();
  await expect(page.getByTestId("knowledge-create-wizard")).toBeVisible();

  await page.goBack();
  await expect(page).not.toHaveURL(/kb=/u);
  await expect(page.getByTestId("knowledge-base-list")).toBeVisible();
  await expect(page.getByTestId("knowledge-create-wizard")).toHaveCount(0);

  await page.getByRole("button", { name: "View documents" }).click();
  await expect(page).toHaveURL(new RegExp(`kb=${BASE_ID}`));
  await expect(
    page.getByTestId("knowledge-document-rows").getByText("existing-guide.txt"),
  ).toBeVisible();
  await expect(page.getByTestId("knowledge-create-wizard")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Upload document" }),
  ).toBeVisible();
  expect(state.baseCreates).toHaveLength(0);
  expect(state.baseUpdates).toHaveLength(0);
  expect(state.uploadRequests).toHaveLength(0);
});

for (const needsInitialConfiguration of [false, true]) {
  test(`existing-base wizard retries only failed files without repeating ${needsInitialConfiguration ? "initial configuration" : "base mutations"}`, async ({
    page,
  }) => {
    const BASE_ID = "40000000-0000-4000-8000-000000000001";
    const state = await mockKnowledgeRoutes(page, {
      bases: [
        {
          id: BASE_ID,
          name: "产品手册",
          description: "",
          status: "active",
          document_count: 0,
          delete_error: null,
          embedding_model_id: needsInitialConfiguration ? null : MODEL_ID,
        },
      ],
    });
    await page.goto("/projects/alpha/knowledge");
    await page.getByRole("button", { name: "View documents" }).click();
    await page.getByRole("button", { name: "Upload document" }).click();
    const wizard = page.getByTestId("knowledge-create-wizard");
    await wizard.getByLabel("File", { exact: true }).setInputFiles([
      {
        name: "ok.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("正常内容"),
      },
      {
        name: "reject-a.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("超配额 A"),
      },
      {
        name: "reject-b.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("超配额 B"),
      },
    ]);
    await wizard.getByRole("button", { name: "Next", exact: true }).click();
    await expect(wizard.getByTestId("chunk-preview-panel")).toBeVisible();
    await expect(
      wizard.getByLabel("Display name (optional)", { exact: true }),
    ).toHaveCount(0);
    if (needsInitialConfiguration) {
      await wizard.getByLabel("Embedding model").click();
      await page
        .getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" })
        .click();
    }
    await wizard
      .getByRole("button", { name: "Upload & process", exact: true })
      .click();

    const statusList = wizard.getByTestId("wizard-document-status");
    await expect(statusList.getByText("handbook-1.txt")).toBeVisible();
    await expect(wizard.getByText("reject-a.txt: 文件超出配额")).toBeVisible();
    await expect(wizard.getByText("reject-b.txt: 文件超出配额")).toBeVisible();
    await expect(
      wizard.getByRole("button", { name: "Retry failed uploads", exact: true }),
    ).toBeEnabled();
    expect(state.uploadRequests.map((request) => request.fileName)).toEqual([
      "ok.txt",
      "reject-a.txt",
      "reject-b.txt",
    ]);
    expect(state.baseCreates).toHaveLength(0);
    expect(state.baseUpdates).toHaveLength(needsInitialConfiguration ? 1 : 0);

    // An unsuccessful retry resends exactly the failures and retains the
    // successful document in the processing list.
    await wizard
      .getByRole("button", { name: "Retry failed uploads", exact: true })
      .click();
    await expect.poll(() => state.uploadRequests.length).toBe(5);
    await expect(
      wizard.getByRole("button", { name: "Retry failed uploads", exact: true }),
    ).toBeEnabled();
    expect(
      state.uploadRequests.slice(3).map((request) => request.fileName),
    ).toEqual(["reject-a.txt", "reject-b.txt"]);
    expect(state.uploadCounter).toBe(1);
    await expect(statusList.getByText("handbook-1.txt")).toBeVisible();

    state.acceptRejectedUploads = true;
    await wizard
      .getByRole("button", { name: "Retry failed uploads", exact: true })
      .click();
    await expect(statusList.getByRole("listitem")).toHaveCount(3);
    await expect(statusList.getByText("handbook-3.txt")).toBeVisible();
    await expect(wizard.getByText("reject-a.txt: 文件超出配额")).toHaveCount(0);
    await expect(wizard.getByText("reject-b.txt: 文件超出配额")).toHaveCount(0);
    expect(
      state.uploadRequests.slice(5).map((request) => request.fileName),
    ).toEqual(["reject-a.txt", "reject-b.txt"]);
    expect(state.uploadCounter).toBe(3);
    expect(
      state.uploadRequests.every((request) => request.baseId === BASE_ID),
    ).toBe(true);
    expect(state.baseCreates).toHaveLength(0);
    expect(state.baseUpdates).toHaveLength(needsInitialConfiguration ? 1 : 0);
    if (needsInitialConfiguration) {
      expect(state.baseUpdates[0]).toEqual({
        embedding_model_id: MODEL_ID,
        retrieval_mode: "semantic",
      });
    }
    await wizard.getByRole("button", { name: "Go to documents" }).click();
    const rows = page.getByTestId("knowledge-document-rows");
    await expect(rows.getByText("handbook-1.txt")).toBeVisible();
    await expect(rows.getByText("handbook-2.txt")).toBeVisible();
    await expect(rows.getByText("handbook-3.txt")).toBeVisible();
  });
}

test("retrieval test lists recent queries, refreshes after a search, and backfills on click", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    queries: [
      {
        id: "70000000-0000-4000-8000-000000000091",
        query: "agent 侧历史查询",
        source: "agent",
        result_count: 3,
        top_score: 0.87,
      },
      {
        id: "70000000-0000-4000-8000-000000000092",
        query: "零结果历史查询",
        source: "retrieval_test",
        result_count: 0,
        top_score: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Retrieval test" }).click();

  // The seeded log renders with source labels and a dash for null scores.
  const recent = page.getByTestId("knowledge-recent-queries");
  await expect(recent.getByText("agent 侧历史查询")).toBeVisible();
  await expect(recent.getByText("Agent call")).toBeVisible();
  await expect(recent.getByText("0.870")).toBeVisible();
  const emptyRow = recent
    .getByRole("row")
    .filter({ hasText: "零结果历史查询" });
  await expect(emptyRow.getByText("Retrieval test")).toBeVisible();
  await expect(emptyRow.getByText("—")).toBeVisible();

  // A finished search appends its own log row at the top.
  await page.getByLabel("Query").fill("发布流程");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("knowledge-search-results")).toBeVisible();
  await expect(recent.getByRole("row").first()).toContainText("发布流程");
  await expect(recent.getByRole("row").first()).toContainText("0.930");

  // Clicking a logged query backfills the input for a re-run.
  await recent.getByRole("button", { name: "agent 侧历史查询" }).click();
  await expect(page.getByLabel("Query")).toHaveValue("agent 侧历史查询");
});

test("base settings save retrieval defaults and empty search inputs defer to them", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  // The search inputs surface the current defaults as placeholders.
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await expect(page.getByLabel("Results (top_k)")).toHaveAttribute(
    "placeholder",
    "4",
  );
  await expect(page.getByLabel("Score threshold")).toHaveAttribute(
    "placeholder",
    "0",
  );

  // Saving new defaults goes through the base PATCH route.
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Default results (top_k)").fill("6");
  await page.getByLabel("Default score threshold").fill("2");
  await page.getByLabel("Default score threshold").press("Enter");
  expect(state.baseUpdates).toHaveLength(0);
  await expect(
    page.getByRole("button", { name: "Save", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Default score threshold").fill("0.3");
  // The external field remains associated with the settings form.
  await page.getByLabel("Default score threshold").press("Enter");
  await expect(page.getByText("Saved.")).toBeVisible();
  expect(state.baseUpdates.at(-1)).toMatchObject({
    default_top_k: 6,
    default_score_threshold: 0.3,
  });

  // The retrieval test reflects the saved defaults and, with both inputs
  // empty, omits them from the request so the backend resolves the defaults.
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await expect(page.getByLabel("Results (top_k)")).toHaveAttribute(
    "placeholder",
    "6",
  );
  await expect(page.getByLabel("Score threshold")).toHaveAttribute(
    "placeholder",
    "0.3",
  );
  await page.getByLabel("Query").fill("默认参数查询");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("knowledge-search-results")).toBeVisible();
  const lastSearch = state.searchRequests.at(-1)!;
  expect(lastSearch.query).toBe("默认参数查询");
  expect(lastSearch).not.toHaveProperty("top_k");
  expect(lastSearch).not.toHaveProperty("score_threshold");
});

test("base settings bind and clear the optional reranker without rebuilding, dropping stale results", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  // Search first: these results must not survive a reranker change.
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("发布流程");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("knowledge-search-results")).toBeVisible();

  // Binding a reranker saves through the base PATCH route: effective
  // immediately, no rebuild, and the embedding binding stays untouched.
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Reranker model").click();
  await page
    .getByRole("option", { name: "SiliconFlow · BAAI/bge-reranker-v2-m3" })
    .click();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  expect(state.baseUpdates.at(-1)).toMatchObject({
    reranker_model_id: RERANK_MODEL_ID,
  });
  expect(state.baseUpdates.at(-1)).not.toHaveProperty("clear_reranker_model");
  expect(state.baseUpdates.at(-1)).not.toHaveProperty("embedding_model_id");
  expect(state.rebuildRequests).toHaveLength(0);

  // The old results are gone; a fresh search works under the new binding.
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await expect(page.getByTestId("knowledge-search-results")).toHaveCount(0);
  await page.getByLabel("Query").fill("发布流程");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("knowledge-search-results")).toBeVisible();

  // "No reranking" is an explicit clear, not an omitted field.
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Reranker model").click();
  await page.getByRole("option", { name: "No reranking" }).click();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved.")).toBeVisible();
  expect(state.baseUpdates.at(-1)).toMatchObject({
    clear_reranker_model: true,
  });
  expect(state.baseUpdates.at(-1)).not.toHaveProperty("reranker_model_id");

  // Retrieval still returns results with reranking off.
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await expect(page.getByTestId("knowledge-search-results")).toHaveCount(0);
  await page.getByLabel("Query").fill("发布流程");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("knowledge-search-results")).toBeVisible();
});

test("segment browser edits, toggles, adds, and deletes segments", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const DOC_ID = "50000000-0000-4000-8000-000000000001";
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: DOC_ID,
        knowledge_base_id: BASE_ID,
        name: "guide.txt",
        original_name: "guide.txt",
        status: "ready",
        segment_count: 2,
        word_count: 12,
        error_message: null,
        delete_error: null,
      },
    ],
    segments: {
      [DOC_ID]: [
        {
          id: "60000000-0000-4000-8000-000000000001",
          position: 1,
          content: "第一段原始内容",
          enabled: true,
          source_position: { page: 1 },
        },
        {
          id: "60000000-0000-4000-8000-000000000002",
          position: 2,
          content: "第二段内容",
          enabled: true,
          source_position: { page: 2 },
        },
      ],
    },
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await (await openDocumentActions(page, "guide.txt"))
    .getByRole("menuitem", { name: "View segments" })
    .click();

  const browser = page.getByTestId("knowledge-segment-browser");
  const list = browser.getByTestId("knowledge-segment-list");
  await expect(browser.getByText("2 segments · 12 characters")).toBeVisible();

  // Editing re-embeds server-side and updates the aggregated counts.
  const first = list.getByRole("listitem").filter({ hasText: "第一段" });
  await first.getByRole("button", { name: "View full content" }).click();
  await expect(page.getByRole("dialog")).toContainText("第一段原始内容");
  await page.getByRole("dialog").press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await first.getByRole("button", { name: "Edit" }).click();
  await page.getByLabel("Content").fill("第一段修改后的内容");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Save", exact: true })
    .click();
  await expect(list.getByText("第一段修改后的内容")).toBeVisible();
  await expect(browser.getByText("2 segments · 14 characters")).toBeVisible();

  // Disabling a segment marks it without removing it.
  await list.getByRole("switch", { name: "Disable segment #2" }).click();
  const second = list.getByRole("listitem").filter({ hasText: "第二段" });
  await expect(second.getByText("Disabled")).toBeVisible();
  await expect(
    list.getByRole("switch", { name: "Enable segment #2" }),
  ).toBeVisible();

  // A manual segment appends at the end with the manual badge.
  await browser.getByRole("button", { name: "Add segment" }).click();
  await page.getByLabel("Content").fill("手工补充的分段");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Save", exact: true })
    .click();
  const added = list.getByRole("listitem").filter({ hasText: "手工补充" });
  await expect(added.getByText("Segment #3")).toBeVisible();
  await expect(added.getByText("Manual")).toBeVisible();
  await expect(browser.getByText("3 segments · 21 characters")).toBeVisible();

  // Deleting a segment updates the counts and the list.
  await added.getByRole("button", { name: "Delete" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(list.getByText("手工补充的分段")).toHaveCount(0);
  await expect(browser.getByText("2 segments · 14 characters")).toBeVisible();
});

test("metadata panel adds, renames, and deletes fields and surfaces name conflicts", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 0,
        delete_error: null,
      },
    ],
    metadataFields: [
      {
        id: "80000000-0000-4000-8000-000000000091",
        knowledge_base_id: BASE_ID,
        name: "author",
        field_type: "string",
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page
    .getByRole("navigation", { name: "Knowledge base sections" })
    .getByRole("button", { name: "Metadata" })
    .click();

  // The seeded field renders with its localized type.
  const rows = page.getByTestId("knowledge-metadata-field-rows");
  await expect(rows.getByText("author")).toBeVisible();
  await expect(rows.getByText("Text")).toBeVisible();

  // A duplicate name surfaces the backend conflict inside the dialog.
  await page.getByRole("button", { name: "Add field" }).click();
  const addDialog = page.getByRole("dialog");
  await addDialog.getByLabel("Field name").fill("author");
  await addDialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(
    addDialog.getByText("同一 Knowledge Base 内已存在同名元数据字段"),
  ).toBeVisible();

  // Correcting the name and picking a type creates the field.
  await addDialog.getByLabel("Field name").fill("priority");
  await addDialog.getByLabel("Field type").click();
  await page.getByRole("option", { name: "Number" }).click();
  await addDialog.getByRole("button", { name: "Create", exact: true }).click();
  const priorityRow = rows.getByRole("row").filter({ hasText: "priority" });
  await expect(priorityRow.getByText("Number")).toBeVisible();

  // Renaming rewrites the definition in place.
  const authorRow = rows.getByRole("row").filter({ hasText: "author" });
  await authorRow.getByRole("button", { name: "Rename" }).click();
  await page.getByRole("dialog").getByLabel("Field name").fill("作者");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Save", exact: true })
    .click();
  await expect(rows.getByText("作者")).toBeVisible();
  await expect(rows.getByText("author")).toHaveCount(0);

  // Deleting removes the row after the confirm step.
  await priorityRow.getByRole("button", { name: "Delete" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(rows.getByText("priority")).toHaveCount(0);
});

test("document metadata dialog saves typed values and clears emptied fields", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const DOC_ID = "50000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: DOC_ID,
        knowledge_base_id: BASE_ID,
        name: "guide.txt",
        original_name: "guide.txt",
        status: "ready",
        segment_count: 2,
        doc_metadata: { author: "旧作者" },
        error_message: null,
        delete_error: null,
      },
    ],
    metadataFields: [
      {
        id: "80000000-0000-4000-8000-000000000091",
        knowledge_base_id: BASE_ID,
        name: "author",
        field_type: "string",
      },
      {
        id: "80000000-0000-4000-8000-000000000092",
        knowledge_base_id: BASE_ID,
        name: "priority",
        field_type: "number",
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  await (await openDocumentActions(page, "guide.txt"))
    .getByRole("menuitem", { name: "Metadata" })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Edit metadata · guide.txt")).toBeVisible();

  // Existing values prefill; numbers convert on save.
  const authorInput = dialog.getByLabel(/author/u);
  await expect(authorInput).toHaveValue("旧作者");
  await authorInput.fill("张三");
  await dialog.getByLabel(/priority/u).fill("5");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.metadataUpdates.at(-1)).toEqual({
    values: { author: "张三", priority: 5 },
  });

  // Emptying a stored value sends an explicit null and leaves the rest alone.
  await (await openDocumentActions(page, "guide.txt"))
    .getByRole("menuitem", { name: "Metadata" })
    .click();
  const reopened = page.getByRole("dialog");
  await expect(reopened.getByLabel(/priority/u)).toHaveValue("5");
  await reopened.getByLabel(/author/u).fill("");
  await reopened.getByRole("button", { name: "Save", exact: true }).click();
  await expect(reopened).toHaveCount(0);
  expect(state.metadataUpdates.at(-1)).toEqual({
    values: { author: null },
  });
});

test("retrieval test builds metadata filter conditions into the search request", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    metadataFields: [
      {
        id: "80000000-0000-4000-8000-000000000091",
        knowledge_base_id: BASE_ID,
        name: "author",
        field_type: "string",
      },
      {
        id: "80000000-0000-4000-8000-000000000092",
        knowledge_base_id: BASE_ID,
        name: "priority",
        field_type: "number",
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Retrieval test" }).click();

  // First condition: string field with the contains operator.
  await page.getByRole("button", { name: "Add condition" }).click();
  await page.getByLabel("Condition 1 operator").click();
  await page.getByRole("option", { name: "contains" }).click();
  await page.getByLabel("Condition 1 value").fill("白皮书");

  // Second condition: switching to the number field resets the operator set.
  await page.getByRole("button", { name: "Add condition" }).click();
  await page.getByLabel("Condition 2 field").click();
  await page.getByRole("option", { name: "priority" }).click();
  await page.getByLabel("Condition 2 operator").click();
  await page.getByRole("option", { name: "≥" }).click();
  await page.getByLabel("Condition 2 value").fill("3");

  await page.getByLabel("Query").fill("发布流程");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByTestId("knowledge-search-results")).toBeVisible();
  expect(state.searchRequests.at(-1)?.metadata_filters).toEqual([
    { name: "author", operator: "contains", value: "白皮书" },
    { name: "priority", operator: "gte", value: 3 },
  ]);

  // Removing every condition drops the key from the next request entirely.
  await page.getByRole("button", { name: "Remove condition 2" }).click();
  await page.getByRole("button", { name: "Remove condition 1" }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect.poll(() => state.searchRequests.length).toBe(2);
  expect(state.searchRequests.at(-1)).not.toHaveProperty("metadata_filters");
});

test("settings rebuild confirms, posts the configuration, and documents reprocess", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 2,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000001",
        knowledge_base_id: BASE_ID,
        name: "guide.txt",
        original_name: "guide.txt",
        status: "ready",
        segment_count: 3,
        error_message: null,
        delete_error: null,
      },
      {
        // Never published (failed before its first publish): re-embedding
        // skips it and the outcome must say so instead of claiming success.
        id: "50000000-0000-4000-8000-000000000002",
        knowledge_base_id: BASE_ID,
        name: "never-published.txt",
        original_name: "never-published.txt",
        status: "failed",
        segment_count: 0,
        error_message: "Embedding 请求连续失败已耗尽重试",
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Settings" }).click();

  // The re-embed block sits under the settings form with its own confirm.
  const rebuildSection = page.getByRole("region", { name: "Embedding model" });
  await expect(rebuildSection).toBeVisible();
  await rebuildSection.getByLabel("Embedding model").click();
  await page.getByRole("option", { name: "SiliconFlow · BAAI/bge-m3" }).click();
  await rebuildSection
    .getByRole("button", { name: "Re-embed documents" })
    .click();
  const confirm = page.getByRole("dialog");
  await expect(
    confirm.getByText("Re-embed knowledge base documents"),
  ).toBeVisible();
  // The confirmation states what is preserved: text, edits, enabled states.
  await expect(
    confirm.getByText(/manual edits, and enabled states are preserved/i),
  ).toBeVisible();
  await confirm.getByRole("button", { name: "Re-embed", exact: true }).click();
  // The admission outcome reports real counts: accepted and skipped.
  await expect(page.getByTestId("knowledge-rebuild-outcome")).toHaveText(
    "Re-embedding accepted for 1 documents; 1 never-published documents were skipped (retry them to parse from the original file).",
  );
  expect(state.rebuildRequests.at(-1)).toEqual({
    embedding_model_id: MODEL_ID,
  });

  // The published document re-queues and walks back to ready on subsequent
  // polls; the never-published one stays failed rather than pretending.
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("guide.txt")).toBeVisible();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 15_000 });
  await expect(
    rows.getByRole("row").filter({ hasText: "never-published.txt" }),
  ).toContainText("Failed");
});

test("the existing-base upload wizard accepts the K4 document formats", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 0,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Upload document" }).click();

  const wizard = page.getByTestId("knowledge-create-wizard");
  await expect(wizard).toBeVisible();
  await expect(wizard.getByText(/HTML, PPTX, and EPUB/u)).toBeVisible();
  await expect(wizard.getByLabel("File")).toHaveAttribute(
    "accept",
    ".pdf,.docx,.txt,.md,.csv,.xlsx,.html,.htm,.pptx,.epub",
  );
});

// ---------------------------------------------------------------------------
// T9: URL state, document search, and pagination
// ---------------------------------------------------------------------------

const T9_BASE_ID = "40000000-0000-4000-8000-000000000001";

function t9Base(overrides: Partial<MockBase> = {}): MockBase {
  return {
    id: T9_BASE_ID,
    name: "产品手册",
    description: "",
    status: "active",
    document_count: 0,
    delete_error: null,
    ...overrides,
  };
}

function t9Documents(count: number): MockDocument[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `50000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
    knowledge_base_id: T9_BASE_ID,
    name: `doc-${String(index + 1).padStart(3, "0")}.txt`,
    original_name: `doc-${String(index + 1).padStart(3, "0")}.txt`,
    status: "ready",
    segment_count: 3,
    error_message: null,
    delete_error: null,
  }));
}

test("the URL carries base, view, and document state through reload and history", async ({
  page,
}) => {
  await mockKnowledgeRoutes(page, {
    bases: [t9Base({ document_count: 3 })],
    documents: t9Documents(3),
  });
  await page.goto("/projects/alpha/knowledge");

  // Opening a base pushes kb= into the URL.
  await page.getByRole("button", { name: "View documents" }).click();
  await expect(page).toHaveURL(new RegExp(`kb=${T9_BASE_ID}`));
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("doc-001.txt")).toBeVisible();

  // Switching sections rewrites view=.
  await page.getByRole("button", { name: "Settings" }).click();
  await expect(page).toHaveURL(/view=settings/u);
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  await expect(page).not.toHaveURL(/view=/u);

  // Opening the segment browser pushes doc=.
  const menu = await openDocumentActions(page, "doc-002.txt");
  await menu.getByRole("menuitem", { name: "View segments" }).click();
  await expect(page.getByTestId("knowledge-segment-browser")).toBeVisible();
  await expect(page).toHaveURL(/doc=50000000-0000-4000-8000-000000000002/u);

  // A reload restores the same document from the URL alone.
  await page.reload();
  await expect(page.getByTestId("knowledge-segment-browser")).toBeVisible();
  await expect(page.getByText("分段 1 的内容")).toBeVisible();

  // Browser back retraces each pushed location: document → list →
  // settings → list → base overview.
  await page.goBack();
  await expect(page.getByTestId("knowledge-documents-table")).toBeVisible();
  await expect(page).not.toHaveURL(/doc=/u);
  await page.goBack();
  await expect(page).toHaveURL(/view=settings/u);
  await page.goBack();
  await page.goBack();
  await expect(page).not.toHaveURL(/kb=/u);
  await expect(
    page.getByRole("button", { name: "View documents" }),
  ).toBeVisible();
});

test("filters, sort, and paging work over the complete list and keep keywords out of the URL", async ({
  page,
}) => {
  // 130 documents force two backend pages (100 + 30); the filters must see
  // rows from both.
  const documents = t9Documents(130);
  documents[124]!.status = "failed";
  documents[124]!.error_message = "Embedding 失败";
  await mockKnowledgeRoutes(page, {
    bases: [t9Base({ document_count: documents.length })],
    documents,
  });
  await page.goto(`/projects/alpha/knowledge?kb=${T9_BASE_ID}`);

  const pageInfo = page.getByTestId("knowledge-documents-page-info");
  await expect(pageInfo).toHaveText("Page 1/7 · 130 documents");

  // Paging is a replace navigation on page=.
  await page.getByRole("button", { name: "Next" }).click();
  await expect(pageInfo).toHaveText("Page 2/7 · 130 documents");
  await expect(page).toHaveURL(/page=2/u);
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("doc-021.txt")).toBeVisible();

  // The keyword narrows across every backend page but never enters the URL;
  // applying it resets the page. "12" matches doc-012, doc-112, and
  // doc-120 … doc-129 — twelve rows, ten of them beyond backend page one.
  await page.getByRole("searchbox", { name: "Search documents" }).fill("12");
  await expect(pageInfo).toHaveText("Page 1/1 · 12 documents");
  await expect(rows.getByText("doc-120.txt")).toBeVisible();
  await expect(rows.getByText("doc-125.txt")).toBeVisible();
  expect(new URL(page.url()).search).not.toContain("12");
  await expect(page).not.toHaveURL(/page=/u);

  // The status filter is URL state and combines with the keyword: row 125
  // (doc-125) is the only failed document matching "12".
  await page.getByRole("combobox", { name: "Status" }).click();
  await page.getByRole("option", { name: "Failed" }).click();
  await expect(page).toHaveURL(/status=failed/u);
  await expect(pageInfo).toHaveText("Page 1/1 · 1 documents");
  await expect(rows.getByText("doc-125.txt")).toBeVisible();

  // Clearing the keyword keeps the failed filter over the complete list.
  await page.getByRole("searchbox", { name: "Search documents" }).fill("");
  await expect(pageInfo).toHaveText("Page 1/1 · 1 documents");

  // Sort is URL state too; name descending puts the tail first.
  await page.getByRole("combobox", { name: "Status" }).click();
  await page.getByRole("option", { name: "All statuses" }).click();
  await page.getByRole("combobox", { name: "Sort" }).click();
  await page.getByRole("option", { name: "Name descending" }).click();
  await expect(page).toHaveURL(/sort=name_desc/u);
  await expect(rows.getByText("doc-130.txt")).toBeVisible();

  // A reload restores the safe URL location (sort) with the keyword cleared.
  await page.getByRole("searchbox", { name: "Search documents" }).fill("12");
  await page.reload();
  await expect(page).toHaveURL(/sort=name_desc/u);
  await expect(
    page.getByRole("searchbox", { name: "Search documents" }),
  ).toHaveValue("");
  await expect(page.getByTestId("knowledge-documents-page-info")).toHaveText(
    "Page 1/7 · 130 documents",
  );
});

test("an incomplete document list is an explicit error, never a partial table", async ({
  page,
}) => {
  await mockKnowledgeRoutes(page, {
    bases: [t9Base({ document_count: 130 })],
    documents: t9Documents(130),
    documentListTruncated: true,
  });
  await page.goto(`/projects/alpha/knowledge?kb=${T9_BASE_ID}`);

  // The client saw 100 of 130 rows and a premature empty page: it must not
  // publish the partial list as if it were complete. The generous timeout
  // covers the query client's retry backoff before the error surfaces.
  await expect(
    page.getByText("The list did not load completely. Refresh to retry."),
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("knowledge-documents-table")).toHaveCount(0);
});

test("unknown base or document ids show inaccessible states instead of cached objects", async ({
  page,
}) => {
  await mockKnowledgeRoutes(page, {
    bases: [t9Base({ document_count: 1 })],
    documents: t9Documents(1),
  });

  // A base id that resolves nowhere is a dead end with a way back — never a
  // restored object.
  await page.goto(
    "/projects/alpha/knowledge?kb=44444444-0000-4000-8000-000000000404",
  );
  await expect(
    page.getByText("This knowledge base does not exist or is inaccessible."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back to knowledge bases" }).click();
  await expect(page.getByText("产品手册")).toBeVisible();

  // Same for a foreign document id inside an accessible base.
  await page.goto(
    `/projects/alpha/knowledge?kb=${T9_BASE_ID}&doc=55555555-0000-4000-8000-000000000404`,
  );
  await expect(
    page.getByText("This document does not exist or is inaccessible."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Back to documents" }).click();
  await expect(page.getByTestId("knowledge-documents-table")).toBeVisible();

  // A malformed kb is dropped by parsing: the base list renders directly.
  await page.goto("/projects/alpha/knowledge?kb=not-a-uuid&view=settings");
  await expect(
    page.getByRole("button", { name: "View documents" }),
  ).toBeVisible();
});

test("deleting the only row of the last page steps the pagination back", async ({
  page,
}) => {
  await mockKnowledgeRoutes(page, {
    bases: [t9Base({ document_count: 21 })],
    documents: t9Documents(21),
  });
  await page.goto(`/projects/alpha/knowledge?kb=${T9_BASE_ID}&page=2`);

  const pageInfo = page.getByTestId("knowledge-documents-page-info");
  await expect(pageInfo).toHaveText("Page 2/2 · 21 documents");
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("doc-021.txt")).toBeVisible();

  const menu = await openDocumentActions(page, "doc-021.txt");
  await menu.getByRole("menuitem", { name: "Delete" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();

  // The last page vanished with its only row; the URL walks back to page 1.
  await expect(pageInfo).toHaveText("Page 1/1 · 20 documents");
  await expect(page).not.toHaveURL(/page=/u);
  await expect(rows.getByText("doc-001.txt")).toBeVisible();
});

test("a segment URL locates through the detail endpoint and fails loudly for foreign ids", async ({
  page,
}) => {
  const documents = t9Documents(1);
  const documentId = documents[0]!.id;
  await mockKnowledgeRoutes(page, {
    bases: [t9Base({ document_count: 1 })],
    documents,
    segments: {
      [documentId]: [
        {
          id: "60000000-0000-4000-8000-000000000001",
          position: 1,
          content: "第一段内容",
          enabled: true,
          source_position: { page: 1 },
        },
        {
          id: "60000000-0000-4000-8000-000000000002",
          position: 2,
          content: "被定位的第二段内容",
          enabled: true,
          source_position: { page: 2 },
        },
      ],
    },
  });

  // The segment resolves through its detail read — the pinned card shows
  // the located content next to the regular list.
  await page.goto(
    `/projects/alpha/knowledge?kb=${T9_BASE_ID}&doc=${documentId}&segment=60000000-0000-4000-8000-000000000002`,
  );
  const locateCard = page.getByTestId("knowledge-segment-locate");
  await expect(locateCard.getByText("Located segment #2")).toBeVisible();
  await expect(locateCard.getByText("被定位的第二段内容")).toBeVisible();

  // Dismissing the card strips segment= without touching the rest.
  await locateCard.getByRole("button", { name: "Dismiss" }).click();
  await expect(page.getByTestId("knowledge-segment-locate")).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`doc=${documentId}`));
  await expect(page).not.toHaveURL(/segment=/u);

  // A segment id from another document answers 404: an explicit failure,
  // not a resurrected object.
  await page.goto(
    `/projects/alpha/knowledge?kb=${T9_BASE_ID}&doc=${documentId}&segment=66666666-0000-4000-8000-000000000404`,
  );
  await expect(
    page.getByText(
      "This segment cannot be located; it may have been deleted or is inaccessible.",
    ),
  ).toBeVisible();
});

test("a mounted segment location refreshes after editing and stops showing a deleted segment", async ({
  page,
}) => {
  const documents = t9Documents(1);
  const documentId = documents[0]!.id;
  const segmentId = "60000000-0000-4000-8000-000000000001";
  await mockKnowledgeRoutes(page, {
    bases: [t9Base({ document_count: 1 })],
    documents,
    segments: {
      [documentId]: [
        {
          id: segmentId,
          position: 1,
          content: "Original located text",
          enabled: true,
          source_position: {},
        },
      ],
    },
  });
  await page.goto(
    `/projects/alpha/knowledge?kb=${T9_BASE_ID}&doc=${documentId}&segment=${segmentId}`,
  );
  const locateCard = page.getByTestId("knowledge-segment-locate");
  const list = page.getByTestId("knowledge-segment-list");
  await expect(locateCard).toContainText("Original located text");
  await list.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByLabel("Content").fill("Updated located text");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Save", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(list).toContainText("Updated located text");
  // Stay on the same document/segment URL: no reload/remount can hide a
  // missing mutation invalidation.
  await expect(locateCard).toContainText("Updated located text");
  await expect(locateCard).not.toContainText("Original located text");

  await list.getByRole("button", { name: "Delete", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(locateCard).toContainText(
    "This segment cannot be located; it may have been deleted or is inaccessible.",
  );
  await expect(locateCard).not.toContainText("Updated located text");
});

for (const processingKind of ["reparse", "reembed"] as const) {
  test(`a located segment follows ${processingKind} publication while the document stays open`, async ({
    page,
  }) => {
    const documents = t9Documents(1);
    const document = documents[0]!;
    document.status = "processing";
    document.version = 2;
    const segmentId = "60000000-0000-4000-8000-000000000001";
    const state = await mockKnowledgeRoutes(page, {
      bases: [t9Base({ document_count: 1 })],
      documents,
      segments: {
        [document.id]: [
          {
            id: segmentId,
            position: 1,
            content: "Previously published text",
            enabled: true,
            source_position: {},
          },
        ],
      },
    });
    await page.goto(
      `/projects/alpha/knowledge?kb=${T9_BASE_ID}&doc=${document.id}&segment=${segmentId}`,
    );
    const locateCard = page.getByTestId("knowledge-segment-locate");
    await expect(locateCard).toContainText("Previously published text");
    await expect(locateCard).toContainText(
      "This segment belongs to an earlier document version",
    );

    // Simulate the worker publishing while the mounted documents query polls.
    // Re-embedding keeps the segment; re-parsing replaces the old identities.
    state.documents[0]!.status = "ready";
    state.detailVersion = 2;
    if (processingKind === "reparse") state.segments.set(document.id, []);
    if (processingKind === "reparse") {
      await expect(locateCard).toContainText(
        "This segment cannot be located; it may have been deleted or is inaccessible.",
        { timeout: 10_000 },
      );
      await expect(locateCard).not.toContainText("Previously published text");
    } else {
      await expect(locateCard).not.toContainText(
        "This segment belongs to an earlier document version",
        { timeout: 10_000 },
      );
      await expect(locateCard).toContainText("Previously published text");
    }
  });
}

test("the preview file picker previews each shown file exactly once", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "Create from documents" }).click();

  await page.getByLabel("File").setInputFiles([
    {
      name: "alpha.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("甲文件内容"),
    },
    {
      name: "beta.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("乙文件内容"),
    },
  ]);
  await expect(page.getByText("2 files selected")).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();

  // Entering the step automatically previews the first file, once.
  const previewPanel = page.getByTestId("chunk-preview-panel");
  await expect(previewPanel.getByText("Previewing: alpha.txt")).toBeVisible();
  await expect(previewPanel.getByText("预览来源 alpha.txt")).toBeVisible();
  await expect(previewPanel.getByText("Showing 3 of 7 chunks")).toBeVisible();
  expect(state.previewRequests).toHaveLength(1);

  // Picking the other file swaps the panel with exactly one new upload; the
  // replaced file's chunks disappear immediately.
  await previewPanel.getByRole("combobox", { name: "Preview file" }).click();
  await page.getByRole("option", { name: "beta.txt" }).click();
  await expect(previewPanel.getByText("Previewing: beta.txt")).toBeVisible();
  await expect(previewPanel.getByText("预览来源 beta.txt")).toBeVisible();
  await expect(previewPanel.getByText("预览来源 alpha.txt")).toHaveCount(0);
  expect(state.previewRequests).toHaveLength(2);
  expect(state.previewRequests.at(-1)?.file).toBe("beta.txt");

  // Step navigation never re-uploads the file that is already shown.
  await page.getByRole("button", { name: "Previous" }).click();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(previewPanel.getByText("预览来源 beta.txt")).toBeVisible();
  expect(state.previewRequests).toHaveLength(2);

  // Only the current file's preview is kept, so returning to the first file
  // is a fresh request rather than a cache hit.
  await previewPanel.getByRole("combobox", { name: "Preview file" }).click();
  await page.getByRole("option", { name: "alpha.txt" }).click();
  await expect(previewPanel.getByText("预览来源 alpha.txt")).toBeVisible();
  expect(state.previewRequests).toHaveLength(3);
  expect(state.previewRequests.at(-1)?.file).toBe("alpha.txt");
});

test("a replaced slow preview never overwrites the winner and removing the file clears it", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "Create from documents" }).click();

  await page.getByLabel("File").setInputFiles([
    {
      name: "slow-alpha.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("慢文件内容"),
    },
    {
      name: "beta.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("快文件内容"),
    },
  ]);
  await page.getByRole("button", { name: "Next" }).click();

  // The first file's preview hangs at the mock; switch away while it is
  // still in flight.
  const previewPanel = page.getByTestId("chunk-preview-panel");
  await expect(
    previewPanel.getByText("Previewing: slow-alpha.txt"),
  ).toBeVisible();
  await previewPanel.getByRole("combobox", { name: "Preview file" }).click();
  await page.getByRole("option", { name: "beta.txt" }).click();
  await expect(previewPanel.getByText("预览来源 beta.txt")).toBeVisible();

  // The slow response lands after the fast winner and must be discarded.
  await page.waitForTimeout(900);
  await expect(previewPanel.getByText("预览来源 beta.txt")).toBeVisible();
  await expect(previewPanel.getByText("预览来源 slow-alpha.txt")).toHaveCount(
    0,
  );
  expect(state.previewRequests).toHaveLength(2);

  // Removing the previewed file clears its panel; the remaining file
  // re-previews automatically on return.
  await page.getByRole("button", { name: "Previous" }).click();
  await page.getByRole("button", { name: "Remove beta.txt" }).click();
  await expect(page.getByText("1 file selected")).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();
  await expect(
    previewPanel.getByText("Previewing: slow-alpha.txt"),
  ).toBeVisible();
  await expect(previewPanel.getByText("预览来源 slow-alpha.txt")).toBeVisible({
    timeout: 5_000,
  });
  await expect(previewPanel.getByText("预览来源 beta.txt")).toHaveCount(0);
  expect(state.previewRequests).toHaveLength(3);
  expect(state.previewRequests.at(-1)?.file).toBe("slow-alpha.txt");
});

const T11_BASE = {
  id: "40000000-0000-4000-8000-000000000001",
  name: "产品手册",
  description: "",
  status: "active" as const,
  document_count: 1,
  delete_error: null,
};

test("results carry rank and score provenance while diagnostics stay collapsed and safe", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page, { bases: [T11_BASE] });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Retrieval test" }).click();

  await page.getByLabel("Query").fill("发布流程");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const results = page.getByTestId("knowledge-search-results");
  await expect(results.getByRole("listitem")).toHaveCount(3);

  // Every retrieval test asks for the bounded diagnostics; the base override
  // is absent unless the user forces a route.
  expect(state.searchRequests.at(-1)?.debug).toBe(true);
  expect(state.searchRequests.at(-1)).not.toHaveProperty("retrieval_mode");

  // Final rank plus this call's score kind, never a confidence percentage.
  const first = results.getByRole("listitem").first();
  await expect(first).toContainText("#1");
  await expect(first).toContainText("Retrieval score 0.930");
  await expect(first).toContainText("Rerank");
  await expect(first).not.toContainText("%");

  // The native threshold score sits in the per-hit disclosure, not the row.
  await first.getByText("Hit diagnostics").click();
  await expect(first).toContainText("Native score 0.930");
  await expect(first).toContainText("Ranking score 0.930");

  // The collapsed panel reveals actual parameters, counts, and timings.
  const diagnostics = page.getByTestId("knowledge-search-diagnostics");
  await expect(diagnostics).toBeVisible();
  await diagnostics.locator("summary").click();
  await expect(diagnostics).toContainText("m10-v1");
  await expect(diagnostics).toContainText("Semantic candidates");
  await expect(diagnostics).toContainText("56 ms");
  await expect(diagnostics).toContainText(
    "10000000-0000-4000-8000-00000000000e",
  );
  // Result snippets never leak into the diagnostics block.
  await expect(diagnostics).not.toContainText("重排后应当排在第一位的内容");

  // Forcing a route applies to this one call only.
  await page.getByRole("combobox", { name: "Retrieval route" }).click();
  await page.getByRole("option", { name: "Hybrid (this search)" }).click();
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect.poll(() => state.searchRequests.length).toBe(2);
  expect(state.searchRequests.at(-1)?.retrieval_mode).toBe("hybrid");
  expect(state.searchRequests.at(-1)?.debug).toBe(true);
});

test("empty reasons are told apart and a failed search persists until retried", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page, { bases: [T11_BASE] });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Retrieval test" }).click();

  // Before the first search the panel says so explicitly.
  await expect(page.getByTestId("knowledge-search-never")).toBeVisible();

  const searchFor = async (query: string) => {
    await page.getByLabel("Query").fill(query);
    await page.getByRole("button", { name: "Search", exact: true }).click();
  };
  const empty = page.getByTestId("knowledge-search-empty");

  await searchFor("notready 问题");
  await expect(empty).toHaveText(
    "No target document is ready for retrieval yet.",
  );
  await searchFor("unrelated 问题");
  await expect(empty).toHaveText(
    "Recall produced no candidates for this query.",
  );
  await searchFor("staleconflict 问题");
  await expect(empty).toHaveText(
    "Candidates changed while the search ran; run it again.",
  );

  // A threshold that filters everything names the threshold, not "no data".
  await page.getByLabel("Score threshold").fill("0.95");
  await searchFor("发布流程");
  await expect(empty).toHaveText(
    "Every candidate fell below the score threshold or the metadata filters.",
  );
  await page.getByLabel("Score threshold").fill("");

  // A model failure stays in the results area with a retry that resends the
  // exact same request; results only replace it after a successful search.
  await searchFor("rerank-down 状况");
  const error = page.getByTestId("knowledge-search-error");
  await expect(error).toContainText("Reranker 服务暂不可用，请稍后重试");
  const requestsBeforeRetry = state.searchRequests.length;
  await error.getByRole("button", { name: "Retry" }).click();
  await expect
    .poll(() => state.searchRequests.length)
    .toBe(requestsBeforeRetry + 1);
  expect(state.searchRequests.at(-1)?.query).toBe("rerank-down 状况");
  await expect(error).toContainText("Reranker 服务暂不可用，请稍后重试");

  await searchFor("发布流程");
  await expect(page.getByTestId("knowledge-search-results")).toBeVisible();
  await expect(page.getByTestId("knowledge-search-error")).toHaveCount(0);
});

test("hit detail pins the scored content, highlights true child matches, and locates into documents", async ({
  page,
}) => {
  const DOC_ID = "50000000-0000-4000-8000-000000000001";
  const SEG_ID = "60000000-0000-4000-8000-000000000011";
  const state = await mockKnowledgeRoutes(page, {
    bases: [T11_BASE],
    documents: [
      {
        id: DOC_ID,
        knowledge_base_id: T11_BASE.id,
        name: "发布说明.pdf",
        original_name: "发布说明.pdf",
        status: "ready",
        segment_count: 1,
        error_message: null,
        delete_error: null,
      },
    ],
    segments: {
      [DOC_ID]: [
        {
          id: SEG_ID,
          position: 7,
          content: "完整原始分段正文，比检索片段更长。",
          enabled: true,
          source_position: { page: 7 },
        },
      ],
    },
    segmentChildren: {
      [SEG_ID]: [
        {
          id: "61000000-0000-4000-8000-0000000000c1",
          position: 1,
          content: "子块一正文",
        },
        {
          id: "61000000-0000-4000-8000-0000000000c2",
          position: 2,
          content: "子块二正文",
        },
      ],
    },
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Retrieval test" }).click();
  await page.getByLabel("Query").fill("发布流程");
  await page.getByRole("button", { name: "Search", exact: true }).click();

  // The detail dialog shows the full original segment — not the snippet —
  // and highlights exactly the children this search matched (C-2, not C-1).
  const results = page.getByTestId("knowledge-search-results");
  await results
    .getByRole("button", { name: "View segment #7 in full" })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByTestId("knowledge-detail-content")).toHaveText(
    "完整原始分段正文，比检索片段更长。",
  );
  await expect(dialog.getByText("Child chunks (2)")).toBeVisible();
  const children = dialog.getByTestId("knowledge-detail-children");
  await expect(children.getByRole("listitem")).toHaveCount(2);
  await expect(children.getByRole("listitem").nth(1)).toContainText(
    "Matched · Lexical · 0.910",
  );
  await expect(children.getByRole("listitem").first()).not.toContainText(
    "Matched",
  );

  // Escape closes the dialog and returns focus to the page.
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // Once the document changes, the pinned read conflicts loudly instead of
  // explaining the old score with new text.
  state.detailDigest = "x".repeat(64);
  await results
    .getByRole("button", { name: "View segment #7 in full" })
    .click();
  await expect(page.getByTestId("knowledge-detail-conflict")).toHaveText(
    "The document changed after this result was scored. Run the search again to see current content.",
  );
  await page.keyboard.press("Escape");

  // With current content again, the detail locates into the documents view.
  state.detailDigest = "d".repeat(64);
  await results
    .getByRole("button", { name: "View segment #7 in full" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Open in documents" })
    .click();
  await expect(page).toHaveURL(new RegExp(`doc=${DOC_ID}`));
  await expect(page).toHaveURL(new RegExp(`segment=${SEG_ID}`));
  await expect(page.getByTestId("knowledge-segment-locate")).toContainText(
    "完整原始分段正文",
  );
});

test("a search that settles after a reranker change cannot resurrect its results", async ({
  page,
}) => {
  const state = await mockKnowledgeRoutes(page, { bases: [T11_BASE] });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Retrieval test" }).click();

  // The search parks server-side; the panel is pending.
  await page.getByLabel("Query").fill("slowrace 旧配置查询");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect.poll(() => state.releaseSlowSearch !== null).toBe(true);
  await expect(page.getByRole("button", { name: "Searching…" })).toBeVisible();

  // While it is in flight, the reranker binding changes: whatever that old
  // call returns is scored under a configuration that no longer exists.
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByLabel("Reranker model").click();
  await page
    .getByRole("option", { name: "SiliconFlow · BAAI/bge-reranker-v2-m3" })
    .click();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved.")).toBeVisible();

  await page.getByRole("button", { name: "Retrieval test" }).click();
  await expect(page.getByTestId("knowledge-search-never")).toBeVisible();

  // Now the stale response arrives — the panel must stay never-searched
  // instead of resurrecting results scored under the old binding.
  state.releaseSlowSearch!();
  state.releaseSlowSearch = null;
  await page.waitForTimeout(400);
  await expect(page.getByTestId("knowledge-search-never")).toBeVisible();
  await expect(page.getByText("慢响应旧结果不得回流")).toHaveCount(0);

  // A fresh search under the new binding wins normally.
  await page.getByLabel("Query").fill("发布流程");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page.getByTestId("knowledge-search-results").getByRole("listitem"),
  ).toHaveCount(3);
});

test("task progress shows stages, counts, and attempts, and a new attempt restarts from zero", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const progress = (patch: Record<string, unknown>) => ({
    kind: "ingest_document",
    status: "running",
    stage: "embedding",
    completed_units: 40,
    total_units: 120,
    attempt_count: 1,
    max_attempts: 3,
    target_version: 1,
    next_attempt_at: null,
    ...patch,
  });
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "进度知识库",
        description: "",
        status: "active",
        document_count: 5,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: "50000000-0000-4000-8000-000000000001",
        knowledge_base_id: BASE_ID,
        name: "embedding.txt",
        original_name: "embedding.txt",
        status: "processing",
        segment_count: 0,
        error_message: null,
        delete_error: null,
        task_progress: progress({}),
      },
      {
        id: "50000000-0000-4000-8000-000000000002",
        knowledge_base_id: BASE_ID,
        name: "retrywait.txt",
        original_name: "retrywait.txt",
        status: "processing",
        segment_count: 0,
        error_message: null,
        delete_error: null,
        task_progress: progress({
          status: "retry_wait",
          attempt_count: 2,
          next_attempt_at: "2026-08-30T13:00:00Z",
        }),
      },
      {
        id: "50000000-0000-4000-8000-000000000003",
        knowledge_base_id: BASE_ID,
        name: "exhausted.txt",
        original_name: "exhausted.txt",
        status: "failed",
        segment_count: 0,
        error_message: "Embedding 请求连续失败已耗尽重试",
        delete_error: null,
        task_progress: progress({ status: "failed", attempt_count: 3 }),
      },
      {
        id: "50000000-0000-4000-8000-000000000004",
        knowledge_base_id: BASE_ID,
        name: "done.txt",
        original_name: "done.txt",
        status: "ready",
        segment_count: 4,
        error_message: null,
        delete_error: null,
      },
      {
        id: "50000000-0000-4000-8000-000000000005",
        knowledge_base_id: BASE_ID,
        name: "reading.txt",
        original_name: "reading.txt",
        status: "processing",
        segment_count: 0,
        error_message: null,
        delete_error: null,
        task_progress: progress({
          kind: "reembed_document",
          stage: "reading_source",
          completed_units: 0,
          total_units: null,
        }),
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  // The lifecycle summary counts every state separately: a failed terminal
  // state is never folded into success.
  const summary = page.getByTestId("knowledge-processing-summary");
  await expect(summary).toContainText("2 processing");
  await expect(summary).toContainText("1 waiting to retry");
  await expect(summary).toContainText("1 failed");
  await expect(summary).toContainText("1 ready");

  const rows = page.getByTestId("knowledge-document-rows");
  const rowFor = (name: string) =>
    rows.getByRole("row").filter({ hasText: name });

  // Verified batch counts for a countable stage; never a percentage.
  await expect(rowFor("embedding.txt")).toContainText("Ingest · Embedding");
  await expect(rowFor("embedding.txt")).toContainText("40/120");
  await expect(rowFor("embedding.txt")).not.toContainText("%");

  // A stage without a verifiable total renders indeterminate: no counter.
  await expect(rowFor("reading.txt")).toContainText(
    "Re-embed · Reading source",
  );
  await expect(rowFor("reading.txt")).not.toContainText("0/");

  // retry_wait names the wait and the attempt it will start.
  await expect(rowFor("retrywait.txt")).toContainText(
    "Waiting for automatic retry",
  );
  await expect(rowFor("retrywait.txt")).toContainText("Attempt 2/3");

  // Exhausted retries keep the failing stage on screen.
  await expect(rowFor("exhausted.txt")).toContainText(
    "Failed during Embedding",
  );
  await expect(rowFor("exhausted.txt")).toContainText("Attempt 3/3");

  // The retry-wait document claims its next attempt: progress restarts from
  // zero — the old attempt's 40 verified units must not ride along.
  const retryDocument = state.documents.find(
    (item) => item.name === "retrywait.txt",
  )!;
  retryDocument.task_progress = progress({
    attempt_count: 2,
    completed_units: 0,
  });
  await expect(rowFor("retrywait.txt")).toContainText("0/120", {
    timeout: 10_000,
  });
  await expect(rowFor("retrywait.txt")).toContainText("Attempt 2/3");
  await expect(rowFor("retrywait.txt")).not.toContainText("40/120");

  // Finishing drops the progress line and moves the summary counts: the
  // claimed retry is a plain "processing" now, so two documents remain
  // active and the retry-wait bucket is gone.
  const embeddingDocument = state.documents.find(
    (item) => item.name === "embedding.txt",
  )!;
  embeddingDocument.status = "ready";
  embeddingDocument.segment_count = 4;
  embeddingDocument.task_progress = null;
  await expect(summary).toContainText("2 ready", { timeout: 10_000 });
  await expect(summary).toContainText("2 processing");
  await expect(summary).not.toContainText("waiting to retry");
  await expect(
    rowFor("embedding.txt").getByTestId("knowledge-task-progress"),
  ).toHaveCount(0);
});

test("batch metadata shows mixed values and applies one all-or-nothing patch", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const documents: MockDocument[] = [
    {
      id: "50000000-0000-4000-8000-000000000001",
      knowledge_base_id: BASE_ID,
      name: "运维手册.txt",
      original_name: "运维手册.txt",
      status: "ready",
      segment_count: 3,
      error_message: null,
      delete_error: null,
      doc_metadata: { category: "运维", priority: 2 },
    },
    {
      id: "50000000-0000-4000-8000-000000000002",
      knowledge_base_id: BASE_ID,
      name: "研发手册.txt",
      original_name: "研发手册.txt",
      status: "ready",
      segment_count: 3,
      error_message: null,
      delete_error: null,
      doc_metadata: { category: "研发", priority: 2 },
    },
  ];
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "批量知识库",
        description: "",
        status: "active",
        document_count: 2,
        delete_error: null,
      },
    ],
    documents,
    metadataFields: [
      {
        id: "80000000-0000-4000-8000-000000000001",
        knowledge_base_id: BASE_ID,
        name: "category",
        field_type: "string",
      },
      {
        id: "80000000-0000-4000-8000-000000000002",
        knowledge_base_id: BASE_ID,
        name: "priority",
        field_type: "number",
      },
      {
        id: "80000000-0000-4000-8000-000000000003",
        knowledge_base_id: BASE_ID,
        name: "due",
        field_type: "time",
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  await page.getByLabel("Select all documents").check();
  await page.getByRole("button", { name: "Edit metadata" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Batch metadata (2 documents)")).toBeVisible();

  // Divergent values are reported, never flattened into a fake blank; a
  // shared value pre-fills its input once the field is switched to set.
  const fieldBoxes = dialog.getByTestId("knowledge-batch-field");
  await expect(fieldBoxes.filter({ hasText: "category" })).toContainText(
    "2 distinct values",
  );

  // Nothing edited yet: the all-or-nothing patch has nothing to send.
  await expect(dialog.getByRole("button", { name: "Save" })).toBeDisabled();

  await fieldBoxes
    .filter({ hasText: "category" })
    .getByLabel("category mode")
    .click();
  await page.getByRole("option", { name: "Set" }).click();
  await fieldBoxes
    .filter({ hasText: "category" })
    .getByLabel("category value")
    .fill("统一分类");
  await expect(
    fieldBoxes
      .filter({ hasText: "category" })
      .getByText("Overwrites this field on 2 documents"),
  ).toBeVisible();

  await fieldBoxes
    .filter({ hasText: "priority" })
    .getByLabel("priority mode")
    .click();
  await page.getByRole("option", { name: "Clear" }).click();

  // The shared priority value pre-filled as 2 — but clear ignores drafts.
  // Server rejects the first submission: the whole batch rolls back and the
  // dialog keeps the edited form for a retry.
  state.batchMetadataFailure = {
    status: 409,
    code: "KNOWLEDGE_CONFLICT",
    message: "字段定义已变化，请重试",
  };
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog.getByText("字段定义已变化，请重试")).toBeVisible();
  expect(state.batchMetadataRequests).toHaveLength(1);

  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toBeHidden();
  expect(state.batchMetadataRequests).toHaveLength(2);
  expect(state.batchMetadataRequests.at(-1)).toEqual({
    document_ids: [
      "50000000-0000-4000-8000-000000000001",
      "50000000-0000-4000-8000-000000000002",
    ],
    // Only explicitly edited fields travel: set category, clear priority,
    // and the untouched "due" never appears.
    values: { category: "统一分类", priority: null },
  });

  // Reopening proves the patch landed: one shared value, no mixed marker.
  await page.getByLabel("Select all documents").check();
  await page.getByRole("button", { name: "Edit metadata" }).click();
  const reopened = page.getByRole("dialog");
  await expect(
    reopened.getByTestId("knowledge-batch-field").filter({
      hasText: "category",
    }),
  ).not.toContainText("distinct values");
});

test("reparse previews the split, freezes parameters, and a stale confirmation conflicts", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const DOC_ID = "50000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "重解析知识库",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: DOC_ID,
        knowledge_base_id: BASE_ID,
        name: "重解析文档.txt",
        original_name: "重解析文档.txt",
        status: "ready",
        segment_count: 4,
        error_message: null,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  await page
    .getByRole("button", { name: "Actions for 重解析文档.txt" })
    .click();
  await page.getByRole("menuitem", { name: "Reparse from original" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Reparse 重解析文档.txt")).toBeVisible();
  // The confirmation names what is destroyed and what it costs.
  await expect(
    dialog.getByText(/Manual segment edits and per-segment disables/),
  ).toBeVisible();

  // Server-side preview with the edited parameters.
  await dialog.getByLabel("Chunk size (characters)").fill("600");
  await dialog.getByRole("button", { name: "Preview split" }).click();
  const preview = dialog.getByTestId("knowledge-reparse-preview");
  await expect(preview).toContainText("Showing 2 of 5 chunks");
  await expect(preview).toContainText("重解析预览首段 · chunk_size=600");
  expect(state.reparsePreviewRequests.at(-1)).toMatchObject({
    expected_version: 1,
    chunk_size: 600,
    chunking_mode: "general",
  });

  // Editing a parameter retires the preview: it described another reparse.
  await dialog.getByLabel("Chunk overlap (characters)").fill("50");
  await expect(preview).toHaveCount(0);

  // The document changes elsewhere; the stale confirmation must conflict,
  // keep the dialog and its parameters, and refresh the authoritative row.
  state.documents[0]!.version = 2;
  const authorityRefresh = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().includes(`/bases/${BASE_ID}/documents?page=`),
  );
  await dialog.getByRole("button", { name: "Reparse", exact: true }).click();
  await expect(dialog.getByTestId("knowledge-reparse-conflict")).toBeVisible();
  expect(state.reparseRequests).toHaveLength(1);
  expect(state.reparseRequests.at(-1)).toMatchObject({ expected_version: 1 });
  await expect(dialog.getByLabel("Chunk size (characters)")).toHaveValue("600");

  // Re-confirming against the refreshed version freezes the parameters.
  await authorityRefresh;
  await dialog.getByRole("button", { name: "Reparse", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(state.reparseRequests.at(-1)).toMatchObject({
    expected_version: 2,
    chunk_size: 600,
    chunk_overlap: 50,
    chunk_separator: "\\n\\n",
    chunking_mode: "general",
  });

  // The accepted reparse queues the document and it reaches ready again.
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 15_000 });
});

test("a stale reparse preview conflicts, refreshes the version, and the next attempt succeeds", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const DOC_ID = "50000000-0000-4000-8000-000000000001";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "重解析知识库",
        description: "",
        status: "active",
        document_count: 1,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: DOC_ID,
        knowledge_base_id: BASE_ID,
        name: "重解析文档.txt",
        original_name: "重解析文档.txt",
        status: "ready",
        segment_count: 4,
        error_message: null,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page
    .getByRole("button", { name: "Actions for 重解析文档.txt" })
    .click();
  await page.getByRole("menuitem", { name: "Reparse from original" }).click();
  const dialog = page.getByRole("dialog");
  const preview = dialog.getByTestId("knowledge-reparse-preview");

  await dialog.getByRole("button", { name: "Preview split" }).click();
  await expect(preview).toBeVisible();
  expect(state.reparsePreviewRequests.at(-1)).toMatchObject({
    expected_version: 1,
  });

  // The document changes elsewhere. A stale preview attempt must conflict,
  // keep the parameter form, and refresh the authoritative row — exactly
  // what the conflict copy promises — instead of pinning the old version.
  state.documents[0]!.version = 2;
  const authorityRefresh = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().includes(`/bases/${BASE_ID}/documents?page=`),
  );
  await dialog.getByRole("button", { name: "Preview split" }).click();
  await expect(dialog.getByTestId("knowledge-reparse-conflict")).toBeVisible();
  await authorityRefresh;

  // Re-previewing against the refreshed version succeeds and retires the
  // conflict notice.
  await expect(async () => {
    await dialog.getByRole("button", { name: "Preview split" }).click();
    await expect(preview).toBeVisible({ timeout: 2_000 });
  }).toPass();
  expect(state.reparsePreviewRequests.at(-1)).toMatchObject({
    expected_version: 2,
  });
  await expect(dialog.getByTestId("knowledge-reparse-conflict")).toHaveCount(0);
});

test("a batch metadata conflict refreshes the selection for re-confirmation", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const KEPT_ID = "50000000-0000-4000-8000-000000000001";
  const GONE_ID = "50000000-0000-4000-8000-000000000002";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "批量知识库",
        description: "",
        status: "active",
        document_count: 2,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: KEPT_ID,
        knowledge_base_id: BASE_ID,
        name: "保留文档.txt",
        original_name: "保留文档.txt",
        status: "ready",
        segment_count: 3,
        error_message: null,
        delete_error: null,
        doc_metadata: { category: "运维" },
      },
      {
        id: GONE_ID,
        knowledge_base_id: BASE_ID,
        name: "消失文档.txt",
        original_name: "消失文档.txt",
        status: "ready",
        segment_count: 3,
        error_message: null,
        delete_error: null,
        doc_metadata: { category: "研发" },
      },
    ],
    metadataFields: [
      {
        id: "80000000-0000-4000-8000-000000000001",
        knowledge_base_id: BASE_ID,
        name: "category",
        field_type: "string",
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  await page.getByLabel("Select all documents").check();
  await page.getByRole("button", { name: "Edit metadata" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Batch metadata (2 documents)")).toBeVisible();

  const fieldBoxes = dialog.getByTestId("knowledge-batch-field");
  await fieldBoxes
    .filter({ hasText: "category" })
    .getByLabel("category mode")
    .click();
  await page.getByRole("option", { name: "Set" }).click();
  await fieldBoxes
    .filter({ hasText: "category" })
    .getByLabel("category value")
    .fill("统一分类");

  // One selected document is deleted elsewhere. The all-or-nothing patch
  // rejects; the dialog must keep the edited form but refresh the
  // authoritative rows so re-confirmation runs against what still exists.
  state.documents.splice(
    state.documents.findIndex((item) => item.id === GONE_ID),
    1,
  );
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog.getByText("文档不存在")).toBeVisible();
  await expect(dialog.getByText("Batch metadata (1 documents)")).toBeVisible();
  await expect(
    fieldBoxes.filter({ hasText: "category" }).getByLabel("category value"),
  ).toHaveValue("统一分类");

  // Re-confirming submits only the surviving selection.
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toBeHidden();
  expect(state.batchMetadataRequests.at(-1)).toEqual({
    document_ids: [KEPT_ID],
    values: { category: "统一分类" },
  });
});

test("the document metadata dialog follows the authoritative row after a conflict", async ({
  page,
}) => {
  const BASE_ID = "40000000-0000-4000-8000-000000000001";
  const KEPT_ID = "50000000-0000-4000-8000-000000000001";
  const GONE_ID = "50000000-0000-4000-8000-000000000002";
  const state = await mockKnowledgeRoutes(page, {
    bases: [
      {
        id: BASE_ID,
        name: "产品手册",
        description: "",
        status: "active",
        document_count: 2,
        delete_error: null,
      },
    ],
    documents: [
      {
        id: KEPT_ID,
        knowledge_base_id: BASE_ID,
        name: "保留文档.txt",
        original_name: "保留文档.txt",
        status: "ready",
        segment_count: 2,
        error_message: null,
        delete_error: null,
      },
      {
        id: GONE_ID,
        knowledge_base_id: BASE_ID,
        name: "消失文档.txt",
        original_name: "消失文档.txt",
        status: "ready",
        segment_count: 2,
        doc_metadata: { author: "旧作者" },
        error_message: null,
        delete_error: null,
      },
    ],
    metadataFields: [
      {
        id: "80000000-0000-4000-8000-000000000091",
        knowledge_base_id: BASE_ID,
        name: "author",
        field_type: "string",
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();

  await (await openDocumentActions(page, "消失文档.txt"))
    .getByRole("menuitem", { name: "Metadata" })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Edit metadata · 消失文档.txt")).toBeVisible();
  await dialog.getByLabel(/author/u).fill("张三");

  // The document is deleted elsewhere. The failed save must refresh the
  // authoritative list; with the row gone there is nothing to re-confirm
  // against, so the stale dialog closes instead of retrying forever.
  state.documents.splice(
    state.documents.findIndex((item) => item.id === GONE_ID),
    1,
  );
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("消失文档.txt")).toHaveCount(0);
  await expect(rows.getByText("保留文档.txt")).toBeVisible();
});
