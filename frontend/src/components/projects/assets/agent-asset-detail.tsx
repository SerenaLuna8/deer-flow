import { Badge } from "@/components/ui/badge";
import type { AssetVersion } from "@/core/shared-assets";

export type AgentAssetVersion = Extract<AssetVersion, { agent_id: string }>;

function StringList({ values }: { values: readonly string[] }) {
  if (values.length === 0) {
    return <span className="text-muted-foreground">未配置</span>;
  }
  return (
    <div className="flex flex-wrap gap-2">
      {values.map((value) => (
        <Badge
          key={value}
          variant="secondary"
          className="font-mono font-normal"
        >
          {value}
        </Badge>
      ))}
    </div>
  );
}

export function AgentAssetDetail({ version }: { version: AgentAssetVersion }) {
  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">Agent 说明</h3>
        <p className="text-muted-foreground text-sm leading-6 whitespace-pre-wrap">
          {version.description || "未填写说明。"}
        </p>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold">角色设定（Soul）</h3>
        <div className="bg-muted/45 rounded-xl px-4 py-3 text-sm leading-6 whitespace-pre-wrap">
          {version.soul || "未配置角色设定。"}
        </div>
      </section>

      <section className="grid gap-4 sm:grid-cols-2">
        <div className="border-border/70 rounded-xl border p-4">
          <p className="text-muted-foreground text-xs">模型引用</p>
          <p className="mt-2 font-mono text-sm break-all">
            {version.model_ref || "未配置"}
          </p>
        </div>
        <div className="border-border/70 rounded-xl border p-4">
          <p className="text-muted-foreground text-xs">工具组</p>
          <div className="mt-2">
            <StringList values={version.tool_groups} />
          </div>
        </div>
      </section>

      <section className="space-y-4">
        <div>
          <h3 className="text-sm font-semibold">Skill 依赖</h3>
          <p className="text-muted-foreground mt-1 text-xs">
            当前接口仅提供固定的 Skill 版本 ID。
          </p>
          <div className="mt-2">
            <StringList values={version.skill_version_ids} />
          </div>
        </div>
        <div>
          <h3 className="text-sm font-semibold">MCP 依赖</h3>
          <p className="text-muted-foreground mt-1 text-xs">
            当前接口仅提供固定的 MCP 版本 ID。
          </p>
          <div className="mt-2">
            <StringList values={version.mcp_version_ids} />
          </div>
        </div>
      </section>
    </div>
  );
}
