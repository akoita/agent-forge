#!/usr/bin/env python3
"""Fetch latest model pricing from LiteLLM and write a vendored snapshot.

Usage:
    python scripts/update_prices.py

The output file is ``agent_forge/observability/model_prices.json``.
It contains a simplified view of LiteLLM's community-maintained
``model_prices_and_context_window.json`` with only the fields we need.

Source: https://github.com/BerriAI/litellm
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

_LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)

_OUTPUT = (
    Path(__file__).resolve().parent.parent / "agent_forge" / "observability" / "model_prices.json"
)


def main() -> None:
    print(f"Fetching pricing from {_LITELLM_URL} ...")
    raw = urllib.request.urlopen(_LITELLM_URL).read()  # noqa: S310
    data: dict = json.loads(raw)

    prices: dict[str, dict[str, object]] = {}
    for model_key, info in sorted(data.items()):
        if not isinstance(info, dict):
            continue
        input_cost = info.get("input_cost_per_token")
        output_cost = info.get("output_cost_per_token")
        if input_cost is None or output_cost is None:
            continue
        entry: dict[str, object] = {
            "input_cost_per_token": input_cost,
            "output_cost_per_token": output_cost,
        }
        cache_cost = info.get("cache_read_input_token_cost")
        if (
            isinstance(cache_cost, (int, float))
            and not isinstance(cache_cost, bool)
            and cache_cost >= 0
        ):
            entry["cache_read_input_token_cost"] = cache_cost
        max_input = info.get("max_input_tokens")
        if isinstance(max_input, int) and not isinstance(max_input, bool) and max_input > 0:
            entry["max_input_tokens"] = max_input
        prices[model_key] = entry

    _OUTPUT.write_text(json.dumps(prices, indent=2) + "\n")
    print(f"Wrote {len(prices)} models to {_OUTPUT}")


if __name__ == "__main__":
    main()
