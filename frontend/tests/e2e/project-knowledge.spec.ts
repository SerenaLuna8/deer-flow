import { expect, test, type Page, type Route } from "@playwright/test";

import type { Capability, Project } from "@/core/projects/types";

const ACCOUNT_ID = "90000000-0000-4000-8000-000000000001";
const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const MODEL_ID = "30000000-0000-4000-8000-000000000001";
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
  const pattern = new RegExp(
    `name="${name}"\\r\\n\\r\\n([^\\r]*)\\r\\n`,
    "u",
  );
  return pattern.exec(body)?.[1] ?? null;
}

type MockBase = {
  id: string;
  name: string;
  description: string;
  status: "active" | "disabled" | "deleting";
  document_count: number;
  delete_error: string | null;
  default_top_k?: number;
  default_score_threshold?: number;
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
    model_configuration_id: MODEL_ID,
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
    version: 1,
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
  /** explicit stateful segments per document id (enables segment CRUD) */
  segments?: Record<string, MockSegment[]>;
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
    queries: options.queries ?? [],
    metadataFields: options.metadataFields ?? [],
    uploadCounter: 0,
    segmentCounter: 0,
    queryCounter: 0,
    fieldCounter: 0,
    documentListRequests: 0,
    searchRequests: [] as Array<Record<string, unknown>>,
    previewRequests: [] as Array<Record<string, string>>,
    baseUpdates: [] as Array<Record<string, unknown>>,
    rebuildRequests: [] as Array<Record<string, unknown>>,
    metadataUpdates: [] as Array<Record<string, unknown>>,
  };

  await page.route("**/api/**", (route) => {
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
      return json(route, {
        items: [
          {
            id: MODEL_ID,
            display_name: "SiliconFlow bge-m3",
            embedding_model: "BAAI/bge-m3",
            embedding_dimension: 1024,
            reranker_model: "BAAI/bge-reranker-v2-m3",
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
      };
      const created: MockBase = {
        id: `40000000-0000-4000-8000-00000000000${state.bases.length + 1}`,
        name: body.name,
        description: body.description ?? "",
        status: "active",
        document_count: 0,
        delete_error: null,
      };
      state.bases.push(created);
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
      };
      state.baseUpdates.push(body);
      if (body.name !== undefined) base.name = body.name;
      if (body.description !== undefined) base.description = body.description;
      if (body.status !== undefined) base.status = body.status;
      if (body.default_top_k !== undefined) {
        base.default_top_k = body.default_top_k;
      }
      if (body.default_score_threshold !== undefined) {
        base.default_score_threshold = body.default_score_threshold;
      }
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
      const pageNumber = Number.parseInt(url.searchParams.get("page") ?? "1", 10);
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
      for (const item of state.documents) {
        if (item.knowledge_base_id !== base.id) continue;
        item.status = "queued";
        item.progression = ["processing", "ready"];
      }
      return json(route, { item: baseView(base), request_id: "req-rebuild" });
    }

    const documentMetadataMatch = /\/documents\/([0-9a-f-]{36})\/metadata$/u.exec(
      path,
    );
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
      return json(route, listPayload(items.map(documentView)));
    }
    if (documentsMatch && method === "POST") {
      state.uploadCounter += 1;
      const form = request.postData() ?? "";
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
        chunking_mode: uploadedMode === "parent_child" ? "parent_child" : "general",
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
      return json(route, {
        item: documentView(uploaded),
        request_id: "req-upload",
      });
    }

    if (path === `${knowledgeBase}/chunk-preview` && method === "POST") {
      const form = request.postData() ?? "";
      const fields = {
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
      const parentChild = fields.chunking_mode === "parent_child";
      const contents = [
        `预览分段一 size=${fields.chunk_size} sep=${fields.chunk_separator}`,
        `预览分段二 spaces=${fields.remove_extra_spaces} urls=${fields.remove_urls_emails}`,
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
      if (query.includes("rerank-down")) {
        return knowledgeError(
          route,
          502,
          "KNOWLEDGE_MODEL_UNAVAILABLE",
          "Reranker 服务暂不可用，请稍后重试",
        );
      }
      if (query.includes("unrelated")) {
        state.queryCounter += 1;
        state.queries.unshift({
          id: `70000000-0000-4000-8000-00000000000${state.queryCounter}`,
          query,
          source: "retrieval_test",
          result_count: 0,
          top_score: null,
        });
        return json(route, { citations: [], request_id: "req-search-empty" });
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
        },
      ];
      // Mirrors the backend contract: segments scoring below the requested
      // threshold are dropped after reranking, never replaced by an error.
      const citations =
        typeof body.score_threshold === "number"
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
      return json(route, { citations, request_id: "req-search" });
    }

    return json(route, { detail: "not found" }, 404);
  });

  return state;
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

  // The preview panel loads with the default parameters after the debounce.
  const previewPanel = page.getByTestId("chunk-preview-panel");
  await expect(
    previewPanel.getByText("Previewing the first file: handbook.txt"),
  ).toBeVisible();
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=\\n\\n"),
  ).toBeVisible();
  await expect(previewPanel.getByText("7 chunks in total")).toBeVisible();

  // Editing the delimiter and rules refreshes the preview with new params.
  await page.getByLabel("Delimiter").fill("。");
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=。"),
  ).toBeVisible();
  await page
    .getByLabel("Replace consecutive spaces, newlines and tabs")
    .check();
  await expect(
    previewPanel.getByText("预览分段二 spaces=true urls=false"),
  ).toBeVisible();

  // A backend preview failure surfaces inline and recovery re-renders chunks.
  await page.getByLabel("Delimiter").fill("BOOM");
  await expect(
    previewPanel.getByText("文件没有可提取的文本"),
  ).toBeVisible();
  await page.getByLabel("Delimiter").fill("。");
  await expect(
    previewPanel.getByText("预览分段一 size=1000 sep=。"),
  ).toBeVisible();

  await page.getByLabel("Name").fill("产品手册");
  await page.getByLabel("Model configuration").click();
  await page.getByRole("option", { name: "SiliconFlow bge-m3" }).click();
  await page.getByRole("button", { name: "Save & process" }).click();

  // Step 3: the base exists and embedding progress advances to ready.
  await expect(page.getByText("Knowledge base created")).toBeVisible();
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

test("creates an empty base from the wizard escape hatch", async ({ page }) => {
  await mockKnowledgeRoutes(page);
  await page.goto("/projects/alpha/knowledge");

  await page.getByRole("button", { name: "New base" }).click();
  await page.getByRole("button", { name: "Create an empty base" }).click();
  await page.getByLabel("Name").fill("空知识库");
  await page.getByLabel("Model configuration").click();
  await page.getByRole("option", { name: "SiliconFlow bge-m3" }).click();
  await page.getByRole("button", { name: "Create", exact: true }).click();

  const baseList = page.getByTestId("knowledge-base-list");
  await expect(baseList.getByText("空知识库")).toBeVisible();
  await expect(baseList.getByText("0 documents")).toBeVisible();
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

  await rows.getByRole("button", { name: "Retry" }).click();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 15_000 });

  // The segment browser replaces the documents table in place.
  await rows.getByRole("button", { name: "View segments" }).click();
  const browser = page.getByTestId("knowledge-segment-browser");
  await expect(
    browser.getByTestId("knowledge-segment-list").getByText("分段 1 的内容"),
  ).toBeVisible();
  await expect(browser.getByText("Page 1 of 1 · 4 total")).toBeVisible();

  // The back entry returns to the documents table of the same base.
  await browser.getByRole("button", { name: "Documents" }).click();
  await expect(page.getByTestId("knowledge-document-rows")).toBeVisible();
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
  const downloadLink = rows
    .getByRole("row")
    .filter({ hasText: "old.txt" })
    .getByRole("link", { name: "Download original" });
  await expect(downloadLink).toHaveAttribute("download", "old.txt");

  // A failed document is deletable without a prior retry.
  const failedRow = rows.getByRole("row").filter({ hasText: "bad.pdf" });
  await expect(failedRow.getByText("文件解析失败")).toBeVisible();
  await failedRow.getByRole("button", { name: "Delete" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await expect(rows.getByText("bad.pdf")).toHaveCount(0);
  await expect(rows.getByText("old.txt")).toBeVisible();

  await rows.getByRole("button", { name: "Delete" }).click();
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
  await row.getByRole("button", { name: "Delete" }).click();
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
  await row.getByRole("button", { name: "Delete" }).click();
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
  await expect(results.getByRole("listitem")).toHaveCount(2);
  await expect(results.getByRole("listitem").first()).toContainText(
    "重排后应当排在第一位的内容",
  );
  await expect(results.getByRole("listitem").first()).toContainText(
    "Relevance 0.930",
  );
  await expect(results.getByRole("listitem").first()).toContainText("Page 7");
  await expect(results.getByRole("listitem").nth(1)).toContainText("Row 12");

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
    "Relevance 0.930",
  );
  expect(state.searchRequests.at(-1)?.score_threshold).toBe(0.5);

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
  await mockKnowledgeRoutes(page, {
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
  await expect(rows.getByRole("button", { name: "Rename" })).toHaveCount(0);
  await expect(rows.getByRole("button", { name: "Delete" })).toHaveCount(0);

  // The segment browser is reachable but exposes no mutation controls.
  await rows.getByRole("button", { name: "View segments" }).click();
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
  await rows.getByRole("button", { name: "Rename" }).click();
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

  // Switching to parent-child reveals the child parameters with defaults.
  await page.getByRole("radio", { name: /Parent-child/u }).check();
  const childSize = page.getByLabel("Child chunk size (characters)");
  await expect(childSize).toHaveValue("500");
  await expect(page.getByLabel("Child delimiter")).toHaveValue("\\n");

  // The preview re-runs in parent-child mode and nests children per parent.
  await expect(
    previewPanel.getByText("父块1子块一 child=500 csep=\\n"),
  ).toBeVisible();
  await expect(previewPanel.getByText("2 child chunks · ", { exact: false }).first()).toBeVisible();

  // Tuning the child size refreshes the nested preview.
  await childSize.fill("300");
  await expect(
    previewPanel.getByText("父块1子块一 child=300 csep=\\n"),
  ).toBeVisible();
  expect(state.previewRequests.at(-1)).toMatchObject({
    chunking_mode: "parent_child",
    child_chunk_size: "300",
    child_chunk_separator: "\\n",
  });

  await page.getByLabel("Name").fill("父子手册");
  await page.getByLabel("Model configuration").click();
  await page.getByRole("option", { name: "SiliconFlow bge-m3" }).click();
  await page.getByRole("button", { name: "Save & process" }).click();

  // The summary reports the mode and the child parameters.
  await expect(page.getByText("Knowledge base created")).toBeVisible();
  await expect(page.getByText("Chunking mode")).toBeVisible();
  await expect(page.getByText("Parent-child", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Child chunk size (characters)"),
  ).toBeVisible();

  // The upload froze the parent-child parameters.
  expect(state.documents.at(-1)?.chunking_mode).toBe("parent_child");
  expect(state.documents.at(-1)?.child_chunk_size).toBe(300);
  expect(state.documents.at(-1)?.child_chunk_separator).toBe("\\n");
});

test("upload dialog sends parent-child chunking parameters", async ({
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
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Upload document" }).click();

  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("File").setInputFiles({
    name: "guide.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("上传对话框父子分块内容"),
  });
  await dialog.getByRole("radio", { name: "Parent-child" }).check();
  await dialog.getByLabel("Child chunk size (characters)").fill("200");
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();

  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("handbook-1.txt")).toBeVisible();
  expect(state.documents.at(-1)?.chunking_mode).toBe("parent_child");
  expect(state.documents.at(-1)?.child_chunk_size).toBe(200);
  expect(state.documents.at(-1)?.child_chunk_separator).toBe("\\n");
});

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
  const emptyRow = recent.getByRole("row").filter({ hasText: "零结果历史查询" });
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
  await page.getByLabel("Default score threshold").fill("0.3");
  await page.getByRole("button", { name: "Save", exact: true }).click();
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
  await page
    .getByTestId("knowledge-document-rows")
    .getByRole("button", { name: "View segments" })
    .click();

  const browser = page.getByTestId("knowledge-segment-browser");
  const list = browser.getByTestId("knowledge-segment-list");
  await expect(browser.getByText("2 segments · 12 characters")).toBeVisible();

  // Editing re-embeds server-side and updates the aggregated counts.
  const first = list.getByRole("listitem").filter({ hasText: "第一段" });
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

  const rows = page.getByTestId("knowledge-document-rows");
  await rows.getByRole("button", { name: "Metadata" }).click();
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
  await rows.getByRole("button", { name: "Metadata" }).click();
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
        error_message: null,
        delete_error: null,
      },
    ],
  });
  await page.goto("/projects/alpha/knowledge");
  await page.getByRole("button", { name: "View documents" }).click();
  await page.getByRole("button", { name: "Settings" }).click();

  // The rebuild block sits under the settings form with its own confirm.
  const rebuildSection = page.getByRole("region", { name: "Embedding model" });
  await expect(rebuildSection).toBeVisible();
  await rebuildSection.getByLabel("Model configuration").click();
  await page.getByRole("option", { name: "SiliconFlow bge-m3" }).click();
  await rebuildSection
    .getByRole("button", { name: "Rebuild embeddings" })
    .click();
  const confirm = page.getByRole("dialog");
  await expect(
    confirm.getByText("Rebuild knowledge base embeddings"),
  ).toBeVisible();
  await confirm.getByRole("button", { name: "Rebuild", exact: true }).click();
  await expect(
    page.getByText("Rebuild started; documents will reprocess one by one."),
  ).toBeVisible();
  expect(state.rebuildRequests.at(-1)).toEqual({
    model_configuration_id: MODEL_ID,
  });

  // Every document re-queues and walks back to ready on subsequent polls.
  await page.getByRole("button", { name: "Documents", exact: true }).click();
  const rows = page.getByTestId("knowledge-document-rows");
  await expect(rows.getByText("guide.txt")).toBeVisible();
  await expect(rows.getByText("Ready")).toBeVisible({ timeout: 15_000 });
});

test("upload dialog accepts the K4 document formats", async ({ page }) => {
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

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText(/HTML, PPTX, and EPUB/u)).toBeVisible();
  await expect(dialog.getByLabel("File")).toHaveAttribute(
    "accept",
    ".pdf,.docx,.txt,.md,.csv,.xlsx,.html,.htm,.pptx,.epub",
  );
});
