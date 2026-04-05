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
  --profiles-dir plugins/proof_of_audit/profiles \
  --profile reentrancy-only \
  --task "Audit this contract" \
  --repo ./my-contract
```

## Challenge Evidence Generator

Generate challenge evidence by comparing two audit reports:

```bash
# Structural comparison (no LLM required)
python -m plugins.proof_of_audit.cli challenge-evidence \
  --original report_a.json \
  --challenger report_b.json \
  --output evidence.json

# LLM-enhanced deep analysis
python -m plugins.proof_of_audit.cli challenge-evidence \
  --original report_a.json \
  --challenger report_b.json \
  --output evidence.json \
  --llm-provider gemini \
  --llm-model gemini-2.0-flash
```

The command identifies:
- **Missed vulnerabilities** — findings in the challenger report absent from the original
- **Severity downgrades** — findings present in both but rated lower by the original
- **False negatives** — LLM-detected logical gaps in the original analysis

Output is a structured JSON payload compatible with `POST /audits/{id}/challenge`.

## Future Extensions

This plugin will also house:

- **Multi-instance persona config** (#119) — per-agent persona deployment topology
- **Report schema validation** — structured audit report format enforcement
