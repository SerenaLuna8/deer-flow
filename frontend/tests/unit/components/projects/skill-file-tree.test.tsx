import { describe, expect, test } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  SKILL_FILE_TREE_PAGE_SIZE,
  SkillFileTree,
  skillFileTreePageWindow,
} from "@/components/projects/assets/skill-file-tree";
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

  test("keeps a folder with more than 5000 direct children on one fixed-size page", () => {
    const largeFolderFiles = Array.from({ length: 5_039 }, (_, index) => ({
      path: `templates/icons/tabler-outline/icon-${String(index).padStart(4, "0")}.svg`,
      media_type: "image/svg+xml",
      size_bytes: 64,
      sha256: `${index}`.padStart(64, "0"),
    }));
    const tree = buildSkillFileTree(
      listWorkingSkillFiles(largeFolderFiles, []),
      [],
    );
    const html = renderToStaticMarkup(
      <SkillFileTree
        nodes={tree}
        selection={null}
        expandedFolders={
          new Set([
            "templates",
            "templates/icons",
            "templates/icons/tabler-outline",
          ])
        }
        onSelectFile={() => undefined}
        onSelectFolder={() => undefined}
        onToggleFolder={() => undefined}
      />,
    );

    expect(SKILL_FILE_TREE_PAGE_SIZE).toBe(100);
    expect(html.match(/role="treeitem"/g)).toHaveLength(103);
    expect(html).toContain("icon-0000.svg");
    expect(html).toContain("icon-0099.svg");
    expect(html).not.toContain("icon-0100.svg");
    expect(html).not.toContain("icon-5038.svg");
    expect(html).toContain("第 1 / 51 页");

    const tablerFolder =
      tree[0]?.kind === "folder" &&
      tree[0].children[0]?.kind === "folder" &&
      tree[0].children[0].children[0]?.kind === "folder"
        ? tree[0].children[0].children[0]
        : null;
    expect(tablerFolder).not.toBeNull();
    const lastPage = skillFileTreePageWindow(tablerFolder?.children ?? [], 50);
    expect(lastPage.page).toBe(50);
    expect(lastPage.pageCount).toBe(51);
    expect(lastPage.items).toHaveLength(39);
  });
});
