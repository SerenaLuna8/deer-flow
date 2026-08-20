import { beforeEach, describe, expect, rs, test } from "@rstest/core";

import { reloadProjectAgentAuthoringState } from "@/components/projects/assets/agent-authoring-recovery";
import {
  listProjectAssets,
  listProjectAssetVersions,
  type ProjectAssetItem,
  type ProjectAssetList,
  type VersionHistoryResponse,
} from "@/core/shared-assets";

rs.mock("@/core/shared-assets", () => ({
  listProjectAssets: rs.fn(),
  listProjectAssetVersions: rs.fn(),
  projectAssetKey: (...parts: unknown[]) => parts,
  projectAssetVersionsKey: (...parts: unknown[]) => parts,
}));

const PROJECT_ID = "00000000-0000-4000-8000-000000000002";
const AGENT_ID = "00000000-0000-4000-8000-000000000003";
const VERSION_ID = "00000000-0000-4000-8000-000000000004";

const item = {
  id: AGENT_ID,
  revision: 2,
  current_version_id: VERSION_ID,
} as ProjectAssetItem;
const catalog = {
  system_items: [],
  project_items: [item],
  request_id: "catalog",
} as ProjectAssetList;
const history = {
  data: [
    {
      id: VERSION_ID,
      agent_id: AGENT_ID,
      version_number: 1,
      relation: "current",
      supersedes_version_id: null,
    },
  ],
  request_id: "history",
} as VersionHistoryResponse;

const mockedListAssets = rs.mocked(listProjectAssets);
const mockedListVersions = rs.mocked(listProjectAssetVersions);

describe("Agent authoring recovery requests", () => {
  beforeEach(() => {
    mockedListAssets.mockReset();
    mockedListVersions.mockReset();
    mockedListAssets.mockResolvedValue(catalog);
    mockedListVersions.mockResolvedValue(history);
  });

  test("forwards one lifecycle AbortSignal to every recovery read", async () => {
    const controller = new AbortController();

    await reloadProjectAgentAuthoringState({
      projectId: PROJECT_ID,
      assetId: AGENT_ID,
      attemptedRevision: 1,
      includeDependencyCatalogs: true,
      signal: controller.signal,
    });

    expect(mockedListAssets.mock.calls.map((call) => call.slice(0, 2))).toEqual(
      [
        [PROJECT_ID, "agents"],
        [PROJECT_ID, "skills"],
        [PROJECT_ID, "mcp-servers"],
        [PROJECT_ID, "agents"],
      ],
    );
    expect(mockedListVersions.mock.calls[0]?.slice(0, 3)).toEqual([
      PROJECT_ID,
      "agents",
      AGENT_ID,
    ]);
    const forwardedSignals = [
      ...mockedListAssets.mock.calls.map((call) => call[2]),
      ...mockedListVersions.mock.calls.map((call) => call[3]),
    ];
    expect(
      forwardedSignals.every(
        (signal) => signal instanceof AbortSignal && !signal.aborted,
      ),
    ).toBe(true);

    controller.abort();
    expect(forwardedSignals.every((signal) => signal?.aborted)).toBe(true);
  });
});
