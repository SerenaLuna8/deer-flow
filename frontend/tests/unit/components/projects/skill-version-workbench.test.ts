import { describe, expect, test } from "@rstest/core";

import {
  initialSkillFilePath,
  selectedSkillFileAncestorFolders,
} from "@/components/projects/assets/skill-version-workbench";

describe("Skill version workbench expansion", () => {
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
