"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { ProjectAccessDenied } from "@/components/projects/project-access-denied";
import { useCurrentProject } from "@/components/projects/project-context";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  ProjectWorkflowApiError,
  readProjectWorkflowReadiness,
} from "@/core/project-workflows/api";
import { createWorkflowDefinitionIdempotencyKey } from "@/core/project-workflows/definition-api";
import type {
  WorkflowDefinitionCreateRequestV1,
  WorkflowDefinitionResponseV1,
} from "@/core/project-workflows/definition-contracts";
import {
  useArchiveWorkflowDefinition,
  useCreateWorkflowDefinition,
  useWorkflowDefinitions,
} from "@/core/project-workflows/definition-queries";
import { useProjectWorkflowReadiness } from "@/core/project-workflows/hooks";

import {
  defaultWorkflowDefinitionListFilters,
  WorkflowDefinitionListShell,
  type WorkflowDefinitionListFilters,
  type WorkflowDefinitionListState,
} from "./workflow-definition-list-shell";

function safeMutationMessage(
  error: unknown,
  action: "create" | "archive",
): string {
  if (error instanceof ProjectWorkflowApiError) {
    if (error.code === "WORKFLOW_FORBIDDEN") {
      return "当前项目权限已变化，无法完成此操作。";
    }
    if (error.code === "WORKFLOW_DRAFT_CONFLICT") {
      return action === "create"
        ? "工作流名称已存在，请换一个名称后重试。"
        : "工作流已被其他成员修改。请关闭窗口并刷新列表后重试。";
    }
    if (error.code === "WORKFLOW_INPUT_INVALID") {
      return "提交内容不符合工作流约束，请检查后重试。";
    }
  }
  return action === "create"
    ? "暂时无法创建工作流，请重试。"
    : "暂时无法归档工作流，请重试。";
}

function isDefinitiveMutationFailure(error: unknown): boolean {
  return (
    error instanceof ProjectWorkflowApiError &&
    error.status >= 400 &&
    error.status < 500
  );
}

function uniqueDefinitions(
  pages: readonly { items: readonly WorkflowDefinitionResponseV1[] }[],
): WorkflowDefinitionResponseV1[] {
  const seen = new Set<string>();
  return pages.flatMap((page) =>
    page.items.filter((definition) => {
      if (seen.has(definition.id)) return false;
      seen.add(definition.id);
      return true;
    }),
  );
}

export type WorkflowDefinitionCreateAttempt = Readonly<{
  body: WorkflowDefinitionCreateRequestV1;
  idempotencyKey: string;
}>;

export function resolveWorkflowDefinitionCreateAttempt(
  previous: WorkflowDefinitionCreateAttempt | null,
  body: WorkflowDefinitionCreateRequestV1,
  generate: () => string = createWorkflowDefinitionIdempotencyKey,
): WorkflowDefinitionCreateAttempt {
  if (
    previous?.body.name === body.name &&
    previous.body.description === body.description
  ) {
    return previous;
  }
  return {
    body: { name: body.name, description: body.description },
    idempotencyKey: generate(),
  };
}

export function WorkflowDefinitionsRouteClient() {
  const project = useCurrentProject();
  const router = useRouter();
  const canRead = project.capabilities.includes("workflow.read");
  const canEdit = project.capabilities.includes("workflow.edit");
  const readiness = useProjectWorkflowReadiness(canRead, {
    readProjectWorkflowReadiness,
  });
  const controlPlaneReady =
    readiness.data?.status === "ready" &&
    readiness.data.workflow_enabled &&
    readiness.data.schema_ready;
  const [filters, setFilters] = useState<WorkflowDefinitionListFilters>(
    defaultWorkflowDefinitionListFilters,
  );
  const [filtersTouched, setFiltersTouched] = useState(false);
  const definitions = useWorkflowDefinitions(
    {
      query: filters.query.trim() || null,
      lifecycle: filters.lifecycle,
      publication: filters.publication,
      sort: filters.sort,
      limit: 50,
    },
    canRead && controlPlaneReady,
  );
  const create = useCreateWorkflowDefinition();

  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const createAttempt = useRef<WorkflowDefinitionCreateAttempt | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);

  const [archiveTarget, setArchiveTarget] =
    useState<WorkflowDefinitionResponseV1 | null>(null);
  const archive = useArchiveWorkflowDefinition(archiveTarget?.id ?? null);
  const archiveIdempotencyKey = useRef<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);

  useEffect(() => {
    if (canEdit) return;
    setCreateOpen(false);
    setName("");
    setDescription("");
    setCreateError(null);
    createAttempt.current = null;
    setArchiveTarget(null);
    setArchiveError(null);
    archiveIdempotencyKey.current = null;
  }, [canEdit]);

  if (!canRead) {
    return <ProjectAccessDenied area="工作流" projectSlug={project.slug} />;
  }

  let state: WorkflowDefinitionListState;
  if (readiness.isPending) {
    state = { status: "loading" };
  } else if (
    readiness.isError ||
    readiness.data === undefined ||
    readiness.data.status === "unavailable"
  ) {
    state = {
      status: "error",
      retry: () => {
        void readiness.refetch();
      },
    };
  } else if (!controlPlaneReady) {
    state = { status: "disabled" };
  } else if (definitions.isPending) {
    state = {
      status: filtersTouched ? "filtering" : "loading",
    };
  } else if (definitions.isError || definitions.data === undefined) {
    state = {
      status: "error",
      retry: () => {
        void definitions.refetch();
      },
    };
  } else {
    const pages = definitions.data.pages;
    state = {
      status: "ready",
      items: uniqueDefinitions(pages),
      nextCursor: pages.at(-1)?.next_cursor ?? null,
      loadingMore: definitions.isFetchingNextPage,
    };
  }

  const workflowHref = (workflowId: string) =>
    `/projects/${encodeURIComponent(project.slug)}/workflows/${encodeURIComponent(workflowId)}`;

  return (
    <>
      <WorkflowDefinitionListShell
        state={state}
        filters={filters}
        canEdit={canEdit}
        onFiltersChange={(nextFilters) => {
          setFiltersTouched(true);
          setFilters(nextFilters);
        }}
        onCreateBlank={() => {
          if (!canEdit) return;
          create.reset();
          setCreateError(null);
          setCreateOpen(true);
        }}
        onOpen={(definition) => router.push(workflowHref(definition.id))}
        onArchive={(definition) => {
          if (!canEdit || definition.lifecycle !== "active") return;
          archive.reset();
          archiveIdempotencyKey.current = null;
          setArchiveError(null);
          setArchiveTarget(definition);
        }}
        onLoadMore={() => {
          if (definitions.hasNextPage && !definitions.isFetchingNextPage) {
            void definitions.fetchNextPage();
          }
        }}
      />

      <Dialog
        open={canEdit && createOpen}
        onOpenChange={(open) => {
          if (!open && create.isPending) return;
          setCreateOpen(open);
          if (!open) {
            setName("");
            setDescription("");
            setCreateError(null);
            createAttempt.current = null;
            create.reset();
          }
        }}
      >
        <DialogContent closeLabel="关闭">
          <DialogHeader>
            <DialogTitle>创建空白工作流</DialogTitle>
            <DialogDescription>
              创建一份空白 Draft，然后进入 Builder
              添加节点。首批不提供模板、Chatflow 或 Agent 类型。
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              const normalizedName = name.trim();
              if (!canEdit || !normalizedName || create.isPending) return;
              const attempt = resolveWorkflowDefinitionCreateAttempt(
                createAttempt.current,
                { name: normalizedName, description },
              );
              createAttempt.current = attempt;
              void create
                .mutateAsync(attempt)
                .then((definition) => {
                  createAttempt.current = null;
                  setName("");
                  setDescription("");
                  setCreateError(null);
                  setCreateOpen(false);
                  router.push(workflowHref(definition.id));
                })
                .catch((error: unknown) => {
                  if (isDefinitiveMutationFailure(error)) {
                    createAttempt.current = null;
                  }
                  setCreateError(safeMutationMessage(error, "create"));
                });
            }}
          >
            <label className="grid gap-2 text-sm">
              名称
              <Input
                autoFocus
                disabled={create.isPending}
                required
                maxLength={255}
                value={name}
                onChange={(event) => setName(event.currentTarget.value)}
              />
            </label>
            <label className="grid gap-2 text-sm">
              描述
              <Textarea
                disabled={create.isPending}
                maxLength={4096}
                value={description}
                onChange={(event) => setDescription(event.currentTarget.value)}
              />
            </label>
            {createError && (
              <p role="alert" className="text-destructive text-sm">
                {createError}
              </p>
            )}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={create.isPending}
                onClick={() => {
                  setCreateOpen(false);
                  setName("");
                  setDescription("");
                  setCreateError(null);
                  createAttempt.current = null;
                  create.reset();
                }}
              >
                取消
              </Button>
              <Button
                type="submit"
                disabled={!canEdit || !name.trim() || create.isPending}
              >
                {create.isPending ? "正在创建…" : "创建并打开"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={canEdit && archiveTarget !== null}
        onOpenChange={(open) => {
          if (open || archive.isPending) return;
          setArchiveTarget(null);
          setArchiveError(null);
          archiveIdempotencyKey.current = null;
          archive.reset();
        }}
      >
        <DialogContent closeLabel="关闭">
          <DialogHeader>
            <DialogTitle>归档工作流</DialogTitle>
            <DialogDescription>
              归档后可通过“已归档”筛选继续查看，但不能从当前使用中列表打开。
            </DialogDescription>
          </DialogHeader>
          {archiveTarget && (
            <p className="text-sm font-medium">{archiveTarget.name}</p>
          )}
          {archiveError && (
            <p role="alert" className="text-destructive text-sm">
              {archiveError}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={archive.isPending}
              onClick={() => {
                setArchiveTarget(null);
                setArchiveError(null);
                archiveIdempotencyKey.current = null;
                archive.reset();
              }}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={!archiveTarget || archive.isPending}
              onClick={() => {
                if (!canEdit || !archiveTarget || archive.isPending) return;
                archiveIdempotencyKey.current ??=
                  createWorkflowDefinitionIdempotencyKey();
                void archive
                  .mutateAsync({
                    body: { expected_revision: archiveTarget.revision },
                    idempotencyKey: archiveIdempotencyKey.current,
                  })
                  .then(() => {
                    archiveIdempotencyKey.current = null;
                    setArchiveError(null);
                    setArchiveTarget(null);
                    archive.reset();
                  })
                  .catch((error: unknown) => {
                    if (isDefinitiveMutationFailure(error)) {
                      archiveIdempotencyKey.current = null;
                    }
                    setArchiveError(safeMutationMessage(error, "archive"));
                  });
              }}
            >
              {archive.isPending ? "正在归档…" : "确认归档"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
