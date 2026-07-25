import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import { SkillFileTree } from "@/components/projects/assets/skill-file-tree";
import {
  addSkillFolder,
  buildSkillFileTree,
  listWorkingSkillFiles,
  type SkillFileMetadata,
} from "@/components/projects/assets/skill-file-workbench-state";

const files: SkillFileMetadata[] = [
  {
    path: "SKILL.md",
    media_type: "text/markdown",
    size_bytes: 20,
    sha256: "a".repeat(64),
  },
  {
    path: "scripts/run.py",
    media_type: "text/x-python",
    size_bytes: 12,
    sha256: "b".repeat(64),
  },
];

describe("Skill file tree", () => {
  test("renders nested folders, empty folders, and one selected file", () => {
    const working = listWorkingSkillFiles(files, []);
    const explicitFolders = addSkillFolder(
      ["references"],
      working,
      "references",
      "examples",
    );
    const html = renderToStaticMarkup(
      <SkillFileTree
        nodes={buildSkillFileTree(working, explicitFolders)}
        selection={{ kind: "file", path: "scripts/run.py" }}
        expandedFolders={new Set(["references", "scripts"])}
        onSelectFile={() => undefined}
        onSelectFolder={() => undefined}
        onToggleFolder={() => undefined}
      />,
    );

    expect(html).toContain('role="tree"');
    expect(html).toContain('aria-label="文件夹 references"');
    expect(html).toContain('aria-label="文件夹 references/examples"');
    expect(html).toContain('aria-label="文件 scripts/run.py"');
    expect(html).toContain('aria-selected="true"');
    expect(html).toContain('aria-expanded="true"');
    expect(html.match(/aria-selected="true"/g)).toHaveLength(1);
  });
});
