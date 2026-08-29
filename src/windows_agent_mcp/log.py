import logging

# ============================================================
# Logging
# ============================================================

# NEVER use print().
#
# In an MCP stdio server, stdout is the protocol transport.
# The official SDK documents that stdout is the MCP wire.
#
# Logs therefore go to stderr.
logging.basicConfig(
    level=logging.INFO,
    format="[WindowsAgentMCP] %(levelname)s: %(message)s",
)

log = logging.getLogger("windows-agent-mcp")
