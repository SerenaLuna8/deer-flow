import { describe, expect, test, rs } from "@rstest/core";
import { renderToStaticMarkup } from "react-dom/server";

import {
  defaultWorkflowDefinitionListFilters,
  WorkflowDefinitionListShell,
  type WorkflowDefinitionListState,
} from "@/components/projects/workflows/definitions/workflow-definition-list-shell";
import type { WorkflowDefinitionResponseV1 } from "@/core/project-workflows/definition-contracts";

const FIRST_ID = "11111111-1111-4111-8111-111111111111";
const SECOND_ID = "22222222-2222-4222-8222-222222222222";

function definition(
  overrides: Partial<WorkflowDefinitionResponseV1> = {},
): WorkflowDefinitionResponseV1 {
  return {
    id: FIRST_ID,
    name: "研究摘要",
    description: "汇总并整理项目资料",
    lifecycle: "active",
    publication: "draft_only",
    revision: 3,
    current_published_version_id: null,
    current_published_version_number: null,
    draft_revision: 3,
    draft_checksum: "a".repeat(64),
    created_at: "2026-08-10T08:00:00Z",
    updated_at: "2026-08-10T09:00:00Z",
    ...overrides,
  };
}

function render(state: WorkflowDefinitionListState, canEdit = false): string {
  return renderToStaticMarkup(
    <WorkflowDefinitionListShell
      state={state}
      filters={defaultWorkflowDefinitionListFilters}
      canEdit={canEdit}
      onFiltersChange={rs.fn()}
      onCreateBlank={rs.fn()}
      onOpen={rs.fn()}
      onArchive={rs.fn()}
      onLoadMore={rs.fn()}
    />,
  );
}

describe("G18 Workflow Definition list shell", () => {
  test("keeps disabled, retryable unavailable, loading, and empty states distinct", () => {
    const disabled = render({ status: "disabled" }, true);
    const unavailable = render({ status: "error", retry: rs.fn() }, true);
    const loading = render({ status: "loading" }, true);
    const editableEmpty = render(
      { status: "ready", items: [], nextCursor: null, loadingMore: false },
      true,
    );
    const readOnlyEmpty = render({
      status: "ready",
      items: [],
      nextCursor: null,
      loadingMore: false,
    });

    expect(disabled).toContain('data-testid="workflow-disabled"');
    expect(disabled).toContain("平台未启用工作流");
    expect(disabled).not.toContain("还没有工作流");
    expect(unavailable).toContain('data-testid="workflow-unavailable"');
    expect(unavailable).toContain(">重试<");
    expect(loading).toContain('aria-busy="true"');
    expect(editableEmpty).toContain('data-testid="workflow-empty"');
    expect(editableEmpty).toContain("创建空白工作流");
    expect(readOnlyEmpty).toContain("当前没有可查看的工作流");
    expect(readOnlyEmpty).not.toContain("创建空白工作流");
  });

  test("keeps filters mounted while a changed query is loading", () => {
    const html = render({ status: "filtering" }, true);

    expect(html).toContain('data-testid="workflow-filtering"');
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('aria-label="搜索工作流"');
    expect(html).toContain('aria-label="生命周期"');
    expect(html).toContain('aria-label="发布状态"');
    expect(html).toContain('aria-label="排序"');
    expect(html).not.toContain('aria-label="正在加载工作流"');
  });

  test("renders search, lifecycle/publication filters, sort, and safe summaries", () => {
    const html = render({
      status: "ready",
      items: [
        definition(),
        definition({
          id: SECOND_ID,
          name: "已发布流程",
          publication: "published",
          current_published_version_id: "33333333-3333-4333-8333-333333333333",
          current_published_version_number: 4,
        }),
      ],
      nextCursor: "cursor-next",
      loadingMore: false,
    });

    expect(html).toContain('aria-label="搜索工作流"');
    expect(html).toContain('aria-label="生命周期"');
    expect(html).toContain('aria-label="发布状态"');
    expect(html).toContain('aria-label="排序"');
    expect(html).toContain("研究摘要");
    expect(html).toContain("仅草稿");
    expect(html).toContain("已发布");
    expect(html).toContain("版本 4");
    expect(html).toContain("加载更多");
    expect(html).not.toContain("draft_checksum");
    expect(html).not.toContain("current_published_version_id");
  });

  test("shows create, edit, and archive only with workflow.edit", () => {
    const ready: WorkflowDefinitionListState = {
      status: "ready",
      items: [definition()],
      nextCursor: null,
      loadingMore: false,
    };
    const viewer = render(ready);
    const editor = render(ready, true);

    expect(viewer).toContain("查看");
    expect(viewer).not.toContain("创建空白工作流");
    expect(viewer).not.toContain(">编辑<");
    expect(viewer).not.toContain(">归档<");
    expect(editor).toContain("创建空白工作流");
    expect(editor).toContain(">编辑<");
    expect(editor).toContain(">归档<");
  });

  test("keeps archived Definitions read-only even for an editor", () => {
    const html = render(
      {
        status: "ready",
        items: [definition({ lifecycle: "archived" })],
        nextCursor: null,
        loadingMore: false,
      },
      true,
    );

    expect(html).toContain('aria-label="查看 研究摘要"');
    expect(html).toContain(">查看<");
    expect(html).not.toContain(">编辑<");
    expect(html).not.toContain(">归档<");
  });

  test("does not turn an empty filtered result into the first-run empty state", () => {
    const html = renderToStaticMarkup(
      <WorkflowDefinitionListShell
        state={{
          status: "ready",
          items: [],
          nextCursor: null,
          loadingMore: false,
        }}
        filters={{
          ...defaultWorkflowDefinitionListFilters,
          query: "missing",
          publication: "published",
        }}
        canEdit
        onFiltersChange={rs.fn()}
        onCreateBlank={rs.fn()}
        onOpen={rs.fn()}
        onArchive={rs.fn()}
        onLoadMore={rs.fn()}
      />,
    );

    expect(html).toContain('data-testid="workflow-filter-empty"');
    expect(html).toContain("没有符合筛选条件的工作流");
    expect(html).not.toContain('data-testid="workflow-empty"');
  });

  test("does not treat sort order alone as an active filter", () => {
    const html = renderToStaticMarkup(
      <WorkflowDefinitionListShell
        state={{
          status: "ready",
          items: [],
          nextCursor: null,
          loadingMore: false,
        }}
        filters={{
          ...defaultWorkflowDefinitionListFilters,
          sort: "name_asc",
        }}
        canEdit
        onFiltersChange={rs.fn()}
        onCreateBlank={rs.fn()}
        onOpen={rs.fn()}
        onArchive={rs.fn()}
        onLoadMore={rs.fn()}
      />,
    );

    expect(html).toContain('data-testid="workflow-empty"');
    expect(html).toContain("创建空白工作流");
    expect(html).not.toContain('data-testid="workflow-filter-empty"');
  });
});
