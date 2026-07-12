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

  test("owns slug resolution and enter while project pages only consume context", () => {
    const provider = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-context.tsx"),
      "utf8",
    );
    const loader = readFileSync(
      resolve(process.cwd(), "src/components/projects/project-home-loader.tsx"),
      "utf8",
    );

    expect(provider).toContain("useEnteredProjectBySlug");
    expect(provider).toContain("useProjectBySlug");
    expect(provider).toContain("useEnterProject");
    expect(loader).toContain("useCurrentProject");
    expect(loader).not.toMatch(/useProjectBySlug|useEnterProject/u);
  });
});
