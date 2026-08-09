import { afterEach, describe, expect, test, rs } from "@rstest/core";

import { replaceAdminSystemSettingsSection } from "@/core/admin-settings/system/api";
import {
  memoryDocumentSettingsValueSchema,
  replaceMemoryDocumentSettingsInputSchema,
  systemSettingsMutationResponseSchema,
} from "@/core/admin-settings/system/types";

const SECTIONS = ["用户偏好与协作方式", "项目背景"];
const ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";

function jsonBody(init: RequestInit | undefined): unknown {
  if (typeof init?.body !== "string") {
    throw new Error("Expected a JSON string request body");
  }
  return JSON.parse(init.body) as unknown;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("Memory document system settings contract", () => {
  test("normalizes and accepts two to eight unique plain titles", () => {
    expect(
      memoryDocumentSettingsValueSchema.parse({
        sections: ["\u3000用户偏好与协作方式\u3000", "项目背景"],
      }),
    ).toEqual({ sections: SECTIONS });

    expect(
      memoryDocumentSettingsValueSchema.safeParse({
        sections: Array.from(
          { length: 8 },
          (_value, index) => `章节 ${index + 1}`,
        ),
      }).success,
    ).toBe(true);
  });

  test("rejects invalid counts, duplicates, markdown headings, long titles, and control characters", () => {
    const invalidSections = [
      ["只有一个章节"],
      Array.from({ length: 9 }, (_value, index) => `章节 ${index + 1}`),
      ["项目背景", " 项目背景 "],
      ["# 项目背景", "长期目标"],
      ["x".repeat(81), "长期目标"],
      ["项目\n背景", "长期目标"],
      ["\n项目背景", "长期目标"],
      ["项目背景\t", "长期目标"],
      ["项目\u200B背景", "长期目标"],
      ["项目\u2028背景", "长期目标"],
      ["项目背景\u2029", "长期目标"],
      ["项目背景 [H:12]", "长期目标"],
      ["项目背景 [SKIP]", "长期目标"],
      ["项目背景 [Correction]", "长期目标"],
      ["项目背景 [PERMANENT]", "长期目标"],
      ["项目背景 [durable]", "长期目标"],
      ["项目背景 [Ephemeral]", "长期目标"],
    ];

    for (const sections of invalidSections) {
      expect(
        memoryDocumentSettingsValueSchema.safeParse({ sections }).success,
      ).toBe(false);
    }
    expect(
      memoryDocumentSettingsValueSchema.safeParse({
        sections: SECTIONS,
        unexpected: true,
      }).success,
    ).toBe(false);
  });

  test("keeps replacement and mutation responses revision-bound and strict", () => {
    expect(
      replaceMemoryDocumentSettingsInputSchema.parse({
        expected_revision: 4,
        value: { sections: SECTIONS },
      }),
    ).toEqual({
      expected_revision: 4,
      value: { sections: SECTIONS },
    });

    expect(
      systemSettingsMutationResponseSchema.parse({
        catalog_revision: 8,
        section: "memory_document",
        stored_revision: 5,
        effective_revision: 5,
        effect_scope: "new_memory_documents",
        effective_at: "2026-08-09T00:00:00Z",
        pending_roles: [],
        policy: {
          revision: 5,
          schema_version: 2,
          value: { sections: SECTIONS },
        },
      }).section,
    ).toBe("memory_document");

    expect(
      replaceMemoryDocumentSettingsInputSchema.safeParse({
        expected_revision: 0,
        value: { sections: SECTIONS },
      }).success,
    ).toBe(false);
  });

  test("saves through the independent memory_document CAS endpoint", async () => {
    const fetcher = rs.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        Response.json({
          catalog_revision: 8,
          section: "memory_document",
          stored_revision: 5,
          effective_revision: 5,
          effect_scope: "new_memory_documents",
          effective_at: "2026-08-09T00:00:00Z",
          pending_roles: [],
          policy: {
            revision: 5,
            schema_version: 2,
            value: { sections: SECTIONS },
          },
        }),
    );
    rs.stubGlobal("document", { cookie: "csrf_token=memory-document-token" });
    rs.stubGlobal("fetch", fetcher);

    const result = await replaceAdminSystemSettingsSection(
      ACCOUNT_ID,
      "memory_document",
      { expected_revision: 4, value: { sections: SECTIONS } },
    );

    expect(result.section).toBe("memory_document");
    const [input, init] = fetcher.mock.calls[0]!;
    const url = new URL(
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url,
      "http://local.test",
    );
    expect(url.pathname).toBe("/api/admin/settings/system/memory_document");
    expect(init?.method).toBe("PUT");
    expect(jsonBody(init)).toEqual({
      expected_revision: 4,
      value: { sections: SECTIONS },
    });
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe(
      "memory-document-token",
    );
  });
});
