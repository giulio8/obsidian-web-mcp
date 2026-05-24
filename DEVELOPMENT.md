# AI Agent Development Guidelines for obsidian-web-mcp

This file contains critical workflow rules for AI coding agents pair-programming with the user in this workspace.

## CRITICAL: VM Restart & MCP Handshake Protocol

When you copy files or make modifications to the codebase and restart the systemd services on the VM (`obsidian-mcp@giulio` and `obsidian-mcp@elsa`):

1. **Stop immediately!**
2. **Do NOT run any subsequent MCP tool calls.**
3. **Ask the USER to trigger/redo the MCP handshake** (e.g. refresh the MCP server connections in their client interface) before proceeding.
4. **Failure to do this will cause subsequent tool calls to fail with `Bad Gateway` or disconnect errors.**
