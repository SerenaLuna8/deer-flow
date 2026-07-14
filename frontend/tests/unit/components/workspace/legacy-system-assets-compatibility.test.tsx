import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

rs.mock("@/core/auth/AuthProvider", () => ({
  useAuth: () => ({
    user: { id: "account-1", system_role: "user" },
  }),
}));
rs.mock("@/core/shared-assets", () => ({
  useSystemAssetCatalog: rs.fn(
    (_accountId: string, kind: "agents" | "skills" | "mcp-servers") => ({
      data: {
        items: [
          {
            id: `${kind}-id`,
            slug: `${kind}-slug`,
            display_name: `${kind} PostgreSQL 资产`,
          },
        ],
      },
      isLoading: false,
      error: null,
    }),
  ),
}));
rs.mock("@/core/agents/hooks", () => ({
  useAgents: () => ({
    agents: [],
    isLoading: false,
    error: new Error("disabled"),
  }),
}));
rs.mock("@/core/skills/hooks", () => ({
  useSkills: () => ({
    skills: [],
    isLoading: false,
    error: new Error("legacy"),
  }),
}));
rs.mock("@/core/mcp/hooks", () => ({
  useMCPConfig: () => ({
    config: null,
    isLoading: false,
    error: new Error("forbidden"),
  }),
}));

import { LegacySystemAssetsCompatibility } from "@/components/workspace/legacy-system-assets-compatibility";
import { useSystemAssetCatalog } from "@/core/shared-assets";

describe("legacy system asset compatibility", () => {
  test("reads all three PostgreSQL system catalogs through the authenticated catalog hook", () => {
    const agents = renderToStaticMarkup(
      <LegacySystemAssetsCompatibility kind="Agent" />,
    );
    const skills = renderToStaticMarkup(
      <LegacySystemAssetsCompatibility kind="Skill" />,
    );
    const mcp = renderToStaticMarkup(
      <LegacySystemAssetsCompatibility kind="MCP" />,
    );

    expect(agents).toContain("agents PostgreSQL 资产");
    expect(skills).toContain("skills PostgreSQL 资产");
    expect(mcp).toContain("mcp-servers PostgreSQL 资产");
    expect(rs.mocked(useSystemAssetCatalog).mock.calls).toEqual([
      ["account-1", "agents"],
      ["account-1", "skills"],
      ["account-1", "mcp-servers"],
    ]);
  });
});
