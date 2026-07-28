---
name: mcp-tools
description: Use configured external MCP servers while keeping approvals and evidence centralized.
---
# MCP Tools Skill

Use this skill when a prompt asks for MCP, external tools, or a named configured
MCP server.

- Discover available MCP servers and tools before claiming they exist.
- Use the exact configured tool name in logs and final evidence.
- Do not invent MCP capabilities.
- Permission prompts and OAuth flows stay owned by SHAMSU's MCP manager.
- If a required server is unavailable, report the server name and the setup action.
- Prefer MCP tool output over model memory for external-system facts.
