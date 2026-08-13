"use client";

import { SkillBuilderResumeBanner } from "@/components/projects/skills/skill-builder-resume-banner";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Project } from "@/core/projects/types";
import {
  skillBuilderCanAuthor,
  useSkillBuilderSessions,
} from "@/core/skill-builder";

import { useCurrentProject } from "../project-context";

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
  return user && canCreate ? (
    <SkillBuilderResumeBanner
      accountId={user.id}
      projectId={project.id}
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
  const project = useCurrentProject();

  return (
    <ProjectAssetPageShell
      kind="skills"
      title="Skill"
      initialSelectedAssetId={selectedAssetId}
      selectionQueryParam="skill_id"
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
              canManageCredentials: project.capabilities.includes(
                "mcp.credentials.approve",
              ),
              credentialsHref: `/projects/${encodeURIComponent(project.slug)}/credentials`,
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
