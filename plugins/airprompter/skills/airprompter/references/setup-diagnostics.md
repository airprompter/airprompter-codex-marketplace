# Setup Diagnostics

Use this reference when AirPrompter tools are missing, or when the user asks to
diagnose, repair, reconnect, or reinstall AirPrompter in Codex.

Before an authenticated diagnostic, read `connection-selection.md` in this
same directory. Select and retain the intended connector/environment and linked
account. Use its read-only `diagnose_connection` tool with the arguments actually
accepted by that host's schema. Inspect its returned status and binding; tool
visibility and plugin presence do not prove authorization. Do not diagnose a
different account merely because the intended account fails.

Keep package-content questions separate from MCP/OAuth health. Compare an
installed plugin with the corresponding released plugin artifact when available;
a content difference from unreleased repository source alone does not identify
a packaging defect. Existing installations update through the host's supported
plugin-update mechanism. Do not modify installed caches or add an independent
skill updater. A reinstall and an account reconnect are separate operations.

## Customer Install Rule

For the official AirPrompter plugin, installation comes from the universal
plugin directory shared by ChatGPT and Codex. The host registers the bundled
MCP connection and prompts the customer for AirPrompter OAuth during install.
Do not ask a customer to run `codex mcp add`, edit `config.toml`, add a Git
marketplace, or install a separate MCP entry.

If an official-directory install is present but tools are unavailable, use the
host's Plugins or Connectors UI to reconnect AirPrompter OAuth, then start a new
task. If the host still does not expose the bundled tools, report a plugin-host
integration failure for support; do not turn internal CLI repair into the
customer onboarding path.

## Local Development Readiness Rule

For private dev and local-marketplace installs, do not infer MCP readiness from
the AirPrompter skill being present or from
`codex plugin list` showing the plugin as installed. The plugin, skill, MCP
registration, and OAuth session are separate states. A healthy Codex setup must
have both the plugin and the matching MCP server entry.

Choose the identity from the installed plugin:

- `airprompter-dev@airprompter-dev` uses server `airprompter_dev`, URL
  `https://mcp.dev.airprompter.com/mcp`, and OAuth resource
  `https://mcp.dev.airprompter.com`.
- `airprompter@airprompter` uses server `airprompter`, URL
  `https://mcp.airprompter.com/mcp`, and OAuth resource
  `https://mcp.airprompter.com`.

## Codex Local Diagnostic And Repair

1. Run `codex plugin list` and confirm the expected plugin is installed and
   enabled.
2. Run `codex mcp get <server-name>`. If it succeeds with the expected URL, the
   MCP server is registered. Do not claim OAuth is healthy from plugin status
   alone.
3. If the MCP entry is missing and the user explicitly asked to repair setup,
   run:

   ```text
   codex mcp add <server-name> --url <mcp-url> --oauth-resource <oauth-resource>
   ```

   This may start OAuth automatically. If authorization does not finish, run:

   ```text
   codex mcp login <server-name> --scopes prompts.read,prompts.write
   ```

4. Run `codex mcp get <server-name>` again and confirm the expected URL.
   Once the selected connection's tools are available, use read-only
   `diagnose_connection` to verify authorization and inspect its binding.
   Registration alone does not prove connection health.
5. Start a new Codex task before testing an AirPrompter workflow because a
   running task's tool inventory is fixed when that task starts.

Only if a power user asks why skills with helper scripts run remotely in Codex:
the default workspace-write sandbox cannot write `~/.airprompter` and has no
network; they can add `~/.airprompter` to `[sandbox_workspace_write]
writable_roots` and set `network_access = true` there in their Codex config.

Prefer the Codex CLI over editing `config.toml` directly. Never print OAuth
codes or tokens. If the user only asked to run a workflow and did not authorize
setup repair, stop after reporting the missing MCP registration and the exact
repair command; do not generate a substitute workflow result.
