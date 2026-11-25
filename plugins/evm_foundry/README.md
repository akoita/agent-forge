# EVM/Foundry Extension Pack (`plugins/evm_foundry/`)

Domain-isolated extension pack targeting the stock `ghcr.io/foundry-rs/foundry:latest` sandbox image with `forge_build`, `forge_test`, fork dry-run simulation (`forge_simulate_script`), mandatory `ApprovalGate` confirmation prior to `--broadcast`, and strict signer/key isolation (`#227`).

## Usage

Load profiles directly via `--profiles-dir`:

```sh
agent-forge run \
  --profiles-dir plugins/evm_foundry/profiles \
  --profile evm-foundry-dev \
  --sandbox-image ghcr.io/foundry-rs/foundry:latest \
  --task "Compile contracts and run the Foundry test suite"
```

| Profile ID | Aliases | Focus |
| --- | --- | --- |
| `evm-foundry-dev` | `foundry-dev`, `evm-dev` | Offline `forge_build` and `forge_test` inside `ghcr.io/foundry-rs/foundry:latest` |
| `evm-foundry-deploy` | `foundry-deploy`, `evm-deploy` | Fork simulation (`forge_simulate_script`) -> `ApprovalGate` -> `forge_broadcast_script` |

## Safety Controls

1. **Simulation before broadcast (`SimulationGateTracker`):** `forge_broadcast_script` requires an unconsumed `sim_id` from a matching `forge_simulate_script` run.
2. **Mandatory `ApprovalGate`:** `ForgeBroadcastScriptTool.requires_approval` is `True` (`approval_category="evm_broadcast"`), blocking unattended `--broadcast` calls.
3. **Signer isolation (`validate_sandbox_signer_isolation`):** Rejects private key/mnemonic environment variables, embedded PEM or 32-byte hex keys, and `--private-key`/`--mnemonic`/`--unlocked` CLI flags inside the sandbox.
4. **Offline-by-default sandbox (`build_foundry_sandbox_config`):** Keeps `network_enabled=False` unless validated RPC domains are explicitly declared in `rpc_domains`.
