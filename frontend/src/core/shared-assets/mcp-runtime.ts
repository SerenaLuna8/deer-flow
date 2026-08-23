import {
  isSafeConfiguredProjectMcpUrl,
  type AssetScope,
  type AssetVersion,
} from "./types";

export const MCP_RUNTIME_TRANSPORTS = ["sse", "http"] as const;
export const SYSTEM_MCP_RUNTIME_TRANSPORTS = ["stdio", "sse", "http"] as const;

const PROJECT_MCP_HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/u;
const PROJECT_MCP_QUERY_NAME = /^[A-Za-z0-9._~-]{1,128}$/u;
const FORBIDDEN_PROJECT_MCP_HEADERS = new Set([
  "connection",
  "content-length",
  "host",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

export type McpRuntimeTransport = (typeof MCP_RUNTIME_TRANSPORTS)[number];
export type McpAssetVersion = Extract<AssetVersion, { mcp_server_id: string }>;
export type ScopedMcpVersion = {
  scope: AssetScope;
  version: McpAssetVersion;
};

export type McpRuntimeBlockMessages = {
  unsupportedProjectTransport: string;
  unsupportedSystemTransport: string;
  missingProjectUrl: string;
  invalidProjectUrl: string;
  projectOAuth: string;
  projectCredentialTargetsOnly: string;
  missingSystemCommand: string;
  missingSystemUrl: string;
  systemEnvOnly: string;
  systemRemoteSecretsOnly: string;
};

export const UNSUPPORTED_MCP_VERSION_MESSAGE =
  "当前仅支持 SSE 或 HTTP。此历史配置可以查看，但不能发布、绑定或用于 Agent。";
export const UNSUPPORTED_SYSTEM_MCP_VERSION_MESSAGE =
  "当前 Private runtime 仅支持 stdio、SSE 或 HTTP。此系统历史配置可以查看，但不能绑定或用于 Agent。";

export function isForbiddenProjectMcpHeaderName(name: string): boolean {
  return FORBIDDEN_PROJECT_MCP_HEADERS.has(name.toLowerCase());
}

export function isValidProjectMcpCredentialName(
  target: "headers" | "query",
  name: string,
): boolean {
  return target === "headers"
    ? name.length <= 255 &&
        PROJECT_MCP_HEADER_NAME.test(name) &&
        !isForbiddenProjectMcpHeaderName(name)
    : PROJECT_MCP_QUERY_NAME.test(name);
}

function projectMcpSecretSchemasAreSupported(
  schemas: readonly Record<string, string[]>[],
): boolean {
  const headerNames = new Set<string>();
  const queryNames = new Set<string>();
  for (const schema of schemas) {
    const sections = Object.entries(schema);
    if (sections.length === 0) return false;
    for (const [section, fields] of sections) {
      if (
        (section !== "headers" && section !== "query") ||
        fields.length === 0
      ) {
        return false;
      }
      const destination = section === "headers" ? headerNames : queryNames;
      for (const field of fields) {
        const comparable = section === "headers" ? field.toLowerCase() : field;
        if (
          !isValidProjectMcpCredentialName(section, field) ||
          destination.has(comparable)
        ) {
          return false;
        }
        destination.add(comparable);
      }
    }
  }
  return true;
}

const DEFAULT_RUNTIME_BLOCK_MESSAGES: McpRuntimeBlockMessages = {
  unsupportedProjectTransport: UNSUPPORTED_MCP_VERSION_MESSAGE,
  unsupportedSystemTransport: UNSUPPORTED_SYSTEM_MCP_VERSION_MESSAGE,
  missingProjectUrl:
    "当前传输方式缺少 URL。此历史配置可以查看，但不能发布、绑定或用于 Agent。",
  invalidProjectUrl:
    "当前 Project MCP 需要无内嵌凭据、无查询参数或片段的绝对 HTTP 或 HTTPS URL，主机仅支持精确的 localhost 或规范格式的 IPv4/IPv6 字面量，不解析普通 DNS 主机名。localhost 大小写不敏感并按 127.0.0.1 处理，IPv6 请显式填写 [::1]；IP 必须属于管理员配置的允许网段。此历史配置可以查看，但不能发布、绑定或用于 Agent。",
  projectOAuth:
    "当前 Project MCP 不支持配置内 OAuth。此历史配置可以查看，但不能发布、绑定或用于 Agent。",
  projectCredentialTargetsOnly:
    "当前 Project MCP 的凭证参数只能发送到请求头（Header）或查询参数（Query）。此历史配置可以查看，但不能发布、绑定或用于 Agent。",
  missingSystemCommand:
    "当前 stdio 系统 MCP 缺少 command，不能绑定或用于 Agent。",
  missingSystemUrl: "当前远程系统 MCP 缺少 URL，不能绑定或用于 Agent。",
  systemEnvOnly:
    "当前 stdio 系统 MCP Secret 槽位仅支持 env，不能绑定或用于 Agent。",
  systemRemoteSecretsOnly:
    "当前远程系统 MCP Secret 槽位仅支持 headers、query 或 oauth，不能绑定或用于 Agent。",
};

export function isMcpRuntimeTransport(
  transport: string,
): transport is McpRuntimeTransport {
  return MCP_RUNTIME_TRANSPORTS.includes(transport as McpRuntimeTransport);
}

export function mcpVersionRuntimeBlockReason(
  version: Pick<McpAssetVersion, "definition" | "secret_slots">,
  scope: AssetScope,
  messages: McpRuntimeBlockMessages = DEFAULT_RUNTIME_BLOCK_MESSAGES,
): string | null {
  const transport = version.definition.transport;
  const definitionSecretSchemas = version.definition.secret_slots.map(
    (slot) => slot.payload_schema,
  );
  const storedSecretSchemas = version.secret_slots.map(
    (slot) => slot.payload_schema,
  );
  const secretSchemas = [
    ...definitionSecretSchemas,
    ...storedSecretSchemas,
  ].map((schema) => Object.keys(schema));

  if (scope === "project") {
    if (!isMcpRuntimeTransport(transport)) {
      return messages.unsupportedProjectTransport;
    }
    if (!version.definition.url?.trim()) {
      return messages.missingProjectUrl;
    }
    if (!isSafeConfiguredProjectMcpUrl(version.definition.url)) {
      return messages.invalidProjectUrl;
    }
    if (Object.keys(version.definition.oauth).length > 0) {
      return messages.projectOAuth;
    }
    if (
      !projectMcpSecretSchemasAreSupported(definitionSecretSchemas) ||
      !projectMcpSecretSchemasAreSupported(storedSecretSchemas)
    ) {
      return messages.projectCredentialTargetsOnly;
    }
    return null;
  }

  if (
    !(SYSTEM_MCP_RUNTIME_TRANSPORTS as readonly string[]).includes(transport)
  ) {
    return messages.unsupportedSystemTransport;
  }
  if (transport === "stdio" && !version.definition.command?.trim()) {
    return messages.missingSystemCommand;
  }
  if (
    (transport === "sse" || transport === "http") &&
    !version.definition.url?.trim()
  ) {
    return messages.missingSystemUrl;
  }
  const allowedSecretSection =
    transport === "stdio"
      ? new Set(["env"])
      : new Set(["headers", "query", "oauth"]);
  if (
    secretSchemas.some(
      (sections) =>
        sections.length === 0 ||
        sections.some((section) => !allowedSecretSection.has(section)),
    )
  ) {
    return transport === "stdio"
      ? messages.systemEnvOnly
      : messages.systemRemoteSecretsOnly;
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
      return "Agent 引用的 MCP 配置无法确认，请先改用当前项目可用的 MCP 配置。";
    }
    const reason = mcpVersionRuntimeBlockReason(entry.version, entry.scope);
    if (reason) {
      return `该 MCP 配置当前不能作为 Agent 依赖：${reason}`;
    }
  }
  return null;
}
