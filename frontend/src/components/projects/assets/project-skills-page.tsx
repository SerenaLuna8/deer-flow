"use client";

import { SkillBuilderResumeBanner } from "@/components/projects/skills/skill-builder-resume-banner";
import { SkillRevisionEntry } from "@/components/projects/skills/skill-revision-entry";
import { useAuth } from "@/core/auth/AuthProvider";
import type { Project } from "@/core/projects/types";
import {
  skillBuilderCanAuthor,
  useSkillBuilderSessionByVersion,
  useSkillBuilderSessions,
} from "@/core/skill-builder";

import { useCurrentProject } from "../project-context";

import type { ProjectAssetVersionRenderContext } from "./project-asset-detail-sheet";
import { ProjectAssetPageShell } from "./project-asset-page-shell";
import { SkillAssetDetail, type SkillAssetVersion } from "./skill-asset-detail";

function ProjectSkillDetail({
  project,
  version,
  context,
}: {
  project: Project;
  version: SkillAssetVersion;
  context: ProjectAssetVersionRenderContext;
}) {
  const designSession = useSkillBuilderSessionByVersion(
    context.accountId,
    context.projectId,
    version.id,
    context.item.scope === "project",
  );
  const designRecordHref = designSession.data
    ? `/projects/${encodeURIComponent(project.slug)}/skills/new/${encodeURIComponent(designSession.data.id)}`
    : null;

  return (
    <SkillAssetDetail
      version={version}
      designRecordHref={designRecordHref}
      workspace={{
        accountId: context.accountId,
        projectId: context.projectId,
        item: context.item,
        canAuthor: context.canAuthor,
        editing: context.editing,
        credentialBindingsDirty: context.credentialBindingsDirty,
        onEditingChange: context.onEditingChange,
        onDirtyChange: context.onDirtyChange,
        onCredentialBindingsDirtyChange:
          context.onCredentialBindingsDirtyChange,
        onActivationValidityChange: context.onActivationValidityChange,
        onVersionCreated: context.onVersionCreated,
        canManageCredentials: project.capabilities.includes(
          "mcp.credentials.approve",
        ),
        credentialsHref: `/projects/${encodeURIComponent(project.slug)}/credentials`,
        focusCredentials: context.focusSkillCredentials,
        onCredentialsFocused: context.onSkillCredentialsFocused,
      }}
    />
  );
}

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
  selectedVersionId = null,
  focusSelectedSkillCredentials = false,
}: {
  selectedAssetId?: string | null;
  selectedVersionId?: string | null;
  focusSelectedSkillCredentials?: boolean;
}) {
  const project = useCurrentProject();

  return (
    <ProjectAssetPageShell
      kind="skills"
      title="Skill"
      initialSelectedAssetId={selectedAssetId}
      initialSelectedVersionId={selectedVersionId}
      initialFocusSkillCredentials={focusSelectedSkillCredentials}
      selectionQueryParam="skill_id"
      selectionDependentQueryParams={[
        "skill_version_id",
        "configure_credentials",
      ]}
      renderLead={({ project }) => (
        <ProjectSkillBuilderLead project={project} />
      )}
      renderDetailActions={({ item, editing }) => (
        <SkillRevisionEntry project={project} item={item} disabled={editing} />
      )}
      renderVersion={(version, context) =>
        "skill_id" in version ? (
          <ProjectSkillDetail
            project={project}
            version={version}
            context={context}
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
