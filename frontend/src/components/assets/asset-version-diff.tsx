import type { AssetVersion } from "@/core/shared-assets";

type DiffRow = {
  label: string;
  previous: string;
  current: string;
};

function value(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function list(items: readonly string[]): string {
  return items.length === 0 ? "—" : items.join("、");
}

function describe(version: AssetVersion): Record<string, string> {
  const common = {
    载荷校验和: value(
      "payload_checksum" in version ? version.payload_checksum : undefined,
    ),
  };
  if ("agent_id" in version) {
    return {
      ...common,
      描述: value(version.description),
      模型: value(version.model_ref),
      工具组: list(version.tool_groups),
      "Skill 版本": list(version.skill_version_ids),
      "MCP 版本": list(version.mcp_version_ids),
    };
  }
  if ("skill_id" in version) {
    return {
      ...common,
      描述: value(version.description),
      兼容性: value(version.compatibility),
      扫描结论: value(version.scan_decision),
      扫描规则: list(version.scan_rule_ids),
      文件: version.file_views
        .map(
          (file) =>
            `${file.path} · ${file.size_bytes} B · ${file.media_type} · ${file.sha256}`,
        )
        .join("\n"),
      凭据要求: version.secret_requirements
        .map((item) => `${item.name}${item.optional ? "（可选）" : "（必需）"}`)
        .join("、"),
    };
  }
  if ("mcp_server_id" in version) {
    return {
      ...common,
      描述: value(version.definition.description),
      传输方式: version.definition.transport,
      命令: value(version.definition.command),
      URL: value(version.definition.url),
      参数: list(version.definition.args),
      超时: `${version.definition.timeout_seconds} 秒`,
      "Credential 槽位": version.credential_slots
        .map(
          (slot) =>
            `${slot.name}${slot.required ? "（必需）" : "（可选）"} · ${slot.purpose || "无说明"}`,
        )
        .join("\n"),
    };
  }
  return {
    状态: version.status,
    载荷结构版本: String(version.payload_schema_version),
    载荷字段: Object.entries(version.payload_schema)
      .map(([group, fields]) => `${group}: ${fields.join("、")}`)
      .join("\n"),
  };
}

function diffRows(
  previous: AssetVersion | null,
  current: AssetVersion,
): DiffRow[] {
  const before = previous ? describe(previous) : {};
  const after = describe(current);
  return Object.entries(after)
    .filter(([key, currentValue]) => before[key] !== currentValue)
    .map(([label, currentValue]) => ({
      label,
      previous: before[label] ?? "—",
      current: currentValue || "—",
    }));
}

export function AssetVersionDiff({
  previous = null,
  current,
}: {
  previous?: AssetVersion | null;
  current: AssetVersion;
}) {
  const rows = diffRows(previous, current);
  if (rows.length === 0) {
    return <p className="text-muted-foreground text-sm">没有结构化变化。</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[36rem] text-left text-xs">
        <thead className="text-muted-foreground">
          <tr className="border-b">
            <th className="px-2 py-2 font-medium">字段</th>
            <th className="px-2 py-2 font-medium">上一版本</th>
            <th className="px-2 py-2 font-medium">当前版本</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label} className="border-b last:border-0">
              <th className="px-2 py-2 align-top font-medium">{row.label}</th>
              <td className="text-muted-foreground px-2 py-2 align-top whitespace-pre-wrap">
                {row.previous}
              </td>
              <td className="px-2 py-2 align-top whitespace-pre-wrap">
                {row.current}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
