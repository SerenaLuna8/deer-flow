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
});
