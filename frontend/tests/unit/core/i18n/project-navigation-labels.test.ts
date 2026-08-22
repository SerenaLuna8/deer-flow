import { describe, expect, test } from "@rstest/core";

import { enUS, zhCN } from "@/core/i18n/locales";

describe("project navigation labels", () => {
  test("localizes Memory, Agent, Skill, and MCP by interface language", () => {
    expect([
      zhCN.project.navigation.memory,
      zhCN.project.navigation.agents,
      zhCN.project.navigation.skills,
      zhCN.project.navigation.mcp,
    ]).toEqual(["记忆", "智能体", "技能", "工具"]);

    expect([
      enUS.project.navigation.memory,
      enUS.project.navigation.agents,
      enUS.project.navigation.skills,
      enUS.project.navigation.mcp,
    ]).toEqual(["Memory", "Agent", "Skill", "MCP"]);
  });
});
