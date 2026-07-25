import { beforeEach, describe, expect, test, rs } from "@rstest/core";

import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  createProjectMemoryFact,
  exportProjectMemory,
  importProjectMemory,
  loadProjectMemory,
  projectMemoryPermissions,
  projectMemoryQueryKey,
  reloadProjectMemory,
  deleteProjectMemoryFact,
  updateProjectMemoryFact,
  type UserMemory,
} from "@/core/private-work/memory";

rs.mock("@/core/api/fetcher", () => ({ fetch: rs.fn() }));

const mockedFetch = rs.mocked(fetchWithAuth);
const scope = {
  accountId: "11111111-1111-4111-8111-111111111111",
  projectId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
};
const access = {
  scope,
  apiBaseURL: `/api/projects/${scope.projectId}/private-work`,
};

const memory = {
  version: "1.0",
  lastUpdated: "2026-07-15T00:00:00Z",
  user: {
    workContext: { summary: "Work", updatedAt: "2026-07-15T00:00:00Z" },
    personalContext: {
      summary: "Personal",
      updatedAt: "2026-07-15T00:00:00Z",
    },
    topOfMind: { summary: "Focus", updatedAt: "2026-07-15T00:00:00Z" },
  },
  history: {
    recentMonths: { summary: "Recent", updatedAt: "2026-07-15T00:00:00Z" },
    earlierContext: {
      summary: "Earlier",
      updatedAt: "2026-07-15T00:00:00Z",
    },
    longTermBackground: {
      summary: "Long term",
      updatedAt: "2026-07-15T00:00:00Z",
    },
  },
  facts: [
    {
      id: "fact-1",
      content: "Ship runnable versions first.",
      category: "preference",
      confidence: 0.9,
      createdAt: "2026-07-15T00:00:00Z",
      source: "manual",
    },
  ],
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("project memory adapter", () => {
  test("parses backend fact provenance for list, export, and import", async () => {
    const memoryWithProvenance = {
      ...memory,
      facts: [
        {
          ...memory.facts[0]!,
          source: "thread-source",
          sourceThreadId: "thread-source",
          sourceRunId: "run-source",
        },
      ],
    };
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse({
          namespace: "default",
          version: 3,
          memory: memoryWithProvenance,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(memoryWithProvenance))
      .mockResolvedValueOnce(
        jsonResponse({
          namespace: "default",
          version: 4,
          memory: memoryWithProvenance,
        }),
      );

    await expect(loadProjectMemory(access)).resolves.toMatchObject({
      memory: {
        facts: [{ sourceThreadId: "thread-source", sourceRunId: "run-source" }],
      },
    });
    await expect(exportProjectMemory(access)).resolves.toMatchObject({
      facts: [{ sourceThreadId: "thread-source", sourceRunId: "run-source" }],
    });
    await expect(
      importProjectMemory(access, 3, memoryWithProvenance),
    ).resolves.toMatchObject({ version: 4 });
  });

  test("uses account/project query keys and exact project memory paths", async () => {
    mockedFetch
      .mockResolvedValueOnce(
        jsonResponse({ namespace: "default", version: 3, memory }),
      )
      .mockResolvedValueOnce(jsonResponse(memory))
      .mockResolvedValueOnce(
        jsonResponse({ namespace: "default", version: 4, memory }),
      );

    expect(projectMemoryQueryKey(scope)).toEqual([
      "account",
      scope.accountId,
      "project",
      scope.projectId,
      "private-work",
      "memory",
      "default",
    ]);
    await loadProjectMemory(access);
    await exportProjectMemory(access);
    await importProjectMemory(access, 3, memory);

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/api/projects/${scope.projectId}/memory?namespace=default`,
      `/api/projects/${scope.projectId}/memory/export?namespace=default`,
      `/api/projects/${scope.projectId}/memory/import?namespace=default`,
    ]);
    expect(mockedFetch.mock.calls[2]![1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ expected_version: 3, memory }),
    });
  });

  test("strictly rejects extra response and import fields", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({ namespace: "default", version: 3, memory, leaked: true }),
    );
    await expect(loadProjectMemory(access)).rejects.toThrow();
    await expect(
      importProjectMemory(access, 3, {
        ...memory,
        leaked: true,
      } as unknown as UserMemory),
    ).rejects.toThrow();
    expect(mockedFetch).toHaveBeenCalledTimes(1);
  });

  test("keeps provenance fields whitelisted without passing through unknown fact fields", async () => {
    mockedFetch.mockResolvedValueOnce(
      jsonResponse({
        namespace: "default",
        version: 3,
        memory: {
          ...memory,
          facts: [
            {
              ...memory.facts[0]!,
              sourceThreadId: "thread-source",
              sourceRunId: "run-source",
              leaked: true,
            },
          ],
        },
      }),
    );

    await expect(loadProjectMemory(access)).rejects.toThrow();
  });

  test("lets a Viewer export and delete existing own Memory without edit authority", () => {
    expect(projectMemoryPermissions(["private_work.read_own"])).toEqual({
      canRead: true,
      canExport: true,
      canReload: false,
      canImport: false,
      canAdd: false,
      canModify: false,
      canDelete: true,
    });
  });

  test("creates, reloads, updates, and deletes with optimistic versions", async () => {
    mockedFetch.mockImplementation(async () =>
      jsonResponse({ namespace: "default", version: 4, memory }),
    );

    await createProjectMemoryFact(access, 2, {
      content: "New fact",
      category: "context",
      confidence: 0.8,
    });
    await reloadProjectMemory(access);
    await updateProjectMemoryFact(access, "fact/1", 3, {
      content: "Updated",
      confidence: 0.95,
    });
    await deleteProjectMemoryFact(access, "fact/1", 4);

    expect(mockedFetch.mock.calls.map(([url]) => url)).toEqual([
      `/api/projects/${scope.projectId}/memory/facts?namespace=default`,
      `/api/projects/${scope.projectId}/memory/reload?namespace=default`,
      `/api/projects/${scope.projectId}/memory/facts/fact%2F1?namespace=default`,
      `/api/projects/${scope.projectId}/memory/facts/fact%2F1?namespace=default`,
    ]);
    expect(mockedFetch.mock.calls[0]![1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        expected_version: 2,
        content: "New fact",
        category: "context",
        confidence: 0.8,
      }),
    });
    expect(mockedFetch.mock.calls[2]![1]).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({
        expected_version: 3,
        content: "Updated",
        confidence: 0.95,
      }),
    });
    expect(mockedFetch.mock.calls[3]![1]).toMatchObject({
      method: "DELETE",
      body: JSON.stringify({ expected_version: 4 }),
    });
  });
});
