"use client";

import { SkillBuilderResumeBanner } from "@/components/projects/skills/skill-builder-resume-banner";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Project } from "@/core/projects/types";
import {
  skillBuilderCanAuthor,
  useSkillBuilderSessions,
} from "@/core/skill-builder";

import { ProjectAssetPageShell } from "./project-asset-page-shell";
import { SkillAssetDetail } from "./skill-asset-detail";

function ProjectSkillBuilderLead({ project }: { project: Project }) {
  const { user } = useAuth();
  const canCreate = skillBuilderCanAuthor(project.capabilities);
  const sessions = useSkillBuilderSessions(
    user?.id ?? "",
    project.id,
    Boolean(user && canCreate),
  );
  return canCreate ? (
    <SkillBuilderResumeBanner
      projectSlug={project.slug}
      sessions={sessions.data ?? []}
    />
  ) : null;
}

export function ProjectSkillsPage({
  selectedAssetId = null,
}: {
  selectedAssetId?: string | null;
}) {
  return (
    <ProjectAssetPageShell
      kind="skills"
      title="Skill"
      description="查看项目可用的具体技能、兼容性、扫描结果与文件快照，并维护项目自建 Skill 的版本。"
      initialSelectedAssetId={selectedAssetId}
      renderLead={({ project }) => (
        <ProjectSkillBuilderLead project={project} />
      )}
      renderVersion={(version, context) =>
        "skill_id" in version ? (
          <SkillAssetDetail
            version={version}
            workspace={{
              accountId: context.accountId,
              projectId: context.projectId,
              item: context.item,
              canAuthor: context.canAuthor,
              editing: context.editing,
              onEditingChange: context.onEditingChange,
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
