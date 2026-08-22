"""MCP client using langchain-mcp-adapters."""

import logging
import sys
from typing import Any

from deerflow.mcp.config import ExtensionsConfig, McpServerConfig

logger = logging.getLogger(__name__)

_HOST_ENV_REFERENCE_PREFIX = "${"
_POSIX_STDIO_ENV = {
    "HOME": "/tmp",
    "LOGNAME": "actweave-mcp",
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    "SHELL": "/bin/sh",
    "TERM": "dumb",
    "USER": "actweave-mcp",
}
_WINDOWS_STDIO_ENV = {
    "APPDATA": r"C:\Windows\Temp",
    "HOMEDRIVE": "C:",
    "HOMEPATH": r"\Windows\Temp",
    "LOCALAPPDATA": r"C:\Windows\Temp",
    "PATH": r"C:\Windows\System32;C:\Windows",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
    "PROCESSOR_ARCHITECTURE": "",
    "SYSTEMDRIVE": "C:",
    "SYSTEMROOT": r"C:\Windows",
    "TEMP": r"C:\Windows\Temp",
    "USERNAME": "actweave-mcp",
    "USERPROFILE": r"C:\Windows\Temp",
}


def _isolated_stdio_environment(values: dict[str, str]) -> dict[str, str]:
    if any(_HOST_ENV_REFERENCE_PREFIX in value for value in values.values()):
        raise ValueError("stdio MCP environment cannot reference Worker host variables")
    base = _WINDOWS_STDIO_ENV if sys.platform == "win32" else _POSIX_STDIO_ENV
    return {**base, **values}


def build_server_params(server_name: str, config: McpServerConfig) -> dict[str, Any]:
    """Build server parameters for MultiServerMCPClient.

    Args:
        server_name: Name of the MCP server.
        config: Configuration for the MCP server.

    Returns:
        Dictionary of server parameters for langchain-mcp-adapters.
    """
    transport_type = config.type or "stdio"
    params: dict[str, Any] = {"transport": transport_type}

    if transport_type == "stdio":
        if not config.command:
            raise ValueError(f"MCP server '{server_name}' with stdio transport requires 'command' field")
        params["command"] = config.command
        params["args"] = config.args
        # The MCP SDK otherwise inherits a host allowlist (including HOME and
        # PATH), while langchain-mcp-adapters expands `${VAR}` from the Worker
        # process.  Always provide a complete, fixed child environment and
        # reject expansion syntax so only the admitted definition plus its
        # execution-boundary secret injection can reach the child process.
        params["env"] = _isolated_stdio_environment(config.env)
    elif transport_type in ("sse", "http"):
        if not config.url:
            raise ValueError(f"MCP server '{server_name}' with {transport_type} transport requires 'url' field")
        params["url"] = config.url
        # Add headers if present
        if config.headers:
            params["headers"] = config.headers
    else:
        raise ValueError(f"MCP server '{server_name}' has unsupported transport type: {transport_type}")

    return params


def build_servers_config(runtime_mcp_config: ExtensionsConfig) -> dict[str, dict[str, Any]]:
    """Build servers configuration for MultiServerMCPClient.

    Args:
        runtime_mcp_config: Extensions configuration containing all MCP servers.

    Returns:
        Dictionary mapping server names to their parameters.
    """
    enabled_servers = runtime_mcp_config.get_enabled_mcp_servers()

    if not enabled_servers:
        logger.info("No enabled MCP servers found")
        return {}

    servers_config = {}
    for server_name, server_config in enabled_servers.items():
        try:
            servers_config[server_name] = build_server_params(server_name, server_config)
            logger.info(f"Configured MCP server: {server_name}")
        except Exception as e:
            logger.error(f"Failed to configure MCP server '{server_name}': {e}")

    return servers_config
