import { describe, expect, rs, test } from "@rstest/core";
import {
  Children,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";

import { SkillCreateMenuItems } from "@/components/projects/assets/project-asset-page-shell";
import { projectSkillImportNeedsSecretSetup } from "@/components/projects/assets/project-asset-page-shell";
import { projectSkillExactVersionSelectionHref } from "@/components/projects/assets/project-asset-page-shell";

type InspectableProps = {
  children?: ReactNode;
  href?: unknown;
  onSelect?: unknown;
};

function inspectable(node: ReactNode): ReactElement<InspectableProps> {
  if (!isValidElement<InspectableProps>(node)) {
    throw new TypeError("Expected a React element");
  }
  return node;
}

function textContent(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) return node.map(textContent).join("");
  return isValidElement<InspectableProps>(node)
    ? textContent(node.props.children)
    : "";
}

describe("Project Skill creation paths", () => {
  test("renders exactly AI Builder and archive upload choices", () => {
    const onImport = rs.fn();
    const tree = SkillCreateMenuItems({
      projectSlug: "catalog-team",
      onImport,
    });
    const items = Children.toArray(inspectable(tree).props.children);

    expect(items.map(textContent)).toEqual(["AI 对话创建", "上传压缩包"]);

    const aiLink = inspectable(
      Children.only(inspectable(items[0]).props.children),
    );
    expect(aiLink.props.href).toBe("/projects/catalog-team/skills/new");

    const uploadSelect = inspectable(items[1]).props.onSelect;
    expect(uploadSelect).toBeTypeOf("function");
    if (typeof uploadSelect === "function") uploadSelect();
    expect(onImport).toHaveBeenCalledTimes(1);
  });

  test("guides only imported Skills with declared secrets to secret setup", () => {
    expect(
      projectSkillImportNeedsSecretSetup({
        secret_requirements: [
          { name: "provider_key", target_env: "API_KEY", optional: false },
        ],
      }),
    ).toBe(true);
    expect(
      projectSkillImportNeedsSecretSetup({ secret_requirements: [] }),
    ).toBe(false);
  });

  test("keeps archive-import navigation pinned to its exact version", () => {
    const skillId = "11111111-1111-4111-8111-111111111111";
    const versionId = "22222222-2222-4222-8222-222222222222";

    expect(
      projectSkillExactVersionSelectionHref(
        "/projects/demo/skills",
        "?view=project&skill_id=old&skill_version_id=old&configure_secrets=1",
        skillId,
        versionId,
        false,
      ),
    ).toBe(
      `/projects/demo/skills?view=project&skill_id=${skillId}&skill_version_id=${versionId}`,
    );
    expect(
      projectSkillExactVersionSelectionHref(
        "/projects/demo/skills",
        "?view=project",
        skillId,
        versionId,
        true,
      ),
    ).toBe(
      `/projects/demo/skills?view=project&skill_id=${skillId}&skill_version_id=${versionId}&configure_secrets=1`,
    );
  });
});
