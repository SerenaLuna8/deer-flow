import { beforeEach, describe, expect, rs, test } from "@rstest/core";

rs.mock("@/core/api/fetcher", () => ({
  fetch: rs.fn(),
  AuthRequiredError: class AuthRequiredError extends Error {},
}));
rs.mock("@/core/config", () => ({ getBackendBaseURL: () => "/backend" }));

import {
  AuthRequiredError,
  fetch as authenticatedFetch,
} from "@/core/api/fetcher";
import {
  fetchKnowledgeAttachment,
  KnowledgeApiError,
  knowledgeAttachmentURL,
  listKnowledgeDocumentAttachments,
  listKnowledgeFileCapabilities,
  previewKnowledgeChunks,
  uploadKnowledgeDocument,
} from "@/core/knowledge/api";
import { knowledgeFileCapabilitiesQueryKey } from "@/core/knowledge/query-keys";
import {
  knowledgeChunkPreviewResponseSchema,
  knowledgeDocumentItemSchema,
  knowledgeDocumentAttachmentListResponseSchema,
  knowledgeFileCapabilitiesSchema,
  knowledgeReparsePreviewResponseSchema,
  knowledgeSegmentDetailResponseSchema,
  knowledgeSegmentItemSchema,
} from "@/core/knowledge/types";

const fetchMock = rs.mocked(authenticatedFetch);

beforeEach(() => {
  fetchMock.mockReset();
});

const PROJECT_ID = "10000000-0000-4000-8000-000000000001";
const BASE_ID = "40000000-0000-4000-8000-000000000001";
const DOCUMENT_ID = "50000000-0000-4000-8000-000000000001";

const processingProfile = {
  parse: {
    etl_type: "builtin",
    extractor_id: "builtin.markdown",
    extractor_version: "1",
    normalization_version: "md-v1",
    image_policy_version: "raster-v1",
    header_rules: [],
  },
  chunk: {
    unit: "token",
    mode: "general",
    size: 1000,
    overlap: 100,
    separator: "\\n\\n",
    child_size: 500,
    child_separator: "\\n",
    remove_extra_spaces: false,
    remove_urls_emails: false,
    tokenizer_profile_id: "knowledge-cl100k-v1",
    tokenizer_digest: "d".repeat(64),
    cleaner_version: "cleaner-v1",
    splitter_version: "splitter-v1",
  },
};

const sourceSpan = {
  block_id: "paragraph:1",
  start: 0,
  end: 8,
  location: { paragraph: 1 },
  role: "source",
};

const fileCapabilities = {
  effective_etl: "builtin",
  capability_revision: "a".repeat(64),
  formats: [
    {
      extension: ".pdf",
      parser_id: "builtin.pdf",
      available: true,
      reason_code: null,
      embedded_images: true,
    },
  ],
  chunk_limits: {
    unit: "token",
    tokenizer_profile_id: "knowledge-cl100k-v1",
    parent_min: 200,
    parent_max: 4000,
    parent_max_chars: 4000,
    overlap_max: 500,
    child_min: 100,
    child_max: 2000,
  },
};

const previewResponse = {
  items: [
    {
      position: 1,
      content: "正文内容",
      word_count: 4,
      child_contents: [],
      token_count: 3,
      source_spans: [sourceSpan],
      attachments: [{ ref: "a".repeat(64), alt_text: "拓扑图" }],
    },
  ],
  total: 1,
  preview_fingerprint: "b".repeat(64),
  source_sha256: "c".repeat(64),
  effective_profile: processingProfile,
  warnings: [
    {
      code: "HEADER_INFERRED",
      message: "已自动识别表头，请确认",
      source_position: { row: 1 },
    },
  ],
  preview_attachments: [
    {
      ref: "a".repeat(64),
      media_type: "image/png",
      data_base64: "aGVsbG8=",
    },
  ],
  omitted_preview_attachment_count: 0,
  table_sources: [
    {
      sheet: null,
      header_mode: "auto",
      header_row: 1,
      header_cells: ["设备", "端口"],
    },
  ],
  request_id: "preview-request",
};

const documentResponse = {
  parsing_profile: processingProfile,
  parse_warnings: [],
  chunk_size_unit: "token",
  tokenizer_profile_id: "knowledge-cl100k-v1",
  content_initialized: true,
  id: DOCUMENT_ID,
  project_id: PROJECT_ID,
  knowledge_base_id: BASE_ID,
  name: "说明.md",
  original_name: "说明.md",
  media_type: "text/markdown",
  size_bytes: 1024,
  status: "ready",
  enabled: true,
  version: 1,
  chunk_size: 1000,
  chunk_overlap: 100,
  chunk_separator: "\\n\\n",
  remove_extra_spaces: false,
  remove_urls_emails: false,
  chunking_mode: "general",
  child_chunk_size: 500,
  child_chunk_separator: "\\n",
  segment_count: 1,
  word_count: 4,
  hit_count: 0,
  doc_metadata: {},
  error_message: null,
  delete_error: null,
  task_progress: null,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const segmentResponse = {
  id: "60000000-0000-4000-8000-000000000001",
  document_version: 3,
  position: 1,
  content: "![机架](knowledge-attachment:" + "e".repeat(64) + ")",
  word_count: 4,
  enabled: true,
  hit_count: 0,
  source_position: { page: 1 },
  created_at: "2026-09-01T00:00:00Z",
  token_count: 7,
  source_spans: [sourceSpan],
};

const segmentDetailResponse = {
  segment: segmentResponse,
  knowledge_base_id: BASE_ID,
  document_id: DOCUMENT_ID,
  document_name: "说明.md",
  content_state: "current",
  stored_content_version: 3,
  current_document_version: 3,
  children_total: 0,
  child_page: 1,
  children: [],
  attachments: [
    {
      attachment_id: "70000000-0000-4000-8000-000000000001",
      ref: "e".repeat(64),
      alt_text: "机架",
      media_type: "image/png",
      width: 640,
      height: 480,
    },
    {
      attachment_id: "70000000-0000-4000-8000-000000000001",
      ref: "e".repeat(64),
      alt_text: "重复位置",
      media_type: "image/png",
      width: 640,
      height: 480,
    },
  ],
  summary: null,
  request_id: "segment-detail",
};

const documentAttachmentsResponse = {
  items: [
    {
      attachment_id: "70000000-0000-4000-8000-000000000001",
      ref: "e".repeat(64),
      media_type: "image/png",
      width: 640,
      height: 480,
    },
  ],
  document_version: 3,
  request_id: "document-attachments",
};

function zeroBytesBase64(byteLength: number): string {
  const completeTriples = Math.floor(byteLength / 3);
  const remainder = byteLength % 3;
  return (
    "AAAA".repeat(completeTriples) +
    (remainder === 1 ? "AA==" : remainder === 2 ? "AAA=" : "")
  );
}

describe("frozen Knowledge parsing responses", () => {
  test("accepts the actual capability DTO and rejects storage locators", () => {
    expect(
      knowledgeFileCapabilitiesSchema.safeParse(fileCapabilities).success,
    ).toBe(true);
    expect(
      knowledgeFileCapabilitiesSchema.safeParse({
        ...fileCapabilities,
        storage_key: "private/key",
      }).success,
    ).toBe(false);
    expect(
      knowledgeFileCapabilitiesSchema.safeParse({
        ...fileCapabilities,
        formats: [
          {
            ...fileCapabilities.formats[0],
            source_path: "/private/work/file.pdf",
          },
        ],
      }).success,
    ).toBe(false);
  });

  test("rejects missing fields and unknown nested parsing fields", () => {
    const missingCapabilityLimits: Record<string, unknown> = {
      ...fileCapabilities,
    };
    delete missingCapabilityLimits.chunk_limits;
    expect(
      knowledgeFileCapabilitiesSchema.safeParse(missingCapabilityLimits)
        .success,
    ).toBe(false);
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse({
        ...previewResponse,
        effective_profile: {
          ...processingProfile,
          parse: {
            ...processingProfile.parse,
            storage_key: "private/object/key",
          },
        },
      }).success,
    ).toBe(false);
    expect(
      knowledgeDocumentItemSchema.safeParse({
        ...documentResponse,
        parsing_profile: {
          ...processingProfile,
          working_directory: "/private/work",
        },
      }).success,
    ).toBe(false);
  });

  test("accepts the actual Gateway preview DTO", () => {
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse(previewResponse).success,
    ).toBe(true);
  });

  test("accepts the actual Gateway document DTO", () => {
    expect(
      knowledgeDocumentItemSchema.safeParse(documentResponse).success,
    ).toBe(true);
  });

  test("accepts the same parsing projection on a reparse preview", () => {
    expect(
      knowledgeReparsePreviewResponseSchema.safeParse({
        ...previewResponse,
        document_version: 2,
      }).success,
    ).toBe(true);
  });

  test("rejects unsafe source metadata and reversed source intervals", () => {
    const withSpan = (span: unknown) => ({
      ...previewResponse,
      items: [{ ...previewResponse.items[0], source_spans: [span] }],
    });
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse(
        withSpan({
          ...sourceSpan,
          location: { storage_key: "private/key" },
        }),
      ).success,
    ).toBe(false);
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse(
        withSpan({ ...sourceSpan, start: 9, end: 8 }),
      ).success,
    ).toBe(false);
  });

  test("enforces preview attachment type, encoding, count, and byte budgets", () => {
    const withAttachments = (attachments: unknown[]) => ({
      ...previewResponse,
      preview_attachments: attachments,
    });
    const attachment = previewResponse.preview_attachments[0];

    expect(
      knowledgeChunkPreviewResponseSchema.safeParse(
        withAttachments([{ ...attachment, media_type: "image/svg+xml" }]),
      ).success,
    ).toBe(false);
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse(
        withAttachments([{ ...attachment, data_base64: "***=" }]),
      ).success,
    ).toBe(false);
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse(
        withAttachments([
          {
            ...attachment,
            data_base64: zeroBytesBase64(128 * 1024 + 1),
          },
        ]),
      ).success,
    ).toBe(false);
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse(
        withAttachments(
          Array.from({ length: 17 }, () => ({
            ...attachment,
            data_base64: zeroBytesBase64(128 * 1024),
          })),
        ),
      ).success,
    ).toBe(false);
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse(
        withAttachments(Array.from({ length: 21 }, () => attachment)),
      ).success,
    ).toBe(false);
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse(
        withAttachments([
          { ...attachment, data_base64: zeroBytesBase64(128 * 1024) },
        ]),
      ).success,
    ).toBe(true);
  });

  test("accepts at most the first ten preview chunks", () => {
    expect(
      knowledgeChunkPreviewResponseSchema.safeParse({
        ...previewResponse,
        items: Array.from({ length: 11 }, () => previewResponse.items[0]),
      }).success,
    ).toBe(false);
  });

  test("accepts the frozen Segment item and ordered safe detail attachments", () => {
    expect(knowledgeSegmentItemSchema.safeParse(segmentResponse).success).toBe(
      true,
    );
    const parsed = knowledgeSegmentDetailResponseSchema.safeParse(
      segmentDetailResponse,
    );
    expect(parsed.success).toBe(true);
    if (parsed.success) {
      expect(parsed.data.attachments.map((item) => item.alt_text)).toEqual([
        "机架",
        "重复位置",
      ]);
    }
  });

  test("accepts only safe current-document attachment choices", () => {
    expect(
      knowledgeDocumentAttachmentListResponseSchema.safeParse(
        documentAttachmentsResponse,
      ).success,
    ).toBe(true);
    expect(
      knowledgeDocumentAttachmentListResponseSchema.safeParse({
        ...documentAttachmentsResponse,
        items: [
          {
            ...documentAttachmentsResponse.items[0],
            storage_key: "private/object/key",
          },
        ],
      }).success,
    ).toBe(false);
  });

  test("rejects missing Segment derivations and internal locator fields", () => {
    const withoutTokenCount: Record<string, unknown> = { ...segmentResponse };
    delete withoutTokenCount.token_count;
    expect(
      knowledgeSegmentItemSchema.safeParse(withoutTokenCount).success,
    ).toBe(false);
    expect(
      knowledgeSegmentItemSchema.safeParse({
        ...segmentResponse,
        index_text: "internal model text",
      }).success,
    ).toBe(false);
    expect(
      knowledgeSegmentDetailResponseSchema.safeParse({
        ...segmentDetailResponse,
        attachments: [
          {
            ...segmentDetailResponse.attachments[0],
            storage_key: "private/object/key",
          },
        ],
      }).success,
    ).toBe(false);
  });

  test("accepts the exact manual Segment marker but rejects arbitrary legacy locators", () => {
    expect(
      knowledgeSegmentItemSchema.safeParse({
        ...segmentResponse,
        source_position: { manual: true },
        source_spans: [],
      }).success,
    ).toBe(true);
    expect(
      knowledgeSegmentItemSchema.safeParse({
        ...segmentResponse,
        source_position: { storage_key: "private/object/key" },
      }).success,
    ).toBe(false);
  });
});

describe("Knowledge parsing requests", () => {
  const processingParameters = {
    unit: "token" as const,
    mode: "general" as const,
    size: 900,
    overlap: 80,
    separator: "\\n\\n",
    child_size: 400,
    child_separator: "\\n",
    remove_extra_spaces: true,
    remove_urls_emails: false,
    header_rules: [{ sheet: null, mode: "explicit" as const, row: 2 }],
  };

  test("keeps file capabilities under the exact account and project root", () => {
    const first = knowledgeFileCapabilitiesQueryKey({
      accountId: "20000000-0000-4000-8000-000000000001",
      projectId: PROJECT_ID,
    });
    const otherAccount = knowledgeFileCapabilitiesQueryKey({
      accountId: "20000000-0000-4000-8000-000000000002",
      projectId: PROJECT_ID,
    });

    expect(first).toEqual([
      "account",
      "20000000-0000-4000-8000-000000000001",
      "project",
      PROJECT_ID,
      "knowledge",
      "file-capabilities",
    ]);
    expect(otherAccount).not.toEqual(first);
  });

  test("queries project-scoped capabilities and forwards AbortSignal", async () => {
    fetchMock.mockResolvedValue(
      Response.json(fileCapabilities, { status: 200 }),
    );
    const controller = new AbortController();

    await expect(
      listKnowledgeFileCapabilities(PROJECT_ID, controller.signal),
    ).resolves.toEqual(fileCapabilities);
    expect(fetchMock).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/knowledge/file-capabilities`,
      { signal: controller.signal },
    );
  });

  test("lists selectable attachments only under the current document", async () => {
    fetchMock.mockResolvedValue(
      Response.json(documentAttachmentsResponse, { status: 200 }),
    );
    const controller = new AbortController();

    await expect(
      listKnowledgeDocumentAttachments(
        PROJECT_ID,
        DOCUMENT_ID,
        controller.signal,
      ),
    ).resolves.toEqual(documentAttachmentsResponse);
    expect(fetchMock).toHaveBeenCalledWith(
      `/backend/api/projects/${PROJECT_ID}/knowledge/documents/${DOCUMENT_ID}/attachments`,
      { signal: controller.signal },
    );
  });

  test("previews with one strict flat processing profile and forwards AbortSignal", async () => {
    fetchMock.mockResolvedValue(
      Response.json(previewResponse, { status: 200 }),
    );
    const file = new File(["name,value\na,1"], "DATA.CSV", {
      type: "text/csv",
    });
    const controller = new AbortController();

    await previewKnowledgeChunks(
      PROJECT_ID,
      { file, processing_profile: processingParameters } as never,
      controller.signal,
    );

    const [, init] = fetchMock.mock.calls.at(-1) ?? [];
    const form = init?.body as FormData;
    expect(init?.signal).toBe(controller.signal);
    expect(form.get("file")).toBe(file);
    expect(form.get("processing_profile")).toBe(
      JSON.stringify(processingParameters),
    );
    expect([...form.keys()].sort()).toEqual(["file", "processing_profile"]);
  });

  test("uploads the same strict profile with only its matching server fingerprint", async () => {
    fetchMock.mockResolvedValue(
      Response.json(
        { item: documentResponse, request_id: "upload-request" },
        { status: 200 },
      ),
    );
    const file = new File(["body"], "guide.md", { type: "text/markdown" });

    await uploadKnowledgeDocument(PROJECT_ID, BASE_ID, {
      file,
      name: "Guide",
      processing_profile: processingParameters,
      expected_preview_fingerprint: "f".repeat(64),
    } as never);

    const [, init] = fetchMock.mock.calls.at(-1) ?? [];
    const form = init?.body as FormData;
    expect(form.get("file")).toBe(file);
    expect(form.get("name")).toBe("Guide");
    expect(form.get("processing_profile")).toBe(
      JSON.stringify(processingParameters),
    );
    expect(form.get("expected_preview_fingerprint")).toBe("f".repeat(64));
    expect([...form.keys()].sort()).toEqual([
      "expected_preview_fingerprint",
      "file",
      "name",
      "processing_profile",
    ]);
  });

  test("builds distinct encoded management and citation attachment URLs", () => {
    const common = {
      projectId: "project/one",
      documentId: "document/two",
      segmentId: "segment three",
      attachmentId: "attachment/four",
      expectedDocumentVersion: 3,
      expectedContentDigest: "b".repeat(64),
    };

    expect(knowledgeAttachmentURL({ ...common, purpose: "management" })).toBe(
      "/backend/api/projects/project%2Fone/knowledge/documents/document%2Ftwo/segments/segment%20three/attachments/attachment%2Ffour?expected_document_version=3&expected_content_digest=" +
        "b".repeat(64),
    );
    expect(
      knowledgeAttachmentURL({
        ...common,
        purpose: "citation",
        baseId: "base/five",
      }),
    ).toContain("/knowledge/bases/base%2Ffive/documents/");
  });

  test("fetches only a successful safe image response as a Blob", async () => {
    fetchMock.mockResolvedValue(
      new Response(new Uint8Array([1, 2, 3]), {
        status: 200,
        headers: { "Content-Type": "image/webp" },
      }),
    );
    const controller = new AbortController();

    const blob = await fetchKnowledgeAttachment(
      {
        projectId: PROJECT_ID,
        documentId: DOCUMENT_ID,
        segmentId: "60000000-0000-4000-8000-000000000001",
        attachmentId: "70000000-0000-4000-8000-000000000001",
        expectedDocumentVersion: 3,
        expectedContentDigest: "b".repeat(64),
        purpose: "management",
      },
      controller.signal,
    );

    expect(blob).toMatchObject({ size: 3, type: "image/webp" });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/attachments/"),
      { signal: controller.signal },
    );
  });

  test.each([
    [403, "KNOWLEDGE_FORBIDDEN"],
    [404, "KNOWLEDGE_NOT_FOUND"],
    [409, "KNOWLEDGE_CONFLICT"],
  ])(
    "maps a %s error envelope before attempting to read image bytes",
    async (status, knowledgeCode) => {
      const response = Response.json(
        {
          detail: {
            code: knowledgeCode,
            message: "内容已更新",
            request_id: "attachment-conflict",
          },
        },
        { status },
      );
      const blob = rs.spyOn(response, "blob");
      fetchMock.mockResolvedValue(response);

      const failure = await fetchKnowledgeAttachment({
        projectId: PROJECT_ID,
        baseId: BASE_ID,
        documentId: DOCUMENT_ID,
        segmentId: "60000000-0000-4000-8000-000000000001",
        attachmentId: "70000000-0000-4000-8000-000000000001",
        expectedDocumentVersion: 3,
        expectedContentDigest: "b".repeat(64),
        purpose: "citation",
      }).then(
        () => null,
        (error: unknown) => error,
      );

      expect(failure).toBeInstanceOf(KnowledgeApiError);
      expect(failure).toMatchObject({
        status,
        knowledgeCode,
        serverMessage: "内容已更新",
      });
      expect(blob).not.toHaveBeenCalled();
    },
  );

  test("maps the authenticated fetcher's 401 boundary", async () => {
    fetchMock.mockRejectedValue(new AuthRequiredError());

    await expect(
      fetchKnowledgeAttachment({
        projectId: PROJECT_ID,
        documentId: DOCUMENT_ID,
        segmentId: "60000000-0000-4000-8000-000000000001",
        attachmentId: "70000000-0000-4000-8000-000000000001",
        expectedDocumentVersion: 3,
        expectedContentDigest: "b".repeat(64),
        purpose: "management",
      }),
    ).rejects.toMatchObject({ status: 401, code: "AUTH_REQUIRED" });
  });

  test("rejects a successful response whose MIME is not a safe raster", async () => {
    fetchMock.mockResolvedValue(
      new Response("<svg/>", {
        status: 200,
        headers: { "Content-Type": "image/svg+xml" },
      }),
    );

    await expect(
      fetchKnowledgeAttachment({
        projectId: PROJECT_ID,
        documentId: DOCUMENT_ID,
        segmentId: "60000000-0000-4000-8000-000000000001",
        attachmentId: "70000000-0000-4000-8000-000000000001",
        expectedDocumentVersion: 3,
        expectedContentDigest: "b".repeat(64),
        purpose: "management",
      }),
    ).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });

  test("preserves an AbortError from the authenticated fetch", async () => {
    const aborted = new DOMException("cancelled", "AbortError");
    fetchMock.mockRejectedValue(aborted);

    await expect(
      listKnowledgeFileCapabilities(PROJECT_ID, new AbortController().signal),
    ).rejects.toBe(aborted);
  });
});
