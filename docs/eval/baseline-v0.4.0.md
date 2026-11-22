# Model Benchmark Report (v0.4.0 Baseline)

## Measured Default Selection

- **Selected default:** `gemini` / `gemini-3.1-flash-lite-preview` (profile `gemini-flash-lite`, API-reported `gemini-3.1-flash-lite`).
- **Settings & revision:** `temperature=0.0`, `max_iterations=10`, `max_tokens_per_run=60000`, `sandbox_backend=bwrap`, `harness_revision=92a8541`.
- **Selection rationale:** Among accessible candidates, `gemini-flash-lite` achieved the only verified fix (`1/4`, `25%`) at `$0.027518` total cost across all 4 attempts (`$0.027518` per verified fix including failed runs) with `0` API rate-limit failures (`113.28s` median latency). Both `gemini-25-flash` (`0/4`, `$0.017782` total cost, `73.72s` median latency) and `gemini-3-flash` (`0/4`, `$0.036208` total cost, `70.92s` median latency) scored `0` verified fixes due to `Max iterations reached` on tasks 1–2 and `Gemini rate limit exceeded after 5 retries` on tasks 3–4.
- **Diagnostic notes (baseline before harness changes):**
  - On all three timed-out tasks for `gemini-flash-lite`, traces show the agent applied valid edits and passed `python3 -m unittest` inside the candidate sandbox on iterations 7–8, then spent iterations 9–10 on redundant verification reads until `max_iterations=10` cut off the run before returning a final `stop` response. Increasing the iteration headroom or adding a post-edit completion hint is a high-leverage follow-up once this baseline is recorded.
  - `Tool Error Rate` includes `run_shell` calls where `python3 -m unittest` exits `0` while writing test progress to `stderr`.
- **Skipped profiles (missing credentials):** `openai-gpt54` (`OPENAI_API_KEY`), `anthropic-sonnet` (`ANTHROPIC_API_KEY`), and `meta-llama` (`TOGETHER_API_KEY`). Re-run with those environment variables set to complete cross-provider comparisons:

```sh
agent-forge models benchmark \
  --sandbox-backend bwrap \
  --attempts 1 \
  --max-iterations 10 \
  --max-tokens 60000 \
  --output-json docs/eval/baseline-v0.4.0.json \
  --output-md docs/eval/baseline-v0.4.0.md
```

- **Generated at:** `2026-11-22T16:11:54.589688+00:00`
- **Harness revision:** `92a8541`
- **Sandbox backend:** `bwrap`
- **Tasks:** `fix-health-status`, `fix-pagination-offset`, `fix-multi-file-summary`, `fix-retry-backoff`

## Profile Summary

| Profile | Provider | Model | Status | Verified | Median Latency | Total Cost (incl. failures) | Cost / Verified Fix | Tool Error Rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gemini-flash-lite` | gemini | `gemini-3.1-flash-lite-preview` | completed | 1/4 (25%) | 113.28s | $0.027518 | $0.027518 | 33.3% |
| `gemini-25-flash` | gemini | `gemini-2.5-flash` | completed | 0/4 (0%) | 73.72s | $0.017782 | N/A (0 verified) | 26.9% |
| `gemini-3-flash` | gemini | `gemini-3-flash-preview` | completed | 0/4 (0%) | 70.92s | $0.036208 | N/A (0 verified) | 29.2% |
| `openai-gpt54` | openai | `gpt-5.4` | skipped (Missing credential variable OPENAI_API_KEY.) | - | - | - | - | - |
| `anthropic-sonnet` | anthropic | `claude-sonnet-4-6` | skipped (Missing credential variable ANTHROPIC_API_KEY.) | - | - | - | - | - |
| `meta-llama` | openai | `meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8` | skipped (Missing credential variable TOGETHER_API_KEY.) | - | - | - | - | - |

## Per-Task Attempts

| Profile | Task | Attempt | Status | Verified | Latency | Tokens | Est. Cost | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gemini-flash-lite` | `fix-health-status` | 1 | passed | yes | 80.82s | 24208 | $0.006468 | verified |
| `gemini-flash-lite` | `fix-pagination-offset` | 1 | failed | no | 178.88s | 26409 | $0.007004 | Max iterations reached |
| `gemini-flash-lite` | `fix-multi-file-summary` | 1 | failed | no | 145.74s | 26503 | $0.007184 | Max iterations reached |
| `gemini-flash-lite` | `fix-retry-backoff` | 1 | failed | no | 21.83s | 25577 | $0.006862 | Max iterations reached |
| `gemini-25-flash` | `fix-health-status` | 1 | failed | no | 80.23s | 25759 | $0.008190 | Max iterations reached |
| `gemini-25-flash` | `fix-pagination-offset` | 1 | failed | no | 57.85s | 28060 | $0.008891 | Max iterations reached |
| `gemini-25-flash` | `fix-multi-file-summary` | 1 | failed | no | 134.29s | 2026 | $0.000701 | Gemini rate limit exceeded after 5 retries |
| `gemini-25-flash` | `fix-retry-backoff` | 1 | failed | no | 67.20s | 0 | $0.000000 | Gemini rate limit exceeded after 5 retries |
| `gemini-3-flash` | `fix-health-status` | 1 | failed | no | 75.75s | 26464 | $0.013919 | Max iterations reached |
| `gemini-3-flash` | `fix-pagination-offset` | 1 | failed | no | 65.45s | 35037 | $0.017769 | Max iterations reached |
| `gemini-3-flash` | `fix-multi-file-summary` | 1 | failed | no | 66.08s | 2087 | $0.001030 | Gemini rate limit exceeded after 5 retries |
| `gemini-3-flash` | `fix-retry-backoff` | 1 | failed | no | 77.24s | 6961 | $0.003490 | Gemini rate limit exceeded after 5 retries |
