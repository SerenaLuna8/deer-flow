import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { useCurrentProject } from "@/components/projects/project-context";

function ProjectConsumer() {
  const project = useCurrentProject();
  return createElement("span", null, project.display_name);
}

describe("project context owner", () => {
  test("rejects consumers outside the project provider", () => {
    expect(() => renderToStaticMarkup(createElement(ProjectConsumer))).toThrow(
      "useCurrentProject must be used within a ProjectContextProvider",
    );
  });

  test("mounts private work scope from the authenticated account and entered project", () => {
    const source = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-context.tsx"),
      "utf8",
    );

    expect(source).toContain("ProjectPrivateWorkProvider");
    expect(source).toContain("accountId={user.id}");
    expect(source).toContain("projectId={entry.project.id}");
  });
});
