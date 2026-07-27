import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, test } from "@rstest/core";

import {
  initialSkillFilePath,
  selectedSkillFileAncestorFolders,
} from "@/components/projects/assets/skill-version-workbench";

describe("Skill version workbench expansion", () => {
  test("leaves version creation to the detail-level action bar", () => {
    const source = readFileSync(
      resolve(
        process.cwd(),
        "src/components/projects/assets/skill-version-workbench.tsx",
      ),
      "utf8",
    );

    expect(source).not.toContain("编辑为新版本");
    expect(source).not.toContain("PencilIcon");
    expect(source).toContain("const isEditing = canAuthor && editing;");
  });

  test("keeps a large version folded when the default selection is root SKILL.md", () => {
    const files = [
      { path: "SKILL.md" },
      ...Array.from({ length: 12_138 }, (_, index) => ({
        path: `templates/icons/tabler-outline/icon-${String(index).padStart(5, "0")}.svg`,
      })),
    ];

    const selectedPath = initialSkillFilePath(files);

    expect(selectedPath).toBe("SKILL.md");
    expect(selectedSkillFileAncestorFolders(selectedPath)).toEqual([]);
  });

  test("expands only every ancestor of a newly selected deep file", () => {
    expect(
      selectedSkillFileAncestorFolders(
        "templates/icons/tabler-outline/icon-00001.svg",
      ),
    ).toEqual([
      "templates",
      "templates/icons",
      "templates/icons/tabler-outline",
    ]);
  });
});
