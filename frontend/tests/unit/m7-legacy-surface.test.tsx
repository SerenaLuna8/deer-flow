import { readFileSync, readdirSync } from "node:fs";
import { extname, join, resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

const SOURCE_ROOT = resolve(process.cwd(), "src");
const SOURCE_EXTENSIONS = new Set([".ts", ".tsx"]);

function readFrontendProductionSources(directory = SOURCE_ROOT): string {
  return readdirSync(directory, { withFileTypes: true })
    .sort((left, right) => left.name.localeCompare(right.name))
    .map((entry) => {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) return readFrontendProductionSources(path);
      if (!entry.isFile() || !SOURCE_EXTENSIONS.has(extname(entry.name))) {
        return "";
      }
      return readFileSync(path, "utf8");
    })
    .join("\n");
}

describe("M7 project-only frontend surface", () => {
  test("contains no live legacy workspace route or global API literal", () => {
    const productionFiles = readFrontendProductionSources();

    for (const forbidden of [
      "/workspace/chats",
      "/workspace/agents",
      "/workspace/memory",
      "/workspace/scheduled-tasks",
      "/workspace/skills",
      "/workspace/tools",
      "/workspace/projects",
      "/api/memory",
      "/api/agents",
      "/api/skills",
      "/api/mcp/config",
      "LEGACY_WORKSPACE_CHAT_SCOPE",
    ]) {
      expect(productionFiles).not.toContain(forbidden);
    }
  });
});
