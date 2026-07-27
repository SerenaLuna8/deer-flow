import type { AssetScope, AssetVersion } from "./types";

export const MCP_RUNTIME_TRANSPORTS = ["sse", "http"] as const;
export const SYSTEM_MCP_RUNTIME_TRANSPORTS = ["stdio", "sse", "http"] as const;

export type McpRuntimeTransport = (typeof MCP_RUNTIME_TRANSPORTS)[number];
export type McpAssetVersion = Extract<AssetVersion, { mcp_server_id: string }>;
export type ScopedMcpVersion = {
  scope: AssetScope;
  version: McpAssetVersion;
};

export const UNSUPPORTED_MCP_VERSION_MESSAGE =
  "当前仅支持 SSE 或 HTTP。此历史版本可以查看，但不能发布、绑定或用于 Agent。";
export const UNSUPPORTED_SYSTEM_MCP_VERSION_MESSAGE =
  "当前 Private runtime 仅支持 stdio、SSE 或 HTTP。此系统历史版本可以查看，但不能绑定或用于 Agent。";

export function isMcpRuntimeTransport(
  transport: string,
): transport is McpRuntimeTransport {
  return MCP_RUNTIME_TRANSPORTS.includes(transport as McpRuntimeTransport);
}

function isProjectRemoteMcpUrl(value: string): boolean {
  if (value.length === 0 || value !== value.trim() || value.includes("\\"))
    return false;
  try {
    const parsed = new URL(value);
    const hostname = parsed.hostname.toLowerCase().replace(/\.$/u, "");
    return (
      parsed.protocol === "https:" &&
      !parsed.username &&
      !parsed.password &&
      !parsed.search &&
      !parsed.hash &&
      parsed.port !== "0" &&
      hostname.includes(".") &&
      hostname !== "localhost" &&
      !hostname.endsWith(".localhost") &&
      !hostname.includes("*") &&
      !hostname.includes(":") &&
      !/^\d+(?:\.\d+){3}$/u.test(hostname)
    );
  } catch {
    return false;
  }
}

export function mcpVersionRuntimeBlockReason(
  version: Pick<McpAssetVersion, "definition" | "credential_slots">,
  scope: AssetScope,
): string | null {
  const transport = version.definition.transport;
  const credentialSlots =
    version.credential_slots.length > 0
      ? version.credential_slots
      : version.definition.credential_slots;
  const credentialSchemas = credentialSlots.map((slot) =>
    Object.keys(slot.payload_schema),
  );

  if (scope === "project") {
    if (!isMcpRuntimeTransport(transport)) {
      return UNSUPPORTED_MCP_VERSION_MESSAGE;
    }
    if (!version.definition.url?.trim()) {
      return "当前传输方式缺少 URL。此历史版本可以查看，但不能发布、绑定或用于 Agent。";
    }
    if (!isProjectRemoteMcpUrl(version.definition.url)) {
      return "当前 Project MCP 需要无凭据、无查询参数的绝对 HTTPS URL。此历史版本可以查看，但不能发布、绑定或用于 Agent。";
    }
    if (Object.keys(version.definition.oauth).length > 0) {
      return "当前 Project MCP 不支持版本内 OAuth 配置。此历史版本可以查看，但不能发布、绑定或用于 Agent。";
    }
    if (
      credentialSchemas.some(
        (sections) => sections.length !== 1 || sections[0] !== "headers",
      )
    ) {
      return "当前 Project MCP Credential 槽位仅支持 headers。此历史版本可以查看，但不能发布、绑定或用于 Agent。";
    }
    const seenHeaderNames = new Set<string>();
    for (const slot of credentialSlots) {
      for (const headerName of slot.payload_schema.headers ?? []) {
        const normalizedName = headerName.toLowerCase();
        if (seenHeaderNames.has(normalizedName)) {
          return "当前 Project MCP Credential 槽位不能重复声明请求头。此历史版本可以查看，但不能发布、绑定或用于 Agent。";
        }
        seenHeaderNames.add(normalizedName);
      }
    }
    return null;
  }

  if (
    !(SYSTEM_MCP_RUNTIME_TRANSPORTS as readonly string[]).includes(transport)
  ) {
    return UNSUPPORTED_SYSTEM_MCP_VERSION_MESSAGE;
  }
  if (transport === "stdio" && !version.definition.command?.trim()) {
    return "当前 stdio 系统 MCP 缺少 command，不能绑定或用于 Agent。";
  }
  if (
    (transport === "sse" || transport === "http") &&
    !version.definition.url?.trim()
  ) {
    return "当前远程系统 MCP 缺少 URL，不能绑定或用于 Agent。";
  }
  const allowedCredentialSection =
    transport === "stdio" ? new Set(["env"]) : new Set(["headers", "oauth"]);
  if (
    credentialSchemas.some(
      (sections) =>
        sections.length === 0 ||
        sections.some((section) => !allowedCredentialSection.has(section)),
    )
  ) {
    return transport === "stdio"
      ? "当前 stdio 系统 MCP Credential 槽位仅支持 env，不能绑定或用于 Agent。"
      : "当前远程系统 MCP Credential 槽位仅支持 headers 或 oauth，不能绑定或用于 Agent。";
  }
  return null;
}

export function supportedMcpVersionIds(
  versions: readonly ScopedMcpVersion[],
): ReadonlySet<string> {
  return new Set(
    versions
      .filter(
        ({ scope, version }) =>
          mcpVersionRuntimeBlockReason(version, scope) === null,
      )
      .map(({ version }) => version.id),
  );
}

export function mcpDependencyRuntimeBlockReason(
  requiredVersionIds: readonly string[],
  versions: readonly ScopedMcpVersion[],
): string | null {
  if (requiredVersionIds.length === 0) return null;
  const byId = new Map(versions.map((entry) => [entry.version.id, entry]));
  for (const versionId of requiredVersionIds) {
    const entry = byId.get(versionId);
    if (!entry) {
      return "Agent 引用的 MCP 版本无法确认，请先改用当前项目可用的 MCP 版本。";
    }
    const reason = mcpVersionRuntimeBlockReason(entry.version, entry.scope);
    if (reason) {
      return `该 MCP 版本当前不能作为 Agent 依赖：${reason}`;
    }
  }
  return null;
}
