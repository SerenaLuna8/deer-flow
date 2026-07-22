"use client";

import { ProjectAssetPageShell } from "./project-asset-page-shell";
import { SkillAssetDetail } from "./skill-asset-detail";

export function ProjectSkillsPage() {
  return (
    <ProjectAssetPageShell
      kind="skills"
      title="Skill"
      description="查看项目可用的具体技能、兼容性、扫描结果与文件快照，并维护项目自建 Skill 的版本。"
      renderVersion={(version, context) =>
        "skill_id" in version ? (
          <SkillAssetDetail
            version={version}
            workspace={{
              accountId: context.accountId,
              projectId: context.projectId,
              item: context.item,
              canAuthor: context.canAuthor,
              onDirtyChange: context.onDirtyChange,
              onVersionCreated: context.onVersionCreated,
            }}
          />
        ) : (
          <p role="alert" className="text-destructive text-sm">
            Skill 版本数据无效。
          </p>
        )
      }
    />
  );
}
