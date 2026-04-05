# Proof-of-Audit Plugin

Domain-specific extension for **smart-contract auditing** with Agent Forge.

## Profiles

The `profiles/` directory contains audit-specific agent profiles:

| Profile | Description |
|---|---|
| `reentrancy-only` | Focuses exclusively on reentrancy vulnerabilities |
| `access-control-only` | Focuses on access control and authorization issues |
| `full-spectrum` | All detectors enabled, comprehensive analysis |
| `llm-deep-gemini` | Full spectrum + Gemini provider override |
| `llm-deep-openai` | Full spectrum + OpenAI provider override |

## Usage

Load audit profiles with `--profiles-dir`:

```bash
agent-forge run \
  --profiles-dir plugins/proof-of-audit/profiles \
  --profile reentrancy-only \
  --task "Audit this contract" \
  --repo ./my-contract
```

## Future Extensions

This plugin will also house:

- **Challenge evidence tool** (#118) — generates challenge payloads for PoA disputes
- **Multi-instance persona config** (#119) — per-agent persona deployment topology
- **Report schema validation** — structured audit report format enforcement
