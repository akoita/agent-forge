# Model Benchmark Report

- **Generated at:** `2026-11-23T11:43:00.995574+00:00`
- **Harness revision:** `a914dfe`
- **Sandbox backend:** `subprocess-eval`
- **Tasks:** `fix-health-status`, `fix-pagination-offset`, `fix-multi-file-summary`, `fix-retry-backoff`, `fix-rate-limiter-window`, `fix-csv-quoted-parser`, `fix-config-deep-merge`, `fix-semver-comparator`, `fix-lru-cache-eviction`, `fix-dag-toposort`, `fix-markdown-link-extractor`, `fix-env-template-expander`

## Profile Summary

| Profile | Provider | Model | Status | Verified | Median Latency | Total Cost (incl. failures) | Cost / Verified Fix | Tool Error Rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `deterministic-oracle` | deterministic | `scripted-oracle` | completed | 12/12 (100%) | 0.44s | $0.000000 | $0.000000 | 32.4% |
| `deterministic-noop` | deterministic | `scripted-noop` | completed | 0/12 (0%) | 0.25s | $0.000000 | N/A (0 verified) | 0.0% |
| `deterministic-incomplete` | deterministic | `scripted-incomplete` | completed | 0/12 (0%) | 0.42s | $0.000000 | N/A (0 verified) | 0.0% |

## Per-Task Attempts

| Profile | Task | Attempt | Status | Verified | Latency | Tokens | Est. Cost | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `deterministic-oracle` | `fix-health-status` | 1 | passed | yes | 0.44s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-pagination-offset` | 1 | passed | yes | 0.45s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-multi-file-summary` | 1 | passed | yes | 0.44s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-retry-backoff` | 1 | passed | yes | 0.46s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-rate-limiter-window` | 1 | passed | yes | 0.43s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-csv-quoted-parser` | 1 | passed | yes | 0.43s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-config-deep-merge` | 1 | passed | yes | 0.45s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-semver-comparator` | 1 | passed | yes | 0.44s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-lru-cache-eviction` | 1 | passed | yes | 0.42s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-dag-toposort` | 1 | passed | yes | 0.45s | 655 | $0.000000 | verified |
| `deterministic-oracle` | `fix-markdown-link-extractor` | 1 | passed | yes | 0.46s | 825 | $0.000000 | verified |
| `deterministic-oracle` | `fix-env-template-expander` | 1 | passed | yes | 0.45s | 655 | $0.000000 | verified |
| `deterministic-noop` | `fix-health-status` | 1 | failed | no | 0.26s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-pagination-offset` | 1 | failed | no | 0.25s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-multi-file-summary` | 1 | failed | no | 0.23s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-retry-backoff` | 1 | failed | no | 0.24s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-rate-limiter-window` | 1 | failed | no | 0.25s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-csv-quoted-parser` | 1 | failed | no | 0.26s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-config-deep-merge` | 1 | failed | no | 0.23s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-semver-comparator` | 1 | failed | no | 0.22s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-lru-cache-eviction` | 1 | failed | no | 0.25s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-dag-toposort` | 1 | failed | no | 0.24s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-markdown-link-extractor` | 1 | failed | no | 0.25s | 315 | $0.000000 | empty_patch |
| `deterministic-noop` | `fix-env-template-expander` | 1 | failed | no | 0.28s | 315 | $0.000000 | empty_patch |
| `deterministic-incomplete` | `fix-health-status` | 1 | failed | no | 0.40s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-pagination-offset` | 1 | failed | no | 0.42s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-multi-file-summary` | 1 | failed | no | 0.44s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-retry-backoff` | 1 | failed | no | 0.39s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-rate-limiter-window` | 1 | failed | no | 0.56s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-csv-quoted-parser` | 1 | failed | no | 0.42s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-config-deep-merge` | 1 | failed | no | 0.41s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-semver-comparator` | 1 | failed | no | 0.40s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-lru-cache-eviction` | 1 | failed | no | 0.49s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-dag-toposort` | 1 | failed | no | 0.49s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-markdown-link-extractor` | 1 | failed | no | 0.39s | 485 | $0.000000 | check_failed |
| `deterministic-incomplete` | `fix-env-template-expander` | 1 | failed | no | 0.40s | 485 | $0.000000 | check_failed |
