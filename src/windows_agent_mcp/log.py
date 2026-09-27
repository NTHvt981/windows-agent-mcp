import logging

# stdout is the MCP wire, so logs go to stderr.
logging.basicConfig(
    level=logging.INFO,
    format="[WindowsAgentMCP] %(levelname)s: %(message)s",
)

log = logging.getLogger("windows-agent-mcp")
