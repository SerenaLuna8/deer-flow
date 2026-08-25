import { expect, test } from "@rstest/core";

import { projectSkillDeleteErrorMessage } from "@/components/projects/assets/project-asset-view-model";
import {
  projectAssetDeleteConfirmLabel,
  projectAssetDeleteDescription,
  projectAssetDeleteTitle,
  projectSkillDeleteSuccessMessage,
} from "@/components/projects/assets/project-skill-delete-dialog";
import { SharedAssetApiError } from "@/core/shared-assets";

test("describes irreversible Skill archival, automatic Agent unbinding, and secret loss", () => {
  const description = projectAssetDeleteDescription("Skill", "ppt-master");

  expect(description).toContain("从所有 Agent 中移除");
  expect(description).toContain("保持各自当前状态");
  expect(description).toContain("不会被自动停用");
  expect(description).toContain("秘密会立即销毁");
  expect(description).toContain("尚未物化秘密或后续重试的运行可能失败");
  expect(description).not.toContain("永久删除整个 Skill 包");
  expect(description).not.toContain("解除 Agent 引用");
});

test("renders logical Skill deletion without physical-delete labels", () => {
  expect(projectAssetDeleteTitle("Skill")).toBe("删除 Skill？");
  expect(projectAssetDeleteConfirmLabel("Skill", 0, false)).toBe("确认删除");
  expect(projectAssetDeleteConfirmLabel("Skill", 3, false)).toBe(
    "确认删除（3 秒）",
  );
});

test("reports the authoritative affected Agent count", () => {
  expect(projectSkillDeleteSuccessMessage(0)).toBe(
    "已删除 Skill，并从 0 个 Agent 中移除绑定。",
  );
  expect(projectSkillDeleteSuccessMessage(3)).toBe(
    "已删除 Skill，并从 3 个 Agent 中移除绑定。",
  );
});

test("never gives manual-unbind guidance for the logical Skill delete action", () => {
  const message = projectSkillDeleteErrorMessage(
    new SharedAssetApiError(409, "ASSET_IN_USE", "Asset is still referenced"),
  );

  expect(message).toBe("Skill 删除未能完成，请刷新后重试。");
  expect(message).not.toContain("解除");
  expect(message).not.toContain("历史运行");
  expect(message).not.toContain("物理删除");
});
