# Echo Tool Plugin Example

This example package shows how to ship an external Agent Forge tool via Python
entry points.

Install it in editable mode from the repo root:

```bash
pip install -e ./examples/echo-tool-plugin
```

Once installed, Agent Forge will discover the plugin automatically through the
`agent_forge.tools` entry point group.
