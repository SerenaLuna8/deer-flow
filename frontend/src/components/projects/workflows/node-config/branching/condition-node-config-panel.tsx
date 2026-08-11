"use client";

import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import { useWorkflowWorkbenchStore } from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  conditionNodeConfigV1Schema,
  predicateAstSchema,
  type ConditionNodeConfigV1,
  type PredicateAst,
} from "@/core/project-workflows/types";

import {
  BranchLoopPanelShell,
  PredicateAstEditor,
  arrayOrEmpty,
  bindingOptionsForDocument,
  panelLocked,
  recordOrEmpty,
  stableSemanticId,
  stringOrEmpty,
} from "./shared";

export type ConditionBranchIdentity = {
  branchId: string;
  outputPortId: string;
};

const defaultPredicate = (): PredicateAst => ({
  op: "and",
  items: [
    {
      left: { kind: "literal", value: true },
      operator: "eq",
      right: { kind: "literal", value: true },
    },
  ],
});

export function appendConditionBranch(
  config: ConditionNodeConfigV1,
  identity: ConditionBranchIdentity,
  maximum = 254,
): ConditionNodeConfigV1 {
  const occupied = new Set([
    config.else_output_port_id,
    "error",
    ...config.branches.map((branch) => branch.output_port_id),
  ]);
  if (
    config.branches.length >= Math.min(254, Math.max(1, maximum)) ||
    !identity.branchId ||
    !identity.outputPortId ||
    occupied.has(identity.outputPortId) ||
    config.branches.some((branch) => branch.id === identity.branchId)
  ) {
    return config;
  }
  return {
    ...config,
    branches: [
      ...config.branches,
      {
        id: identity.branchId,
        output_port_id: identity.outputPortId,
        label: null,
        predicate: defaultPredicate(),
      },
    ],
  };
}

export function moveConditionBranch(
  config: ConditionNodeConfigV1,
  fromIndex: number,
  toIndex: number,
): ConditionNodeConfigV1 {
  if (
    fromIndex === toIndex ||
    fromIndex < 0 ||
    fromIndex >= config.branches.length ||
    toIndex < 0 ||
    toIndex >= config.branches.length
  ) {
    return config;
  }
  const branches = [...config.branches];
  const [moved] = branches.splice(fromIndex, 1);
  if (!moved) return config;
  branches.splice(toIndex, 0, moved);
  return { ...config, branches };
}

export function removeConditionBranch(
  config: ConditionNodeConfigV1,
  index: number,
): ConditionNodeConfigV1 {
  if (
    config.branches.length <= 1 ||
    index < 0 ||
    index >= config.branches.length
  ) {
    return config;
  }
  return {
    ...config,
    branches: config.branches.filter((_, branchIndex) => branchIndex !== index),
  };
}

const readConditionConfig = (value: unknown): ConditionNodeConfigV1 => {
  const exact = conditionNodeConfigV1Schema.safeParse(value);
  if (exact.success) return exact.data;
  const config = recordOrEmpty(value);
  const branches = arrayOrEmpty(config.branches).flatMap((candidate) => {
    const branch = recordOrEmpty(candidate);
    const id = stringOrEmpty(branch.id);
    const outputPortId = stringOrEmpty(branch.output_port_id);
    const predicate = predicateAstSchema.safeParse(branch.predicate);
    if (!id || !outputPortId || !predicate.success) return [];
    return [
      {
        id,
        output_port_id: outputPortId,
        label:
          typeof branch.label === "string" && branch.label.length > 0
            ? branch.label
            : null,
        predicate: predicate.data,
      },
    ];
  });
  return {
    branches,
    else_output_port_id: stringOrEmpty(config.else_output_port_id),
  };
};

const conditionIssues = (
  raw: unknown,
  config: ConditionNodeConfigV1,
): string[] => {
  const issues: string[] = [];
  if (config.branches.length === 0) {
    issues.push("至少需要一个 IF 分支；Draft 可暂时不完整，但发布前必须补齐。");
  }
  if (!config.else_output_port_id) {
    issues.push("ELSE fallback 尚未配置。");
  }
  for (const [index, branch] of config.branches.entries()) {
    if (branch.predicate.items.length === 0) {
      issues.push(`${index === 0 ? "IF" : `ELIF ${index}`} 条件尚未配置。`);
    }
  }
  if (!conditionNodeConfigV1Schema.safeParse(raw).success) {
    issues.push(
      "Condition Draft 尚未满足 strict config；有效值仍保留在当前草稿中。",
    );
  }
  return [...new Set(issues)];
};

const conditionPortReferenced = (
  props: WorkflowNodeConfigPanelProps,
  portId: string,
): boolean =>
  (props.document.spec.transitions ?? []).some(
    (transition) =>
      transition.source?.node_id === props.nodeId &&
      transition.source.port_id === portId,
  );

export function ConditionNodeConfigPanel(props: WorkflowNodeConfigPanelProps) {
  const store = useWorkflowWorkbenchStore();
  const locked = panelLocked(props);
  const config = readConditionConfig(props.node.config);
  const issues = conditionIssues(props.node.config, config);
  const bindingOptions = bindingOptionsForDocument(
    props.document,
    props.locale,
  );

  const commit = (next: ConditionNodeConfigV1) => {
    if (locked || next === config) return;
    store.dispatch({
      type: "update_node_config",
      node_id: props.nodeId,
      config: next,
    });
  };

  const ensureElsePort = (value: ConditionNodeConfigV1) =>
    value.else_output_port_id
      ? value
      : {
          ...value,
          else_output_port_id: stableSemanticId("else"),
        };

  return (
    <BranchLoopPanelShell
      disabled={props.disabled}
      issues={issues}
      readOnly={props.readOnly}
      title="条件分支"
    >
      <section className="space-y-3" aria-label="有序 IF / ELIF 分支">
        <div className="space-y-1">
          <h4 className="text-sm font-medium">IF / ELIF</h4>
          <p className="text-muted-foreground text-xs">
            顺序属于执行语义；每项使用 typed Predicate AST，重排或改名不会重建
            stable branch/port ID。
          </p>
        </div>

        {config.branches.map((branch, index) => {
          const referenced = conditionPortReferenced(
            props,
            branch.output_port_id,
          );
          return (
            <article
              aria-label={`${index === 0 ? "IF" : `ELIF ${index}`} 分支`}
              className="border-border space-y-3 rounded-md border p-3"
              key={branch.id}
            >
              <header className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <p className="text-sm font-medium">
                    {index === 0 ? "IF" : `ELIF ${index}`}
                  </p>
                  <p className="text-muted-foreground text-[11px]">
                    stable port: <code>{branch.output_port_id}</code>
                  </p>
                </div>
                <div className="flex flex-wrap gap-1">
                  <Button
                    aria-label={`上移${index === 0 ? "IF" : `ELIF ${index}`}`}
                    disabled={locked || index === 0}
                    onClick={() =>
                      commit(moveConditionBranch(config, index, index - 1))
                    }
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    上移
                  </Button>
                  <Button
                    aria-label={`下移${index === 0 ? "IF" : `ELIF ${index}`}`}
                    disabled={locked || index === config.branches.length - 1}
                    onClick={() =>
                      commit(moveConditionBranch(config, index, index + 1))
                    }
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    下移
                  </Button>
                  <Button
                    aria-label={`删除${index === 0 ? "IF" : `ELIF ${index}`}`}
                    disabled={
                      locked || config.branches.length <= 1 || referenced
                    }
                    onClick={() => commit(removeConditionBranch(config, index))}
                    size="sm"
                    type="button"
                    variant="ghost"
                  >
                    删除
                  </Button>
                </div>
              </header>

              <label className="block space-y-1 text-xs">
                <span>分支标签</span>
                <Input
                  aria-label={`${index === 0 ? "IF" : `ELIF ${index}`} 标签`}
                  disabled={locked}
                  maxLength={128}
                  onChange={(event) => {
                    const branches = [...config.branches];
                    branches[index] = {
                      ...branch,
                      label: event.currentTarget.value || null,
                    };
                    commit({ ...config, branches });
                  }}
                  value={branch.label ?? ""}
                />
              </label>
              {referenced ? (
                <p className="text-muted-foreground text-xs" role="status">
                  该 stable port 已被 transition
                  引用，需先删除引用才能删除分支。
                </p>
              ) : null}
              <PredicateAstEditor
                disabled={locked}
                label={`${index === 0 ? "IF" : `ELIF ${index}`} typed Predicate AST`}
                onChange={(predicate) => {
                  const branches = [...config.branches];
                  branches[index] = { ...branch, predicate };
                  commit({ ...config, branches });
                }}
                options={bindingOptions}
                value={branch.predicate}
              />
            </article>
          );
        })}

        <Button
          disabled={locked || config.branches.length >= 254}
          onClick={() => {
            const token = stableSemanticId("branch");
            commit(
              appendConditionBranch(ensureElsePort(config), {
                branchId: token,
                outputPortId: token,
              }),
            );
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          添加 ELIF 分支
        </Button>
      </section>

      <section
        aria-label="ELSE fallback"
        className="border-border bg-muted/30 space-y-2 rounded-md border p-3"
      >
        <div className="flex items-center justify-between gap-3">
          <div>
            <h4 className="text-sm font-medium">ELSE</h4>
            <p className="text-muted-foreground text-xs">
              永远存在且不可删除，保证 fallback。
            </p>
          </div>
          <code className="text-xs">
            {config.else_output_port_id || "未配置"}
          </code>
        </div>
        {!config.else_output_port_id ? (
          <Button
            disabled={locked}
            onClick={() => commit(ensureElsePort(config))}
            size="sm"
            type="button"
            variant="outline"
          >
            修复 ELSE stable port
          </Button>
        ) : null}
      </section>
    </BranchLoopPanelShell>
  );
}
