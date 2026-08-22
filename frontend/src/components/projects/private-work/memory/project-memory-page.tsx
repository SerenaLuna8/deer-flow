"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

import { ProjectAccessDenied } from "@/components/projects/project-access-denied";
import { useCurrentProject } from "@/components/projects/project-context";
import { useI18n } from "@/core/i18n/hooks";
import { useProjectMemoryQueryModel } from "@/core/private-work/memory/hooks";
import { projectMemoryPermissions } from "@/core/private-work/memory/permissions";
import {
  parseProjectMemorySelectedVersion,
  parseProjectMemoryTab,
  parseProjectMemoryVersionPage,
  type ProjectMemoryTab,
} from "@/core/private-work/memory/query-model";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import type { Project } from "@/core/projects/types";

import { MemoryDocumentWorkbench } from "./memory-document-workbench";

function AuthorizedProjectMemoryPage({
  project,
  permissions,
}: {
  project: Project;
  permissions: ReturnType<typeof projectMemoryPermissions>;
}) {
  const privateWork = usePrivateWorkAccess();
  const router = useRouter();
  const searchParams = useSearchParams();
  const selectedVersion = parseProjectMemorySelectedVersion(
    searchParams.get("version"),
  );
  const activeTab = parseProjectMemoryTab(searchParams.get("tab"));
  const versionPage = parseProjectMemoryVersionPage(
    searchParams.get("versionPage"),
  );

  const replaceMemoryQuery = useCallback(
    (mutate: (parameters: URLSearchParams) => void) => {
      const parameters = new URLSearchParams(searchParams.toString());
      mutate(parameters);
      const query = parameters.toString();
      router.replace(
        `/projects/${encodeURIComponent(project.slug)}/memory${query ? `?${query}` : ""}`,
        { scroll: false },
      );
    },
    [project.slug, router, searchParams],
  );

  const selectVersion = useCallback(
    (version: number | null) => {
      replaceMemoryQuery((parameters) => {
        if (version === null) parameters.delete("version");
        else parameters.set("version", String(version));
      });
    },
    [replaceMemoryQuery],
  );

  const selectTab = useCallback(
    (tab: ProjectMemoryTab) => {
      replaceMemoryQuery((parameters) => {
        if (tab === "archive") parameters.set("tab", "archive");
        else parameters.delete("tab");
      });
    },
    [replaceMemoryQuery],
  );

  const setVersionPage = useCallback(
    (page: number) => {
      replaceMemoryQuery((parameters) => {
        if (page <= 0) parameters.delete("versionPage");
        else parameters.set("versionPage", String(page));
      });
    },
    [replaceMemoryQuery],
  );

  const model = useProjectMemoryQueryModel({
    privateWork,
    permissions,
    selectedVersion,
    versionPage,
    selectVersion,
    previousVersionPage: () => setVersionPage(Math.max(0, versionPage - 1)),
    nextVersionPage: () => setVersionPage(versionPage + 1),
  });

  return (
    <MemoryDocumentWorkbench
      activeTab={activeTab}
      onTabChange={selectTab}
      document={model.document}
      versions={model.versions}
      detail={model.detail}
      episodes={model.episodes}
      pending={model.pending}
      actions={model.actions}
    />
  );
}

export function ProjectMemoryAccessBoundary({ project }: { project: Project }) {
  const { t } = useI18n();
  const permissions = projectMemoryPermissions(project.capabilities);
  if (!permissions.canRead) {
    return (
      <ProjectAccessDenied projectSlug={project.slug} area={t.project.memory} />
    );
  }
  return (
    <AuthorizedProjectMemoryPage
      key={project.id}
      project={project}
      permissions={permissions}
    />
  );
}

export function ProjectMemoryPage() {
  return <ProjectMemoryAccessBoundary project={useCurrentProject()} />;
}
