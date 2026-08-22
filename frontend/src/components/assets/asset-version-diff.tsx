import { useI18n } from "@/core/i18n/hooks";
import type { Translations } from "@/core/i18n/locales/types";
import { resolveModelDisplayName } from "@/core/models/presentation";
import type { Model } from "@/core/models/types";
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

function list(items: readonly string[], separator: string): string {
  return items.length === 0 ? "—" : items.join(separator);
}

function jsonObject(value: Record<string, unknown> | undefined): string {
  const document = value ?? {};
  return JSON.stringify(
    document,
    Object.keys(document).sort((left, right) => left.localeCompare(right)),
    2,
  );
}

type DiffCopy = Translations["adminAssets"]["diff"];
type StatusCopy = Translations["adminAssets"]["status"];

function describe(
  version: AssetVersion,
  copy: DiffCopy,
  statuses: StatusCopy,
  separator: string,
  includeAgentDocuments: boolean,
  models: readonly Model[],
  unavailableModelLabel: string,
): Record<string, string> {
  const common = {
    [copy.payloadChecksum]: value(
      "payload_checksum" in version ? version.payload_checksum : undefined,
    ),
  };
  if ("agent_id" in version) {
    const modelLabel =
      resolveModelDisplayName(version.model_ref, models) ??
      unavailableModelLabel;
    return {
      ...common,
      [copy.description]: value(version.description),
      [copy.payloadSchemaVersion]: String(version.payload_schema_version),
      [copy.model]: `${modelLabel}\n${jsonObject(version.model_settings)}`,
      [copy.toolGroups]: list(version.tool_groups, separator),
      [copy.skillVersions]: list(
        version.skill_refs.map((ref) => `${ref.scope}:${ref.asset_id}`),
        separator,
      ),
      [copy.mcpVersions]: list(version.mcp_version_ids, separator),
      ...(includeAgentDocuments
        ? {
            "AGENTS.md": value(version.agents_instructions),
            "SOUL.md": value(version.soul),
            "IDENTITY.md": value(version.identity),
            "USER.md": value(version.user_context),
          }
        : {}),
    };
  }
  if ("skill_id" in version) {
    return {
      ...common,
      [copy.description]: value(version.description),
      [copy.compatibility]: value(version.compatibility),
      [copy.scanDecision]: value(version.scan_decision),
      [copy.scanRules]: list(version.scan_rule_ids, separator),
      [copy.files]: version.file_views
        .map(
          (file) =>
            `${file.path} · ${file.size_bytes} B · ${file.media_type} · ${file.sha256}`,
        )
        .join("\n"),
      [copy.secretRequirements]: version.secret_requirements
        .map(
          (item) =>
            `${item.name}（${item.optional ? copy.optional : copy.required}）`,
        )
        .join(separator),
    };
  }
  if ("mcp_server_id" in version) {
    return {
      ...common,
      [copy.description]: value(version.definition.description),
      [copy.transport]: version.definition.transport,
      [copy.command]: value(version.definition.command),
      [copy.url]: value(version.definition.url),
      [copy.arguments]: list(version.definition.args, separator),
      [copy.timeout]: copy.seconds(version.definition.timeout_seconds),
      [copy.secretSlots]: version.secret_slots
        .map(
          (slot) =>
            `${slot.name}（${slot.required ? copy.required : copy.optional}） · ${slot.purpose || copy.noDescription}`,
        )
        .join("\n"),
    };
  }
  return {};
}

function diffRows(
  previous: AssetVersion | null,
  current: AssetVersion,
  copy: DiffCopy,
  statuses: StatusCopy,
  separator: string,
  includeAgentDocuments: boolean,
  models: readonly Model[],
  unavailableModelLabel: string,
): DiffRow[] {
  const before = previous
    ? describe(
        previous,
        copy,
        statuses,
        separator,
        includeAgentDocuments,
        models,
        unavailableModelLabel,
      )
    : {};
  const after = describe(
    current,
    copy,
    statuses,
    separator,
    includeAgentDocuments,
    models,
    unavailableModelLabel,
  );
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
  includeAgentDocuments = false,
  models,
}: {
  previous?: AssetVersion | null;
  current: AssetVersion;
  includeAgentDocuments?: boolean;
  models: readonly Model[];
}) {
  const { locale, t } = useI18n();
  const isMcp = "mcp_server_id" in current;
  const rows = diffRows(
    previous,
    current,
    t.adminAssets.diff,
    t.adminAssets.status,
    locale === "zh-CN" ? "、" : ", ",
    includeAgentDocuments,
    models,
    t.adminSystemSettings.fields.unavailableModel,
  );
  if (rows.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        {t.adminAssets.diff.noChanges}
      </p>
    );
  }
  const currentOnly = isMcp && previous === null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[36rem] text-left text-xs">
        <thead className="text-muted-foreground">
          <tr className="border-b">
            <th className="px-2 py-2 font-medium">
              {t.adminAssets.diff.field}
            </th>
            {!currentOnly ? (
              <th className="px-2 py-2 font-medium">
                {isMcp
                  ? t.adminAssets.diff.previousMcpConfiguration
                  : t.adminAssets.diff.previous}
              </th>
            ) : null}
            <th className="px-2 py-2 font-medium">
              {isMcp
                ? t.adminAssets.diff.currentMcpConfiguration
                : t.adminAssets.diff.current}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label} className="border-b last:border-0">
              <th className="px-2 py-2 align-top font-medium">{row.label}</th>
              {!currentOnly ? (
                <td className="text-muted-foreground px-2 py-2 align-top whitespace-pre-wrap">
                  {row.previous}
                </td>
              ) : null}
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
