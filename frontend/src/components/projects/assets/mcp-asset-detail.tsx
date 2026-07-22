import { Badge } from "@/components/ui/badge";
import type { AssetVersion } from "@/core/shared-assets";

export type McpAssetVersion = Extract<AssetVersion, { mcp_server_id: string }>;

function StringMap({ value }: { value: Record<string, string> }) {
  const entries = Object.entries(value);
  if (entries.length === 0) {
    return <p className="text-muted-foreground text-sm">未配置</p>;
  }
  return (
    <dl className="divide-border/70 overflow-hidden rounded-lg border text-xs">
      {entries.map(([key, item]) => (
        <div
          key={key}
          className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] gap-3 px-3 py-2"
        >
          <dt className="font-mono break-all">{key}</dt>
          <dd className="text-muted-foreground font-mono break-all">{item}</dd>
        </div>
      ))}
    </dl>
  );
}

function JsonConfig({ value }: { value: Record<string, unknown> }) {
  return Object.keys(value).length === 0 ? (
    <p className="text-muted-foreground text-sm">未配置</p>
  ) : (
    <pre className="bg-muted/45 overflow-x-auto rounded-lg p-3 text-xs">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function McpAssetDetail({ version }: { version: McpAssetVersion }) {
  const definition = version.definition;
  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">MCP 说明</h3>
        <p className="text-muted-foreground text-sm leading-6 whitespace-pre-wrap">
          {definition.description || "未填写说明。"}
        </p>
      </section>

      <section className="grid gap-4 sm:grid-cols-2">
        <div className="border-border/70 rounded-xl border p-4">
          <p className="text-muted-foreground text-xs">传输方式</p>
          <p className="mt-2 font-mono text-sm">{definition.transport}</p>
        </div>
        <div className="border-border/70 rounded-xl border p-4">
          <p className="text-muted-foreground text-xs">超时</p>
          <p className="mt-2 text-sm">{definition.timeout_seconds} 秒</p>
        </div>
        {definition.command && (
          <div className="border-border/70 rounded-xl border p-4 sm:col-span-2">
            <p className="text-muted-foreground text-xs">命令与参数</p>
            <p className="mt-2 font-mono text-sm break-all">
              {[definition.command, ...definition.args].join(" ")}
            </p>
          </div>
        )}
        {definition.url && (
          <div className="border-border/70 rounded-xl border p-4 sm:col-span-2">
            <p className="text-muted-foreground text-xs">URL</p>
            <p className="mt-2 font-mono text-sm break-all">{definition.url}</p>
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h3 className="text-sm font-semibold">Credential 槽位</h3>
        {version.credential_slots.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            此版本不需要 Credential。
          </p>
        ) : (
          <div className="space-y-3">
            {version.credential_slots.map((slot) => {
              const grants = version.credential_grants.filter(
                (grant) => grant.credential_slot_id === slot.id,
              );
              const active = grants.some((grant) => grant.status === "active");
              const revoked =
                !active && grants.some((grant) => grant.status === "revoked");
              return (
                <div
                  key={slot.id}
                  className="border-border/70 rounded-xl border p-4"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <p className="font-medium">{slot.name}</p>
                      <p className="text-muted-foreground mt-1 text-xs">
                        {slot.purpose || "未填写用途"} ·{" "}
                        {slot.required ? "必需" : "可选"}
                      </p>
                    </div>
                    <Badge variant={active ? "default" : "secondary"}>
                      {active ? "已授权" : revoked ? "已撤销" : "未授权"}
                    </Badge>
                  </div>
                  <div className="mt-3 space-y-1 text-xs">
                    {Object.entries(slot.payload_schema).map(
                      ([group, fields]) => (
                        <p key={group}>
                          <span className="text-muted-foreground">{group}</span>
                          : {fields.join("、")}
                        </p>
                      ),
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <details className="border-border/70 rounded-xl border px-4 py-3">
        <summary className="cursor-pointer text-sm font-medium">
          非敏感配置
        </summary>
        <div className="mt-4 space-y-4">
          <div className="space-y-2">
            <p className="text-xs font-medium">环境变量</p>
            <StringMap value={definition.env} />
          </div>
          <div className="space-y-2">
            <p className="text-xs font-medium">请求头</p>
            <StringMap value={definition.headers} />
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-2">
              <p className="text-xs font-medium">OAuth</p>
              <JsonConfig value={definition.oauth} />
            </div>
            <div className="space-y-2">
              <p className="text-xs font-medium">路由</p>
              <JsonConfig value={definition.routing} />
            </div>
            <div className="space-y-2">
              <p className="text-xs font-medium">工具覆盖</p>
              <JsonConfig value={definition.tool_overrides} />
            </div>
          </div>
        </div>
      </details>
    </div>
  );
}
