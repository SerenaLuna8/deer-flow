import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, rs, test } from "@rstest/core";
import {
  Children,
  isValidElement,
  type ReactElement,
  type ReactNode,
} from "react";

import { SkillCreateMenuItems } from "@/components/projects/assets/project-asset-page-shell";

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

function source(path: string): string {
  return readFileSync(resolve(process.cwd(), "src", path), "utf8");
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

  test("removes the manual-create component and client contract while preserving version authoring", () => {
    const adminPage = source(
      "components/admin/assets/admin-project-asset-page.tsx",
    );
    const adminDialogs = source(
      "components/admin/assets/admin-asset-dialogs.tsx",
    );
    const api = source("core/shared-assets/api.ts");
    const hooks = source("core/shared-assets/hooks.ts");
    const types = source("core/shared-assets/types.ts");
    const localeTypes = source("core/i18n/locales/types.ts");
    const zhCN = source("core/i18n/locales/zh-CN.ts");
    const enUS = source("core/i18n/locales/en-US.ts");

    for (const [contents, removedNames] of [
      [adminPage, ["CreateAssetDialog", "useCreateAdminProjectAsset"]],
      [adminDialogs, ["CreateAssetDialog"]],
      [api, ["createProjectAsset", "createAdminProjectAsset"]],
      [hooks, ["useCreateProjectAsset", "useCreateAdminProjectAsset"]],
      [types, ["CreateAssetInput", "createAssetInputSchema"]],
      [
        localeTypes,
        [
          "createProjectAsset",
          "createAssetTitle",
          "skillCreationDescription",
          "assetCreationDescription",
        ],
      ],
      [
        zhCN,
        [
          "createProjectAsset",
          "createAssetTitle",
          "skillCreationDescription",
          "assetCreationDescription",
        ],
      ],
      [
        enUS,
        [
          "createProjectAsset",
          "createAssetTitle",
          "skillCreationDescription",
          "assetCreationDescription",
        ],
      ],
    ] as const) {
      for (const name of removedNames) {
        expect(contents).not.toMatch(new RegExp(`\\b${name}\\b`, "u"));
      }
    }

    expect(adminPage).toContain("useCreateAdminProjectAssetVersion");
    expect(adminPage).toContain("<CreateVersionDialog");
  });
});
