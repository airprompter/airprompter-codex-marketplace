# AirPrompter Codex Marketplace

This repository publishes the AirPrompter Codex plugin marketplace.

Install the marketplace:

```bash
codex plugin marketplace add airprompter/airprompter-codex-marketplace --ref main
```

Install the plugin:

```bash
codex plugin add airprompter@airprompter
```

AirPrompter uses the hosted MCP server configured in `plugins/airprompter/.mcp.json`.
After plugin installation, complete the Codex MCP OAuth connection in Codex.
