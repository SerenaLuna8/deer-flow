import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectAgentVersionWorkbenchSlot,
  projectAgentVersionWorkbenchSelection,
  projectCurrentVersionSystemUsageLabel,
} from "@/components/projects/assets/project-asset-detail-sheet";

describe("Agent version selection", () => {
  test("shows Main as available without pretending it needs a project binding", () => {
    expect(
      projectCurrentVersionSystemUsageLabel("agents", {
        scope: "system",
        slug: "project-assistant",
        status: "active",
        current_version_id: "00000000-0000-4000-8000-000000000001",
        binding: null,
      }),
    ).toBe("可用");
  });

  test("shows the selected stale version in the workbench without allowing edits", () => {
    const currentAuthoringVersion = {
      id: "00000000-0000-4000-8000-000000000005",
      agents_instructions: "# Current version",
    };
    const selectedStaleDraft = {
      id: "00000000-0000-4000-8000-000000000004",
      agents_instructions: "# Selected stale draft",
    };

    const selection = projectAgentVersionWorkbenchSelection(
      selectedStaleDraft,
      currentAuthoringVersion,
    );

    expect(selection.version?.agents_instructions).toBe(
      "# Selected stale draft",
    );
    expect(selection.canAuthor).toBe(false);
  });

  test("renders the selected version through the workbench slot as read-only", () => {
    const html = renderToStaticMarkup(
      <ProjectAgentVersionWorkbenchSlot
        selectedVersion={{
          id: "00000000-0000-4000-8000-000000000004",
          agents_instructions: "# Selected stale draft",
        }}
        authoringBaseVersion={{
          id: "00000000-0000-4000-8000-000000000005",
          agents_instructions: "# Current version",
        }}
        canAuthor
        render={(version, canAuthor) => (
          <section>
            <p>{version?.agents_instructions}</p>
            {canAuthor ? <button type="button">编辑</button> : <p>只读</p>}
          </section>
        )}
      />,
    );

    expect(html).toContain("# Selected stale draft");
    expect(html).toContain(
      'data-agent-version-id="00000000-0000-4000-8000-000000000004"',
    );
    expect(html).toContain("只读");
    expect(html).not.toContain("# Current version");
    expect(html).not.toContain(">编辑<");
  });
});
