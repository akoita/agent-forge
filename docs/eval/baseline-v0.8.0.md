# Model Benchmark Report

- **Generated at:** `2026-11-25T00:14:26.369716+00:00`
- **Harness revision:** `6c278b0`
- **Sandbox backend:** `subprocess-eval`
- **Tasks:** `fix-health-status`, `fix-pagination-offset`, `fix-multi-file-summary`, `fix-retry-backoff`, `fix-rate-limiter-window`, `fix-csv-quoted-parser`, `fix-config-deep-merge`, `fix-semver-comparator`, `fix-lru-cache-eviction`, `fix-dag-toposort`, `fix-markdown-link-extractor`, `fix-env-template-expander`

## Profile Summary

| Profile | Provider | Model | Status | Verified | Median Latency | Total Cost (incl. failures) | Cost / Verified Fix | Tool Error Rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `deterministic-oracle` | deterministic | `scripted-oracle` | completed | 12/12 (100%) | 0.56s | $0.000000 | $0.000000 | 32.4% |
| `deterministic-noop` | deterministic | `scripted-noop` | completed | 0/12 (0%) | 0.27s | $0.000000 | N/A (0 verified) | 0.0% |
| `deterministic-incomplete` | deterministic | `scripted-incomplete` | completed | 0/12 (0%) | 0.38s | $0.000000 | N/A (0 verified) | 0.0% |
| `deterministic-oracle-code-mode` | deterministic | `scripted-oracle_code_mode` | completed | 12/12 (100%) | 0.58s | $0.000000 | $0.000000 | 24.5% |
| `deterministic-oracle-subagents` | deterministic | `scripted-oracle_subagents` | completed | 12/12 (100%) | 0.65s | $0.000000 | $0.000000 | 32.4% |

## Per-Task Attempts

| Profile | Task | Attempt | Status | Verified | Latency | Tokens | Est. Cost | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `deterministic-oracle` | `fix-health-status` | 1 | passed | yes | 0.60s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-pagination-offset` | 1 | passed | yes | 0.62s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-multi-file-summary` | 1 | passed | yes | 0.73s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-retry-backoff` | 1 | passed | yes | 0.52s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-rate-limiter-window` | 1 | passed | yes | 0.58s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-csv-quoted-parser` | 1 | passed | yes | 0.57s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-config-deep-merge` | 1 | passed | yes | 0.55s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-semver-comparator` | 1 | passed | yes | 0.49s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-lru-cache-eviction` | 1 | passed | yes | 0.52s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-dag-toposort` | 1 | passed | yes | 0.55s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-markdown-link-extractor` | 1 | passed | yes | 0.65s | 825 | $0.000000 | verified |
| `deterministic-oracle` | `fix-env-template-expander` | 1 | passed | yes | 0.56s | 655 | $0.000000 | verified |
| `deterministic-noop` | `fix-health-status` | 1 | failed | no | 0.27s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-pagination-offset` | 1 | failed | no | 0.27s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-multi-file-summary` | 1 | failed | no | 0.24s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-retry-backoff` | 1 | failed | no | 0.27s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-rate-limiter-window` | 1 | failed | no | 0.28s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-csv-quoted-parser` | 1 | failed | no | 0.28s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-config-deep-merge` | 1 | failed | no | 0.27s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-semver-comparator` | 1 | failed | no | 0.29s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-lru-cache-eviction` | 1 | failed | no | 0.27s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-dag-toposort` | 1 | failed | no | 0.28s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-markdown-link-extractor` | 1 | failed | no | 0.27s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-env-template-expander` | 1 | failed | no | 0.29s | 315 | $0.000000 | empty_patch |
| `deterministic-incomplete` | `fix-health-status` | 1 | failed | no | 0.39s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-pagination-offset` | 1 | failed | no | 0.36s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-multi-file-summary` | 1 | failed | no | 0.43s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-retry-backoff` | 1 | failed | no | 0.37s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-rate-limiter-window` | 1 | failed | no | 0.38s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-csv-quoted-parser` | 1 | failed | no | 0.38s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-config-deep-merge` | 1 | failed | no | 0.38s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-semver-comparator` | 1 | failed | no | 0.36s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-lru-cache-eviction` | 1 | failed | no | 0.34s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-dag-toposort` | 1 | failed | no | 0.38s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-markdown-link-extractor` | 1 | failed | no | 0.41s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-env-template-expander` | 1 | failed | no | 0.42s | 485 | $0.000000 | check_failed |
| `deterministic-oracle-code-mode` | `fix-health-status` | 1 | passed | yes | 0.54s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-pagination-offset` | 1 | passed | yes | 0.57s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-multi-file-summary` | 1 | passed | yes | 0.59s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-retry-backoff` | 1 | passed | yes | 0.57s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-rate-limiter-window` | 1 | passed | yes | 0.53s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-csv-quoted-parser` | 1 | passed | yes | 0.51s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-config-deep-merge` | 1 | passed | yes | 0.64s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-semver-comparator` | 1 | passed | yes | 0.58s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-lru-cache-eviction` | 1 | passed | yes | 0.60s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-dag-toposort` | 1 | passed | yes | 0.56s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-markdown-link-extractor` | 1 | passed | yes | 0.60s | 260 | $0.000000 | verified |
| `deterministic-oracle-code-mode` | `fix-env-template-expander` | 1 | passed | yes | 0.59s | 260 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-health-status` | 1 | passed | yes | 0.73s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-pagination-offset` | 1 | passed | yes | 0.69s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-multi-file-summary` | 1 | passed | yes | 0.72s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-retry-backoff` | 1 | passed | yes | 0.69s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-rate-limiter-window` | 1 | passed | yes | 0.65s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-csv-quoted-parser` | 1 | passed | yes | 0.68s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-config-deep-merge` | 1 | passed | yes | 0.64s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-semver-comparator` | 1 | passed | yes | 0.61s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-lru-cache-eviction` | 1 | passed | yes | 0.52s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-dag-toposort` | 1 | passed | yes | 0.50s | 855 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-markdown-link-extractor` | 1 | passed | yes | 0.48s | 1025 | $0.000000 | verified |
| `deterministic-oracle-subagents` | `fix-env-template-expander` | 1 | passed | yes | 0.44s | 855 | $0.000000 | verified |
