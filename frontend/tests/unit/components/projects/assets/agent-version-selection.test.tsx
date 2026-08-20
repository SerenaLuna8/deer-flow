import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ProjectAgentVersionRecoveryControls,
  ProjectAgentVersionWorkbenchSlot,
  projectAgentVersionWorkbenchSelection,
} from "@/components/projects/assets/project-asset-detail-sheet";

describe("Agent version selection", () => {
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

  test("explains a stale draft and offers a new draft before the workbench", () => {
    const html = renderToStaticMarkup(
      <>
        <ProjectAgentVersionRecoveryControls
          kind="agents"
          scope="project"
          canAuthor
          version={{
            id: "00000000-0000-4000-8000-000000000004",
            workflow_status: "draft",
            supersedes_version_id: "00000000-0000-4000-8000-000000000002",
          }}
          currentPublishedVersionId="00000000-0000-4000-8000-000000000005"
          actionPending={false}
          dirty={false}
          versionSelectionPending={false}
          onCreateDraft={() => undefined}
        />
        <section aria-label="Agent 工作台">当前工作台</section>
      </>,
    );

    expect(html).toContain("此草稿基于旧的发布版本，不能直接发布");
    expect(html).toContain("基于此版本创建新草稿");
    expect(html.indexOf("基于此版本创建新草稿")).toBeLessThan(
      html.indexOf("当前工作台"),
    );
  });

  test("does not direct a read-only viewer to an unavailable draft action", () => {
    const html = renderToStaticMarkup(
      <ProjectAgentVersionRecoveryControls
        kind="agents"
        scope="project"
        canAuthor={false}
        version={{
          id: "00000000-0000-4000-8000-000000000004",
          workflow_status: "draft",
          supersedes_version_id: "00000000-0000-4000-8000-000000000002",
        }}
        currentPublishedVersionId="00000000-0000-4000-8000-000000000005"
        actionPending={false}
        dirty={false}
        versionSelectionPending={false}
        onCreateDraft={() => undefined}
      />,
    );

    expect(html).toBe("");
  });
});
