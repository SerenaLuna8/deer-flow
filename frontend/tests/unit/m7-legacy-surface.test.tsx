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
      "/api/threads",
      "LEGACY_WORKSPACE_CHAT_SCOPE",
    ]) {
      expect(productionFiles).not.toContain(forbidden);
    }
  });

  test("private-work and artifact contracts require a project scope", () => {
    const privateWorkTypes = readFileSync(
      resolve(process.cwd(), "src/core/private-work/types.ts"),
      "utf8",
    );
    const privateWorkQueryKeys = readFileSync(
      resolve(process.cwd(), "src/core/private-work/query-keys.ts"),
      "utf8",
    );
    const artifactHooks = readFileSync(
      resolve(process.cwd(), "src/core/artifacts/hooks.ts"),
      "utf8",
    );
    const artifactLoader = readFileSync(
      resolve(process.cwd(), "src/core/artifacts/loader.ts"),
      "utf8",
    );
    const artifactUtils = readFileSync(
      resolve(process.cwd(), "src/core/artifacts/utils.ts"),
      "utf8",
    );

    expect(privateWorkTypes).toContain("scope: ProjectClientScope;");
    expect(privateWorkTypes).not.toContain("scope: ProjectClientScope | null;");
    expect(privateWorkQueryKeys).not.toContain("scopedPrivateWorkQueryKey");
    expect(artifactHooks).not.toContain("url?: string;");
    expect(artifactLoader).not.toContain("url?: string;");
    expect(artifactUtils).not.toContain("urlOfArtifact");
    expect(artifactUtils).not.toContain("/api/threads/${threadId}/artifacts");
  });
});
