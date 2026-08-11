"use client";

import { useState } from "react";

import type { WorkflowNodeConfigPanelProps } from "@/components/projects/workflows/node-config/contracts";
import { useWorkflowWorkbenchStore } from "@/components/projects/workflows/workbench/workbench-store-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  variableAggregateNodeConfigV1Schema,
  workflowValueTypeSchema,
  type ValueBinding,
  type VariableAggregateNodeConfigV1,
  type WorkflowValueType,
} from "@/core/project-workflows/types";

import {
  BranchLoopPanelShell,
  WorkflowValueTypeEditor,
  arrayOrEmpty,
  panelLocked,
  recordOrEmpty,
  safeValueBinding,
  safeValueType,
  stableSemanticId,
  stringOrEmpty,
} from "./shared";

export type AggregateGroup = VariableAggregateNodeConfigV1["groups"][number];

const AGGREGATE_NATIVE_MAX_GROUPS = 254;
const AGGREGATE_NATIVE_MAX_CANDIDATES = 100_000;

export function appendAggregateGroup(
  config: VariableAggregateNodeConfigV1,
  group: AggregateGroup,
  maximum = AGGREGATE_NATIVE_MAX_GROUPS,
): VariableAggregateNodeConfigV1 {
  const limit = Math.min(AGGREGATE_NATIVE_MAX_GROUPS, Math.max(1, maximum));
  if (
    config.groups.length >= limit ||
    !group.id ||
    group.id === "next" ||
    group.id === "error" ||
    config.groups.some((candidate) => candidate.id === group.id)
  ) {
    return config;
  }
  return { ...config, groups: [...config.groups, structuredClone(group)] };
}

export function moveAggregateGroup(
  config: VariableAggregateNodeConfigV1,
  fromIndex: number,
  toIndex: number,
): VariableAggregateNodeConfigV1 {
  if (
    fromIndex === toIndex ||
    fromIndex < 0 ||
    fromIndex >= config.groups.length ||
    toIndex < 0 ||
    toIndex >= config.groups.length
  ) {
    return config;
  }
  const groups = [...config.groups];
  const [moved] = groups.splice(fromIndex, 1);
  if (!moved) return config;
  groups.splice(toIndex, 0, moved);
  return { ...config, groups };
}

export function removeAggregateGroup(
  config: VariableAggregateNodeConfigV1,
  index: number,
): VariableAggregateNodeConfigV1 {
  if (config.groups.length <= 1 || index < 0 || index >= config.groups.length) {
    return config;
  }
  return {
    ...config,
    groups: config.groups.filter((_, groupIndex) => groupIndex !== index),
  };
}

export function appendAggregateCandidate(
  config: VariableAggregateNodeConfigV1,
  groupIndex: number,
  candidateInputId: string,
  maximum = AGGREGATE_NATIVE_MAX_CANDIDATES,
): VariableAggregateNodeConfigV1 {
  const group = config.groups[groupIndex];
  const limit = Math.min(AGGREGATE_NATIVE_MAX_CANDIDATES, Math.max(1, maximum));
  if (
    !group ||
    !candidateInputId ||
    group.candidate_input_ids.length >= limit ||
    group.candidate_input_ids.includes(candidateInputId)
  ) {
    return config;
  }
  const groups = [...config.groups];
  groups[groupIndex] = {
    ...group,
    candidate_input_ids: [...group.candidate_input_ids, candidateInputId],
  };
  return { ...config, groups };
}

export function moveAggregateCandidate(
  config: VariableAggregateNodeConfigV1,
  groupIndex: number,
  fromIndex: number,
  toIndex: number,
): VariableAggregateNodeConfigV1 {
  const group = config.groups[groupIndex];
  if (
    !group ||
    fromIndex === toIndex ||
    fromIndex < 0 ||
    fromIndex >= group.candidate_input_ids.length ||
    toIndex < 0 ||
    toIndex >= group.candidate_input_ids.length
  ) {
    return config;
  }
  const candidateInputIds = [...group.candidate_input_ids];
  const [moved] = candidateInputIds.splice(fromIndex, 1);
  if (!moved) return config;
  candidateInputIds.splice(toIndex, 0, moved);
  const groups = [...config.groups];
  groups[groupIndex] = {
    ...group,
    candidate_input_ids: candidateInputIds,
  };
  return { ...config, groups };
}

export function removeAggregateCandidate(
  config: VariableAggregateNodeConfigV1,
  groupIndex: number,
  candidateIndex: number,
): VariableAggregateNodeConfigV1 {
  const group = config.groups[groupIndex];
  if (
    !group ||
    candidateIndex < 0 ||
    candidateIndex >= group.candidate_input_ids.length
  ) {
    return config;
  }
  const groups = [...config.groups];
  groups[groupIndex] = {
    ...group,
    candidate_input_ids: group.candidate_input_ids.filter(
      (_, index) => index !== candidateIndex,
    ),
  };
  return { ...config, groups };
}

const readAggregateConfig = (value: unknown): VariableAggregateNodeConfigV1 => {
  const exact = variableAggregateNodeConfigV1Schema.safeParse(value);
  if (exact.success) return exact.data;
  const config = recordOrEmpty(value);
  const groups = arrayOrEmpty(config.groups).flatMap((candidate) => {
    const group = recordOrEmpty(candidate);
    const id = stringOrEmpty(group.id);
    const name = stringOrEmpty(group.name);
    const parsedType = workflowValueTypeSchema.safeParse(group.value_type);
    if (!id || !name || !parsedType.success) return [];
    return [
      {
        id,
        name,
        value_type: parsedType.data,
        candidate_input_ids: arrayOrEmpty(group.candidate_input_ids).flatMap(
          (inputId) =>
            typeof inputId === "string" && inputId ? [inputId] : [],
        ),
      },
    ];
  });
  return { strategy: "exclusive_branch", groups };
};

const literalCompatibilityIssue = (
  binding: ValueBinding | null,
  target: WorkflowValueType,
): string | null => {
  if (binding?.kind !== "literal") return null;
  const value = binding.value;
  if (value === null) {
    return target.nullable ? null : "JSON null 与当前不可空类型不兼容";
  }
  if (target.collection) {
    return Array.isArray(value) ? null : "候选不是该组声明的数组类型";
  }
  if (Array.isArray(value)) return "候选是数组，但该组声明为标量";
  if (target.kind === "json") return null;
  if (target.kind === "messages") {
    return "messages 候选需由发布校验确认完整 schema";
  }
  return typeof value === target.kind
    ? null
    : `候选 literal 类型为 ${typeof value}，与 ${target.kind} 不同型`;
};

const aggregateIssues = (
  props: WorkflowNodeConfigPanelProps,
  config: VariableAggregateNodeConfigV1,
  maxGroups: number,
  maxCandidates: number,
): string[] => {
  const issues: string[] = [];
  const bindings = recordOrEmpty(props.node.input_bindings);
  if (config.groups.length === 0) issues.push("至少需要一个聚合分组。");
  if (config.groups.length > maxGroups) {
    issues.push(`分组数超过 Catalog 上限 ${maxGroups}。`);
  }
  for (const [groupIndex, group] of config.groups.entries()) {
    if (group.candidate_input_ids.length === 0) {
      issues.push(`分组 ${groupIndex + 1} 至少需要一个 candidate。`);
    }
    if (group.candidate_input_ids.length > maxCandidates) {
      issues.push(
        `分组 ${groupIndex + 1} 的 candidate 超过 Catalog 上限 ${maxCandidates}。`,
      );
    }
    for (const candidateId of group.candidate_input_ids) {
      if (!(candidateId in bindings)) {
        issues.push(`candidate ${candidateId} 未绑定到节点 input_bindings。`);
        continue;
      }
      const compatibility = literalCompatibilityIssue(
        safeValueBinding(bindings[candidateId]),
        group.value_type,
      );
      if (compatibility) {
        issues.push(`candidate ${candidateId}：${compatibility}。`);
      }
    }
  }
  if (
    !variableAggregateNodeConfigV1Schema.safeParse(props.node.config).success
  ) {
    issues.push("Variable Aggregate Draft 尚未满足 strict config。");
  }
  return [...new Set(issues)];
};

const outputReferenceExists = (
  props: WorkflowNodeConfigPanelProps,
  outputPortId: string,
): boolean => {
  const matches = (value: unknown): boolean => {
    if (Array.isArray(value)) return value.some(matches);
    if (!value || typeof value !== "object") return false;
    const record = value as Record<string, unknown>;
    if (
      record.kind === "node_output" &&
      record.node_id === props.nodeId &&
      record.output_id === outputPortId
    ) {
      return true;
    }
    return Object.values(record).some(matches);
  };
  return matches({
    nodes: props.document.spec.nodes,
    outputs: props.document.spec.workflow_outputs,
  });
};

function AggregateGroupEditor({
  availableCandidateIds,
  config,
  group,
  groupIndex,
  locked,
  maxCandidates,
  onCommit,
  outputReferenced,
}: {
  availableCandidateIds: readonly string[];
  config: VariableAggregateNodeConfigV1;
  group: AggregateGroup;
  groupIndex: number;
  locked: boolean;
  maxCandidates: number;
  onCommit: (next: VariableAggregateNodeConfigV1) => void;
  outputReferenced: boolean;
}) {
  const [candidateToAdd, setCandidateToAdd] = useState("");
  const selectable = availableCandidateIds.filter(
    (candidateId) => !group.candidate_input_ids.includes(candidateId),
  );

  const updateGroup = (patch: Partial<AggregateGroup>) => {
    const groups = [...config.groups];
    groups[groupIndex] = { ...group, ...patch };
    onCommit({ ...config, groups });
  };

  return (
    <article
      aria-label={`聚合分组 ${groupIndex + 1}`}
      className="border-border space-y-3 rounded-md border p-3"
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-sm font-medium">分组 {groupIndex + 1}</p>
          <p className="text-muted-foreground text-[11px]">
            stable output: <code>{group.id}</code>
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          <Button
            aria-label={`上移聚合分组 ${groupIndex + 1}`}
            disabled={locked || groupIndex === 0}
            onClick={() =>
              onCommit(moveAggregateGroup(config, groupIndex, groupIndex - 1))
            }
            size="sm"
            type="button"
            variant="ghost"
          >
            上移
          </Button>
          <Button
            aria-label={`下移聚合分组 ${groupIndex + 1}`}
            disabled={locked || groupIndex === config.groups.length - 1}
            onClick={() =>
              onCommit(moveAggregateGroup(config, groupIndex, groupIndex + 1))
            }
            size="sm"
            type="button"
            variant="ghost"
          >
            下移
          </Button>
          <Button
            aria-label={`删除聚合分组 ${groupIndex + 1}`}
            disabled={locked || config.groups.length <= 1 || outputReferenced}
            onClick={() => onCommit(removeAggregateGroup(config, groupIndex))}
            size="sm"
            type="button"
            variant="ghost"
          >
            删除
          </Button>
        </div>
      </header>

      <label className="block space-y-1 text-xs">
        <span>分组名称</span>
        <Input
          aria-label={`聚合分组 ${groupIndex + 1} 名称`}
          disabled={locked}
          maxLength={128}
          onChange={(event) => {
            if (event.currentTarget.value) {
              updateGroup({ name: event.currentTarget.value });
            }
          }}
          value={group.name}
        />
      </label>
      <WorkflowValueTypeEditor
        disabled={locked}
        label={`聚合分组 ${groupIndex + 1} value type`}
        onChange={(valueType) => updateGroup({ value_type: valueType })}
        value={group.value_type}
      />
      <section className="space-y-2" aria-label="有序 candidates">
        <p className="text-xs font-medium">有序 candidate input IDs</p>
        {group.candidate_input_ids.map((candidateId, candidateIndex) => (
          <div
            className="bg-muted/40 flex flex-wrap items-center gap-2 rounded-md p-2"
            key={`${candidateId}:${candidateIndex}`}
          >
            <code className="min-w-0 flex-1 truncate text-xs">
              {candidateId}
            </code>
            <Button
              aria-label={`上移 candidate ${candidateId}`}
              disabled={locked || candidateIndex === 0}
              onClick={() =>
                onCommit(
                  moveAggregateCandidate(
                    config,
                    groupIndex,
                    candidateIndex,
                    candidateIndex - 1,
                  ),
                )
              }
              size="sm"
              type="button"
              variant="ghost"
            >
              上移
            </Button>
            <Button
              aria-label={`下移 candidate ${candidateId}`}
              disabled={
                locked ||
                candidateIndex === group.candidate_input_ids.length - 1
              }
              onClick={() =>
                onCommit(
                  moveAggregateCandidate(
                    config,
                    groupIndex,
                    candidateIndex,
                    candidateIndex + 1,
                  ),
                )
              }
              size="sm"
              type="button"
              variant="ghost"
            >
              下移
            </Button>
            <Button
              aria-label={`删除 candidate ${candidateId}`}
              disabled={locked}
              onClick={() =>
                onCommit(
                  removeAggregateCandidate(config, groupIndex, candidateIndex),
                )
              }
              size="sm"
              type="button"
              variant="ghost"
            >
              删除
            </Button>
          </div>
        ))}
        <div className="flex gap-2">
          <select
            aria-label={`聚合分组 ${groupIndex + 1} 待添加 candidate`}
            className="border-input h-9 min-w-0 flex-1 rounded-md border bg-transparent px-2 text-sm"
            disabled={locked || selectable.length === 0}
            onChange={(event) => setCandidateToAdd(event.currentTarget.value)}
            value={candidateToAdd}
          >
            <option value="">选择已绑定输入</option>
            {selectable.map((candidateId) => (
              <option key={candidateId} value={candidateId}>
                {candidateId}
              </option>
            ))}
          </select>
          <Button
            disabled={
              locked ||
              !candidateToAdd ||
              group.candidate_input_ids.length >= maxCandidates
            }
            onClick={() => {
              onCommit(
                appendAggregateCandidate(
                  config,
                  groupIndex,
                  candidateToAdd,
                  maxCandidates,
                ),
              );
              setCandidateToAdd("");
            }}
            size="sm"
            type="button"
            variant="outline"
          >
            添加 candidate
          </Button>
        </div>
      </section>
      {outputReferenced ? (
        <p className="text-muted-foreground text-xs" role="status">
          该 stable output 已被引用，需先清理引用才能删除分组。
        </p>
      ) : null}
    </article>
  );
}

export function VariableAggregateNodeConfigPanel(
  props: WorkflowNodeConfigPanelProps,
) {
  const store = useWorkflowWorkbenchStore();
  const locked = panelLocked(props);
  const config = readAggregateConfig(props.node.config);
  const publicLimits = props.catalogEntry.public_limits;
  const maxGroups = Math.min(
    AGGREGATE_NATIVE_MAX_GROUPS,
    publicLimits?.max_aggregate_groups ?? AGGREGATE_NATIVE_MAX_GROUPS,
  );
  const maxCandidates = Math.min(
    AGGREGATE_NATIVE_MAX_CANDIDATES,
    publicLimits?.max_aggregate_candidates ?? AGGREGATE_NATIVE_MAX_CANDIDATES,
  );
  const candidateIds = Object.keys(recordOrEmpty(props.node.input_bindings));
  const issues = aggregateIssues(props, config, maxGroups, maxCandidates);

  const commit = (next: VariableAggregateNodeConfigV1) => {
    if (locked || next === config) return;
    store.dispatch({
      type: "update_node_config",
      node_id: props.nodeId,
      config: next,
    });
  };

  return (
    <BranchLoopPanelShell
      disabled={props.disabled}
      issues={issues}
      readOnly={props.readOnly}
      title="变量聚合"
    >
      <section
        aria-label="exclusive branch 策略"
        className="border-border bg-muted/30 space-y-1 rounded-md border p-3"
      >
        <p className="text-sm font-medium">exclusive_branch（固定）</p>
        <p className="text-muted-foreground text-xs">
          互斥分支，恰好一个 candidate 可用；多个 present 时 fail
          closed，不按顺序选择第一个。
        </p>
        <p className="text-muted-foreground text-xs">
          每组 candidate 必须同型且 schema 兼容。MISSING 表示值不存在；JSON null
          是已存在的值，二者不可混同。
        </p>
      </section>

      <div className="flex items-center justify-between gap-3">
        <div>
          <h4 className="text-sm font-medium">聚合分组</h4>
          <p className="text-muted-foreground text-xs">
            最多 {maxGroups} 个分组；每组最多 {maxCandidates} 个 candidate。
          </p>
        </div>
        <Button
          disabled={locked || config.groups.length >= maxGroups}
          onClick={() => {
            const id = stableSemanticId("aggregate");
            commit(
              appendAggregateGroup(
                config,
                {
                  id,
                  name: `group_${config.groups.length + 1}`,
                  value_type: safeValueType(undefined),
                  candidate_input_ids: [],
                },
                maxGroups,
              ),
            );
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          添加分组
        </Button>
      </div>

      {config.groups.map((group, groupIndex) => (
        <AggregateGroupEditor
          availableCandidateIds={candidateIds}
          config={config}
          group={group}
          groupIndex={groupIndex}
          key={group.id}
          locked={locked}
          maxCandidates={maxCandidates}
          onCommit={commit}
          outputReferenced={outputReferenceExists(props, group.id)}
        />
      ))}
    </BranchLoopPanelShell>
  );
}
