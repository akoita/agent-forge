"""Pinned domain-agnostic coding benchmark corpus with visible and hidden tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

VERIFY_CMD = "python3 -m unittest discover -s tests -v"


@dataclass(frozen=True)
class BenchmarkTask:
    """Self-contained coding benchmark task with independent verifier tests."""

    id: str
    title: str
    prompt: str
    verify_command: str
    files: dict[str, str]
    tests: dict[str, str]
    known_good_files: dict[str, str]
    hidden_tests: dict[str, str] = field(default_factory=dict)
    incomplete_files: dict[str, str] = field(default_factory=dict)
    suite: Literal["smoke", "full"] = "full"


BUILTIN_TASKS: tuple[BenchmarkTask, ...] = (
    # ------------------------------------------------------------------
    # Smoke suite (4 tasks — run in PR CI and quick local checks)
    # ------------------------------------------------------------------
    BenchmarkTask(
        id="fix-health-status",
        title="Return healthy HTTP status from /health handler",
        prompt=(
            "The health endpoint handler in app.py returns an error payload with status 500. "
            "Inspect app.py and tests/test_app.py, fix health() so it returns "
            '({"status": "ok"}, 200), and verify tests pass.'
        ),
        verify_command=VERIFY_CMD,
        suite="smoke",
        files={
            "app.py": (
                '"""Application handlers."""\n\n'
                "def index() -> tuple[str, int]:\n"
                '    return "Hello, World!", 200\n\n\n'
                "def health() -> tuple[dict[str, str], int]:\n"
                '    return {"status": "error"}, 500\n'
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_app.py": (
                "import unittest\n"
                "from app import health, index\n\n\n"
                "class TestApp(unittest.TestCase):\n"
                "    def test_index(self) -> None:\n"
                "        body, code = index()\n"
                "        self.assertEqual(code, 200)\n"
                '        self.assertIn("Hello", body)\n\n'
                "    def test_health_ok(self) -> None:\n"
                "        body, code = health()\n"
                "        self.assertEqual(code, 200)\n"
                '        self.assertEqual(body, {"status": "ok"})\n'
            ),
        },
        hidden_tests={
            "tests/test_app_hidden.py": (
                "import unittest\n"
                "from app import health, index\n\n\n"
                "class TestAppHidden(unittest.TestCase):\n"
                "    def test_health_returns_fresh_payload(self) -> None:\n"
                "        first, code1 = health()\n"
                "        second, code2 = health()\n"
                "        self.assertEqual((code1, code2), (200, 200))\n"
                '        first["mutated"] = "yes"\n'
                '        self.assertEqual(second, {"status": "ok"})\n'
                "        self.assertEqual(index(), ('Hello, World!', 200))\n"
            ),
        },
        incomplete_files={
            "app.py": (
                '"""Application handlers."""\n\n'
                "def index() -> tuple[str, int]:\n"
                '    return "Hello, World!", 200\n\n\n'
                "def health() -> tuple[dict[str, str], int]:\n"
                '    return {"status": "ok"}, 503\n'
            ),
        },
        known_good_files={
            "app.py": (
                '"""Application handlers."""\n\n'
                "def index() -> tuple[str, int]:\n"
                '    return "Hello, World!", 200\n\n\n'
                "def health() -> tuple[dict[str, str], int]:\n"
                '    return {"status": "ok"}, 200\n'
            ),
        },
    ),
    BenchmarkTask(
        id="fix-pagination-offset",
        title="Fix 1-based page slicing and ceiling total_pages",
        prompt=(
            "Fix paginate() in pagination.py so 1-based page slicing and total_pages ceiling "
            "calculation satisfy tests/test_pagination.py, and verify tests pass."
        ),
        verify_command=VERIFY_CMD,
        suite="smoke",
        files={
            "pagination.py": (
                '"""List pagination helper."""\n\n'
                "def paginate(\n"
                "    items: list[int], page: int = 1, page_size: int = 10\n"
                ") -> dict[str, object]:\n"
                "    if page < 1:\n"
                '        raise ValueError("page must be >= 1")\n'
                "    if page_size < 1:\n"
                '        raise ValueError("page_size must be >= 1")\n'
                "    start = page * page_size\n"
                "    end = start + page_size\n"
                "    total_pages = len(items) // page_size\n"
                "    return {\n"
                '        "items": items[start:end],\n'
                '        "page": page,\n'
                '        "page_size": page_size,\n'
                '        "total_items": len(items),\n'
                '        "total_pages": total_pages,\n'
                "    }\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_pagination.py": (
                "import unittest\n"
                "from pagination import paginate\n\n\n"
                "class TestPagination(unittest.TestCase):\n"
                "    def test_first_page(self) -> None:\n"
                "        res = paginate([1, 2, 3, 4, 5], page=1, page_size=2)\n"
                '        self.assertEqual(res["items"], [1, 2])\n'
                '        self.assertEqual(res["total_pages"], 3)\n\n'
                "    def test_last_partial_page(self) -> None:\n"
                "        res = paginate([1, 2, 3, 4, 5], page=3, page_size=2)\n"
                '        self.assertEqual(res["items"], [5])\n'
                '        self.assertEqual(res["total_pages"], 3)\n\n'
                "    def test_empty_items(self) -> None:\n"
                "        res = paginate([], page=1, page_size=5)\n"
                '        self.assertEqual(res["items"], [])\n'
                '        self.assertEqual(res["total_pages"], 0)\n'
            ),
        },
        hidden_tests={
            "tests/test_pagination_hidden.py": (
                "import unittest\n"
                "from pagination import paginate\n\n\n"
                "class TestPaginationHidden(unittest.TestCase):\n"
                "    def test_exact_multiple_and_past_end(self) -> None:\n"
                "        res = paginate([10, 20, 30, 40], page=2, page_size=2)\n"
                '        self.assertEqual(res["items"], [30, 40])\n'
                '        self.assertEqual(res["total_pages"], 2)\n'
                "        past = paginate([10, 20, 30, 40], page=5, page_size=2)\n"
                '        self.assertEqual(past["items"], [])\n\n'
                "    def test_invalid_bounds_raise(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            paginate([1, 2], page=0, page_size=1)\n"
                "        with self.assertRaises(ValueError):\n"
                "            paginate([1, 2], page=1, page_size=0)\n"
            ),
        },
        incomplete_files={
            "pagination.py": (
                '"""List pagination helper."""\n\n'
                "def paginate(\n"
                "    items: list[int], page: int = 1, page_size: int = 10\n"
                ") -> dict[str, object]:\n"
                "    if page < 1:\n"
                '        raise ValueError("page must be >= 1")\n'
                "    if page_size < 1:\n"
                '        raise ValueError("page_size must be >= 1")\n'
                "    start = (page - 1) * page_size\n"
                "    end = start + page_size\n"
                "    total_pages = len(items) // page_size\n"
                "    return {\n"
                '        "items": items[start:end],\n'
                '        "page": page,\n'
                '        "page_size": page_size,\n'
                '        "total_items": len(items),\n'
                '        "total_pages": total_pages,\n'
                "    }\n"
            ),
        },
        known_good_files={
            "pagination.py": (
                '"""List pagination helper."""\n\n'
                "def paginate(\n"
                "    items: list[int], page: int = 1, page_size: int = 10\n"
                ") -> dict[str, object]:\n"
                "    if page < 1:\n"
                '        raise ValueError("page must be >= 1")\n'
                "    if page_size < 1:\n"
                '        raise ValueError("page_size must be >= 1")\n'
                "    start = (page - 1) * page_size\n"
                "    end = start + page_size\n"
                "    total_pages = (len(items) + page_size - 1) // page_size if items else 0\n"
                "    return {\n"
                '        "items": items[start:end],\n'
                '        "page": page,\n'
                '        "page_size": page_size,\n'
                '        "total_items": len(items),\n'
                '        "total_pages": total_pages,\n'
                "    }\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-multi-file-summary",
        title="Fix line-item field mismatch and currency formatting across modules",
        prompt=(
            "Inspect models.py, formatter.py, and tests/test_formatter.py. "
            "Fix summarize_order() in formatter.py so line totals multiply quantity by "
            "unit_price_cents and format totals to two decimal places, and verify tests pass."
        ),
        verify_command=VERIFY_CMD,
        suite="smoke",
        files={
            "models.py": (
                '"""Domain data models for orders."""\n\n'
                "from dataclasses import dataclass\n\n\n"
                "@dataclass(frozen=True)\n"
                "class OrderLine:\n"
                "    sku: str\n"
                "    quantity: int\n"
                "    unit_price_cents: int\n"
            ),
            "formatter.py": (
                '"""Order summary formatter."""\n\n'
                "from models import OrderLine\n\n\n"
                "def summarize_order(lines: list[OrderLine]) -> dict[str, object]:\n"
                "    total_units = sum(line.quantity for line in lines)\n"
                "    total_cents = sum(line.price_cents for line in lines)\n"
                '    formatted = f"${round(total_cents / 100, 1)}"\n'
                "    return {\n"
                '        "total_units": total_units,\n'
                '        "total_cents": total_cents,\n'
                '        "display": formatted,\n'
                "    }\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_formatter.py": (
                "import unittest\n"
                "from formatter import summarize_order\n"
                "from models import OrderLine\n\n\n"
                "class TestFormatter(unittest.TestCase):\n"
                "    def test_summarize_order(self) -> None:\n"
                "        lines = [\n"
                '            OrderLine(sku="A1", quantity=2, unit_price_cents=1250),\n'
                '            OrderLine(sku="B2", quantity=3, unit_price_cents=199),\n'
                "        ]\n"
                "        summary = summarize_order(lines)\n"
                '        self.assertEqual(summary["total_units"], 5)\n'
                '        self.assertEqual(summary["total_cents"], 3097)\n'
                '        self.assertEqual(summary["display"], "$30.97")\n\n'
                "    def test_empty_order(self) -> None:\n"
                "        summary = summarize_order([])\n"
                '        self.assertEqual(summary["total_units"], 0)\n'
                '        self.assertEqual(summary["total_cents"], 0)\n'
                '        self.assertEqual(summary["display"], "$0.00")\n'
            ),
        },
        hidden_tests={
            "tests/test_formatter_hidden.py": (
                "import unittest\n"
                "from formatter import summarize_order\n"
                "from models import OrderLine\n\n\n"
                "class TestFormatterHidden(unittest.TestCase):\n"
                "    def test_trailing_zero_and_single_cent(self) -> None:\n"
                "        trailing = summarize_order(\n"
                '            [OrderLine(sku="C3", quantity=1, unit_price_cents=1250)]\n'
                "        )\n"
                '        self.assertEqual(trailing["display"], "$12.50")\n'
                "        cent = summarize_order(\n"
                '            [OrderLine(sku="D4", quantity=1, unit_price_cents=1)]\n'
                "        )\n"
                '        self.assertEqual(cent["display"], "$0.01")\n'
            ),
        },
        incomplete_files={
            "formatter.py": (
                '"""Order summary formatter."""\n\n'
                "from models import OrderLine\n\n\n"
                "def summarize_order(lines: list[OrderLine]) -> dict[str, object]:\n"
                "    total_units = sum(line.quantity for line in lines)\n"
                "    total_cents = sum(line.quantity * line.unit_price_cents for line in lines)\n"
                '    formatted = f"${round(total_cents / 100, 1)}"\n'
                "    return {\n"
                '        "total_units": total_units,\n'
                '        "total_cents": total_cents,\n'
                '        "display": formatted,\n'
                "    }\n"
            ),
        },
        known_good_files={
            "formatter.py": (
                '"""Order summary formatter."""\n\n'
                "from models import OrderLine\n\n\n"
                "def summarize_order(lines: list[OrderLine]) -> dict[str, object]:\n"
                "    total_units = sum(line.quantity for line in lines)\n"
                "    total_cents = sum(line.quantity * line.unit_price_cents for line in lines)\n"
                '    formatted = f"${total_cents / 100:.2f}"\n'
                "    return {\n"
                '        "total_units": total_units,\n'
                '        "total_cents": total_cents,\n'
                '        "display": formatted,\n'
                "    }\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-retry-backoff",
        title="Fix exponential backoff attempt indexing and max_delay cap",
        prompt=(
            "Inspect retry.py and tests/test_retry.py. Fix compute_backoff() so 1-indexed "
            "attempts return min(base * (2 ** (attempt - 1)), max_delay) and raise ValueError "
            "for attempt < 1."
        ),
        verify_command=VERIFY_CMD,
        suite="smoke",
        files={
            "retry.py": (
                '"""Exponential backoff calculation."""\n\n'
                "def compute_backoff(\n"
                "    attempt: int, *, base: float = 1.0, max_delay: float = 30.0\n"
                ") -> float:\n"
                "    return base * (2**attempt)\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_retry.py": (
                "import unittest\n"
                "from retry import compute_backoff\n\n\n"
                "class TestRetry(unittest.TestCase):\n"
                "    def test_progression_and_cap(self) -> None:\n"
                "        self.assertEqual(compute_backoff(1, base=2.0, max_delay=10.0), 2.0)\n"
                "        self.assertEqual(compute_backoff(2, base=2.0, max_delay=10.0), 4.0)\n"
                "        self.assertEqual(compute_backoff(3, base=2.0, max_delay=10.0), 8.0)\n"
                "        self.assertEqual(compute_backoff(4, base=2.0, max_delay=10.0), 10.0)\n\n"
                "    def test_invalid_attempt_raises(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            compute_backoff(0)\n"
            ),
        },
        hidden_tests={
            "tests/test_retry_hidden.py": (
                "import unittest\n"
                "from retry import compute_backoff\n\n\n"
                "class TestRetryHidden(unittest.TestCase):\n"
                "    def test_negative_attempt_and_base_above_cap(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            compute_backoff(-2)\n"
                "        self.assertEqual(compute_backoff(1, base=15.0, max_delay=5.0), 5.0)\n"
                "        self.assertEqual(compute_backoff(20, base=1.0, max_delay=12.0), 12.0)\n"
            ),
        },
        incomplete_files={
            "retry.py": (
                '"""Exponential backoff calculation."""\n\n'
                "def compute_backoff(\n"
                "    attempt: int, *, base: float = 1.0, max_delay: float = 30.0\n"
                ") -> float:\n"
                "    if attempt == 0:\n"
                '        raise ValueError("attempt must be >= 1")\n'
                "    return base * (2 ** (attempt - 1))\n"
            ),
        },
        known_good_files={
            "retry.py": (
                '"""Exponential backoff calculation."""\n\n'
                "def compute_backoff(\n"
                "    attempt: int, *, base: float = 1.0, max_delay: float = 30.0\n"
                ") -> float:\n"
                "    if attempt < 1:\n"
                '        raise ValueError("attempt must be >= 1")\n'
                "    return min(base * (2 ** (attempt - 1)), max_delay)\n"
            ),
        },
    ),
    # ------------------------------------------------------------------
    # Extended full corpus (8 additional domain-agnostic tasks)
    # ------------------------------------------------------------------
    BenchmarkTask(
        id="fix-rate-limiter-window",
        title="Prune expired sliding-window events and skip rejected entries",
        prompt=(
            "Inspect limiter.py and tests/test_limiter.py. Fix SlidingWindowLimiter.allow() so "
            "events at or before (now - window_seconds) expire before counting, and rejected "
            "requests are never recorded."
        ),
        verify_command=VERIFY_CMD,
        suite="full",
        files={
            "limiter.py": (
                '"""Per-key sliding-window rate limiter."""\n\n'
                "class SlidingWindowLimiter:\n"
                "    def __init__(self, limit: int, window_seconds: float) -> None:\n"
                "        self.limit = limit\n"
                "        self.window_seconds = window_seconds\n"
                "        self._events: dict[str, list[float]] = {}\n\n"
                "    def allow(self, key: str, now: float) -> bool:\n"
                "        entries = self._events.setdefault(key, [])\n"
                "        entries.append(now)\n"
                "        return len(entries) <= self.limit\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_limiter.py": (
                "import unittest\n"
                "from limiter import SlidingWindowLimiter\n\n\n"
                "class TestLimiter(unittest.TestCase):\n"
                "    def test_enforces_limit_and_recovers_after_window(self) -> None:\n"
                "        lim = SlidingWindowLimiter(limit=2, window_seconds=10.0)\n"
                '        self.assertTrue(lim.allow("k1", 0.0))\n'
                '        self.assertTrue(lim.allow("k1", 2.0))\n'
                '        self.assertFalse(lim.allow("k1", 3.0))\n'
                '        self.assertTrue(lim.allow("k1", 15.0))\n'
            ),
        },
        hidden_tests={
            "tests/test_limiter_hidden.py": (
                "import unittest\n"
                "from limiter import SlidingWindowLimiter\n\n\n"
                "class TestLimiterHidden(unittest.TestCase):\n"
                "    def test_exact_boundary_expiry_and_rejected_not_recorded(self) -> None:\n"
                "        lim = SlidingWindowLimiter(limit=2, window_seconds=10.0)\n"
                '        self.assertTrue(lim.allow("user", 0.0))\n'
                '        self.assertTrue(lim.allow("user", 5.0))\n'
                '        self.assertFalse(lim.allow("user", 6.0))\n'
                '        self.assertFalse(lim.allow("user", 7.0))\n'
                '        self.assertTrue(lim.allow("user", 10.0))\n'
                '        self.assertFalse(lim.allow("user", 11.0))\n'
            ),
        },
        incomplete_files={
            "limiter.py": (
                '"""Per-key sliding-window rate limiter."""\n\n'
                "class SlidingWindowLimiter:\n"
                "    def __init__(self, limit: int, window_seconds: float) -> None:\n"
                "        self.limit = limit\n"
                "        self.window_seconds = window_seconds\n"
                "        self._events: dict[str, list[float]] = {}\n\n"
                "    def allow(self, key: str, now: float) -> bool:\n"
                "        cutoff = now - self.window_seconds\n"
                "        entries = [ts for ts in self._events.get(key, []) if ts >= cutoff]\n"
                "        entries.append(now)\n"
                "        self._events[key] = entries\n"
                "        return len(entries) <= self.limit\n"
            ),
        },
        known_good_files={
            "limiter.py": (
                '"""Per-key sliding-window rate limiter."""\n\n'
                "class SlidingWindowLimiter:\n"
                "    def __init__(self, limit: int, window_seconds: float) -> None:\n"
                "        self.limit = limit\n"
                "        self.window_seconds = window_seconds\n"
                "        self._events: dict[str, list[float]] = {}\n\n"
                "    def allow(self, key: str, now: float) -> bool:\n"
                "        cutoff = now - self.window_seconds\n"
                "        entries = [ts for ts in self._events.get(key, []) if ts > cutoff]\n"
                "        if len(entries) >= self.limit:\n"
                "            self._events[key] = entries\n"
                "            return False\n"
                "        entries.append(now)\n"
                "        self._events[key] = entries\n"
                "        return True\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-csv-quoted-parser",
        title="Parse CSV lines with quoted commas and escaped quotes",
        prompt=(
            "Inspect csv_parser.py and tests/test_csv_parser.py. Fix parse_csv_line() to handle "
            'double-quoted fields containing commas and escaped "" quotes without stripping text.'
        ),
        verify_command=VERIFY_CMD,
        suite="full",
        files={
            "csv_parser.py": (
                '"""Single-line CSV parser helper."""\n\n'
                "def parse_csv_line(line: str) -> list[str]:\n"
                '    return [part.strip() for part in line.split(",")]\n'
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_csv_parser.py": (
                "import unittest\n"
                "from csv_parser import parse_csv_line\n\n\n"
                "class TestCsvParser(unittest.TestCase):\n"
                "    def test_quoted_comma(self) -> None:\n"
                '        self.assertEqual(parse_csv_line(\'a,"b,c",d\'), ["a", "b,c", "d"])\n\n'
                "    def test_plain_fields(self) -> None:\n"
                "        self.assertEqual(\n"
                '            parse_csv_line("one,two,three"), ["one", "two", "three"]\n'
                "        )\n"
            ),
        },
        hidden_tests={
            "tests/test_csv_parser_hidden.py": (
                "import unittest\n"
                "from csv_parser import parse_csv_line\n\n\n"
                "class TestCsvParserHidden(unittest.TestCase):\n"
                "    def test_escaped_quotes_and_spaces_and_empty(self) -> None:\n"
                "        self.assertEqual(\n"
                '            parse_csv_line(\'x,"say ""hi""",z\'),\n'
                '            ["x", \'say "hi"\', "z"],\n'
                "        )\n"
                "        self.assertEqual(\n"
                '            parse_csv_line(\'"",b," padded "\'), ["", "b", " padded "]\n'
                "        )\n"
            ),
        },
        incomplete_files={
            "csv_parser.py": (
                '"""Single-line CSV parser helper."""\n\n'
                "def parse_csv_line(line: str) -> list[str]:\n"
                "    fields: list[str] = []\n"
                "    buf: list[str] = []\n"
                "    in_quotes = False\n"
                "    for ch in line:\n"
                "        if ch == '\"':\n"
                "            in_quotes = not in_quotes\n"
                '        elif ch == "," and not in_quotes:\n'
                '            fields.append("".join(buf).strip())\n'
                "            buf.clear()\n"
                "        else:\n"
                "            buf.append(ch)\n"
                '    fields.append("".join(buf).strip())\n'
                "    return fields\n"
            ),
        },
        known_good_files={
            "csv_parser.py": (
                '"""Single-line CSV parser helper."""\n\n'
                "def parse_csv_line(line: str) -> list[str]:\n"
                "    fields: list[str] = []\n"
                "    buf: list[str] = []\n"
                "    in_quotes = False\n"
                "    i = 0\n"
                "    while i < len(line):\n"
                "        ch = line[i]\n"
                "        if ch == '\"':\n"
                "            if in_quotes and i + 1 < len(line) and line[i + 1] == '\"':\n"
                "                buf.append('\"')\n"
                "                i += 1\n"
                "            else:\n"
                "                in_quotes = not in_quotes\n"
                '        elif ch == "," and not in_quotes:\n'
                '            fields.append("".join(buf))\n'
                "            buf.clear()\n"
                "        else:\n"
                "            buf.append(ch)\n"
                "        i += 1\n"
                '    fields.append("".join(buf))\n'
                "    return fields\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-config-deep-merge",
        title="Recursively merge nested dictionaries without mutating inputs",
        prompt=(
            "Inspect config_merge.py and tests/test_config_merge.py. Fix "
            "deep_merge(base, override) to merge nested dictionaries recursively, "
            "preserve immutability of inputs, and replace non-dict or list values cleanly."
        ),
        verify_command=VERIFY_CMD,
        suite="full",
        files={
            "config_merge.py": (
                '"""Configuration dictionary deep-merge utility."""\n\n'
                "from typing import Any\n\n\n"
                "def deep_merge(\n"
                "    base: dict[str, Any], override: dict[str, Any]\n"
                ") -> dict[str, Any]:\n"
                "    merged = dict(base)\n"
                "    merged.update(override)\n"
                "    return merged\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_config_merge.py": (
                "import unittest\n"
                "from config_merge import deep_merge\n\n\n"
                "class TestConfigMerge(unittest.TestCase):\n"
                "    def test_merges_nested_dicts(self) -> None:\n"
                '        base = {"db": {"host": "localhost", "port": 5432}, "debug": False}\n'
                '        override = {"db": {"port": 6432, "ssl": True}}\n'
                "        result = deep_merge(base, override)\n"
                "        self.assertEqual(\n"
                "            result,\n"
                "            {\n"
                '                "db": {"host": "localhost", "port": 6432, "ssl": True},\n'
                '                "debug": False,\n'
                "            },\n"
                "        )\n"
            ),
        },
        hidden_tests={
            "tests/test_config_merge_hidden.py": (
                "import unittest\n"
                "from config_merge import deep_merge\n\n\n"
                "class TestConfigMergeHidden(unittest.TestCase):\n"
                "    def test_does_not_mutate_nested_inputs_or_append_lists(self) -> None:\n"
                "        base = {\n"
                '            "a": {"nested": {"k": 1}, "tags": ["v1"]},\n'
                '            "mode": {"strict": True},\n'
                "        }\n"
                '        override = {"a": {"nested": {"k": 2}, "tags": ["v2"]}, "mode": "fast"}\n'
                "        merged = deep_merge(base, override)\n"
                '        self.assertEqual(merged["mode"], "fast")\n'
                '        self.assertEqual(merged["a"]["tags"], ["v2"])\n'
                '        merged["a"]["nested"]["extra"] = 99\n'
                '        self.assertEqual(base["a"]["nested"], {"k": 1})\n'
                '        self.assertEqual(override["a"]["nested"], {"k": 2})\n'
            ),
        },
        incomplete_files={
            "config_merge.py": (
                '"""Configuration dictionary deep-merge utility."""\n\n'
                "from typing import Any\n\n\n"
                "def deep_merge(\n"
                "    base: dict[str, Any], override: dict[str, Any]\n"
                ") -> dict[str, Any]:\n"
                "    merged = dict(base)\n"
                "    for key, val in override.items():\n"
                "        if isinstance(merged.get(key), dict) and isinstance(val, dict):\n"
                "            merged[key].update(val)\n"
                "        else:\n"
                "            merged[key] = val\n"
                "    return merged\n"
            ),
        },
        known_good_files={
            "config_merge.py": (
                '"""Configuration dictionary deep-merge utility."""\n\n'
                "import copy\n"
                "from typing import Any\n\n\n"
                "def deep_merge(\n"
                "    base: dict[str, Any], override: dict[str, Any]\n"
                ") -> dict[str, Any]:\n"
                "    merged = copy.deepcopy(base)\n"
                "    for key, val in override.items():\n"
                "        if isinstance(merged.get(key), dict) and isinstance(val, dict):\n"
                "            merged[key] = deep_merge(merged[key], val)\n"
                "        else:\n"
                "            merged[key] = copy.deepcopy(val)\n"
                "    return merged\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-semver-comparator",
        title="Compare semantic versions numerically including prerelease precedence",
        prompt=(
            "Inspect semver.py and tests/test_semver.py. Fix compare_versions(a, b) to return "
            "-1, 0, or 1 comparing numeric MAJOR.MINOR.PATCH components numerically and "
            "ordering optional prerelease tags (e.g. 1.0.0-alpha < 1.0.0)."
        ),
        verify_command=VERIFY_CMD,
        suite="full",
        files={
            "semver.py": (
                '"""Semantic version comparator."""\n\n'
                "def compare_versions(a: str, b: str) -> int:\n"
                "    return (a > b) - (a < b)\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_semver.py": (
                "import unittest\n"
                "from semver import compare_versions\n\n\n"
                "class TestSemver(unittest.TestCase):\n"
                "    def test_numeric_minor_patch(self) -> None:\n"
                '        self.assertEqual(compare_versions("1.2.0", "1.10.0"), -1)\n'
                '        self.assertEqual(compare_versions("2.0.0", "2.0.0"), 0)\n'
                '        self.assertEqual(compare_versions("1.10.2", "1.9.9"), 1)\n'
            ),
        },
        hidden_tests={
            "tests/test_semver_hidden.py": (
                "import unittest\n"
                "from semver import compare_versions\n\n\n"
                "class TestSemverHidden(unittest.TestCase):\n"
                "    def test_prerelease_precedence(self) -> None:\n"
                '        self.assertEqual(compare_versions("1.0.0-alpha", "1.0.0"), -1)\n'
                '        self.assertEqual(compare_versions("1.0.0", "1.0.0-rc.1"), 1)\n'
                "        self.assertEqual(\n"
                '            compare_versions("1.0.0-alpha.2", "1.0.0-alpha.10"), -1\n'
                "        )\n"
                '        self.assertEqual(compare_versions("1.0.0-alpha", "1.0.0-beta"), -1)\n'
            ),
        },
        incomplete_files={
            "semver.py": (
                '"""Semantic version comparator."""\n\n'
                "def _parse_core(version: str) -> tuple[int, ...]:\n"
                '    core = version.split("-", 1)[0]\n'
                '    return tuple(int(part) for part in core.split("."))\n\n\n'
                "def compare_versions(a: str, b: str) -> int:\n"
                "    pa, pb = _parse_core(a), _parse_core(b)\n"
                "    return (pa > pb) - (pa < pb)\n"
            ),
        },
        known_good_files={
            "semver.py": (
                '"""Semantic version comparator."""\n\n'
                "def _parse(\n"
                "    version: str,\n"
                ") -> tuple[tuple[int, int, int], bool, list[tuple[int, int | str]]]:\n"
                '    core_str, *rest = version.split("-", 1)\n'
                '    parts = core_str.split(".")\n'
                "    core = (int(parts[0]), int(parts[1]), int(parts[2]))\n"
                "    if not rest:\n"
                "        return core, True, []\n"
                "    tags: list[tuple[int, int | str]] = []\n"
                '    for item in rest[0].split("."):\n'
                "        if item.isdigit():\n"
                "            tags.append((0, int(item)))\n"
                "        else:\n"
                "            tags.append((1, item))\n"
                "    return core, False, tags\n\n\n"
                "def compare_versions(a: str, b: str) -> int:\n"
                "    core_a, rel_a, pre_a = _parse(a)\n"
                "    core_b, rel_b, pre_b = _parse(b)\n"
                "    if core_a != core_b:\n"
                "        return (core_a > core_b) - (core_a < core_b)\n"
                "    if rel_a != rel_b:\n"
                "        return 1 if rel_a else -1\n"
                "    return (pre_a > pre_b) - (pre_a < pre_b)\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-lru-cache-eviction",
        title="Refresh LRU cache recency on get and overwrite put",
        prompt=(
            "Inspect lru_cache.py and tests/test_lru_cache.py. Fix LRUCache so both get() hits "
            "and put() updates move the key to most-recently used before capacity eviction."
        ),
        verify_command=VERIFY_CMD,
        suite="full",
        files={
            "lru_cache.py": (
                '"""Bounded LRU cache."""\n\n'
                "class LRUCache:\n"
                "    def __init__(self, capacity: int) -> None:\n"
                "        self.capacity = capacity\n"
                "        self._data: dict[str, int] = {}\n\n"
                "    def get(self, key: str) -> int | None:\n"
                "        return self._data.get(key)\n\n"
                "    def put(self, key: str, value: int) -> None:\n"
                "        if len(self._data) >= self.capacity and key not in self._data:\n"
                "            oldest = next(iter(self._data))\n"
                "            del self._data[oldest]\n"
                "        self._data[key] = value\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_lru_cache.py": (
                "import unittest\n"
                "from lru_cache import LRUCache\n\n\n"
                "class TestLruCache(unittest.TestCase):\n"
                "    def test_get_refreshes_recency(self) -> None:\n"
                "        cache = LRUCache(2)\n"
                '        cache.put("a", 1)\n'
                '        cache.put("b", 2)\n'
                '        self.assertEqual(cache.get("a"), 1)\n'
                '        cache.put("c", 3)\n'
                '        self.assertEqual(cache.get("a"), 1)\n'
                '        self.assertIsNone(cache.get("b"))\n'
            ),
        },
        hidden_tests={
            "tests/test_lru_cache_hidden.py": (
                "import unittest\n"
                "from lru_cache import LRUCache\n\n\n"
                "class TestLruCacheHidden(unittest.TestCase):\n"
                "    def test_put_overwrite_refreshes_and_invalid_capacity(self) -> None:\n"
                "        with self.assertRaises(ValueError):\n"
                "            LRUCache(0)\n"
                "        cache = LRUCache(2)\n"
                '        cache.put("a", 1)\n'
                '        cache.put("b", 2)\n'
                '        cache.put("a", 10)\n'
                '        cache.put("c", 3)\n'
                '        self.assertEqual(cache.get("a"), 10)\n'
                '        self.assertIsNone(cache.get("b"))\n'
            ),
        },
        incomplete_files={
            "lru_cache.py": (
                '"""Bounded LRU cache."""\n\n'
                "from collections import OrderedDict\n\n\n"
                "class LRUCache:\n"
                "    def __init__(self, capacity: int) -> None:\n"
                "        self.capacity = capacity\n"
                "        self._data: OrderedDict[str, int] = OrderedDict()\n\n"
                "    def get(self, key: str) -> int | None:\n"
                "        if key not in self._data:\n"
                "            return None\n"
                "        self._data.move_to_end(key)\n"
                "        return self._data[key]\n\n"
                "    def put(self, key: str, value: int) -> None:\n"
                "        if len(self._data) >= self.capacity and key not in self._data:\n"
                "            self._data.popitem(last=False)\n"
                "        self._data[key] = value\n"
            ),
        },
        known_good_files={
            "lru_cache.py": (
                '"""Bounded LRU cache."""\n\n'
                "from collections import OrderedDict\n\n\n"
                "class LRUCache:\n"
                "    def __init__(self, capacity: int) -> None:\n"
                "        if capacity < 1:\n"
                '            raise ValueError("capacity must be >= 1")\n'
                "        self.capacity = capacity\n"
                "        self._data: OrderedDict[str, int] = OrderedDict()\n\n"
                "    def get(self, key: str) -> int | None:\n"
                "        if key not in self._data:\n"
                "            return None\n"
                "        self._data.move_to_end(key)\n"
                "        return self._data[key]\n\n"
                "    def put(self, key: str, value: int) -> None:\n"
                "        if key in self._data:\n"
                "            self._data.move_to_end(key)\n"
                "        elif len(self._data) >= self.capacity:\n"
                "            self._data.popitem(last=False)\n"
                "        self._data[key] = value\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-dag-toposort",
        title="Return deterministic lexicographical topological order and reject cycles",
        prompt=(
            "Inspect dag.py and tests/test_dag.py. Fix toposort(deps) so every dependency comes "
            "before its dependents in lexicographically smallest order, rejecting cycles."
        ),
        verify_command=VERIFY_CMD,
        suite="full",
        files={
            "dag.py": (
                '"""Dependency graph topological sort."""\n\n'
                "def toposort(deps: dict[str, list[str]]) -> list[str]:\n"
                "    return sorted(deps.keys())\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_dag.py": (
                "import unittest\n"
                "from dag import toposort\n\n\n"
                "class TestDag(unittest.TestCase):\n"
                "    def test_linear_chain(self) -> None:\n"
                '        deps = {"build": ["compile"], "compile": ["fetch"], "fetch": []}\n'
                '        self.assertEqual(toposort(deps), ["fetch", "compile", "build"])\n'
            ),
        },
        hidden_tests={
            "tests/test_dag_hidden.py": (
                "import unittest\n"
                "from dag import toposort\n\n\n"
                "class TestDagHidden(unittest.TestCase):\n"
                "    def test_lexicographical_tiebreak_and_cycles(self) -> None:\n"
                '        deps = {"z": [], "b": ["a"], "a": [], "m": ["a", "z"]}\n'
                '        self.assertEqual(toposort(deps), ["a", "b", "z", "m"])\n'
                "        with self.assertRaises(ValueError):\n"
                '            toposort({"a": ["b"], "b": ["a"]})\n'
                "        with self.assertRaises(ValueError):\n"
                '            toposort({"a": ["a"]})\n'
            ),
        },
        incomplete_files={
            "dag.py": (
                '"""Dependency graph topological sort."""\n\n'
                "from collections import deque\n\n\n"
                "def toposort(deps: dict[str, list[str]]) -> list[str]:\n"
                "    indegree = {node: len(req) for node, req in deps.items()}\n"
                "    dependents: dict[str, list[str]] = {node: [] for node in deps}\n"
                "    for node, reqs in deps.items():\n"
                "        for req in reqs:\n"
                "            dependents.setdefault(req, []).append(node)\n"
                "    queue = deque(node for node, deg in indegree.items() if deg == 0)\n"
                "    order: list[str] = []\n"
                "    while queue:\n"
                "        cur = queue.popleft()\n"
                "        order.append(cur)\n"
                "        for nxt in dependents[cur]:\n"
                "            indegree[nxt] -= 1\n"
                "            if indegree[nxt] == 0:\n"
                "                queue.append(nxt)\n"
                "    if len(order) != len(deps):\n"
                '        raise ValueError("cycle detected")\n'
                "    return order\n"
            ),
        },
        known_good_files={
            "dag.py": (
                '"""Dependency graph topological sort."""\n\n'
                "import heapq\n\n\n"
                "def toposort(deps: dict[str, list[str]]) -> list[str]:\n"
                "    nodes = set(deps.keys())\n"
                "    for reqs in deps.values():\n"
                "        nodes.update(reqs)\n"
                "    indegree = {node: len(set(deps.get(node, []))) for node in nodes}\n"
                "    dependents: dict[str, list[str]] = {node: [] for node in nodes}\n"
                "    for node, reqs in deps.items():\n"
                "        for req in set(reqs):\n"
                "            dependents[req].append(node)\n"
                "    heap = [node for node, deg in indegree.items() if deg == 0]\n"
                "    heapq.heapify(heap)\n"
                "    order: list[str] = []\n"
                "    while heap:\n"
                "        cur = heapq.heappop(heap)\n"
                "        order.append(cur)\n"
                "        for nxt in dependents[cur]:\n"
                "            indegree[nxt] -= 1\n"
                "            if indegree[nxt] == 0:\n"
                "                heapq.heappush(heap, nxt)\n"
                "    if len(order) != len(nodes):\n"
                '        raise ValueError("cycle detected")\n'
                "    return order\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-markdown-link-extractor",
        title="Extract external HTTP links ignoring inline code spans across modules",
        prompt=(
            "Inspect scanner.py, links.py, and tests/test_links.py. Fix strip_code_spans() in "
            "scanner.py and extract_links() in links.py to return unique HTTP/HTTPS URLs from "
            "Markdown links outside inline backticks in first-seen order."
        ),
        verify_command=VERIFY_CMD,
        suite="full",
        files={
            "scanner.py": (
                '"""Markdown pre-processing helpers."""\n\n'
                "def strip_code_spans(text: str) -> str:\n"
                "    return text\n"
            ),
            "links.py": (
                '"""Markdown link extractor."""\n\n'
                "import re\n"
                "from scanner import strip_code_spans\n\n"
                '_LINK_RE = re.compile(r"\\[[^\\]]+\\]\\(([^)]+)\\)")\n\n\n'
                "def extract_links(markdown: str) -> list[str]:\n"
                "    cleaned = strip_code_spans(markdown)\n"
                "    return sorted(set(_LINK_RE.findall(cleaned)))\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_links.py": (
                "import unittest\n"
                "from links import extract_links\n\n\n"
                "class TestLinks(unittest.TestCase):\n"
                "    def test_extracts_ordered_http_urls(self) -> None:\n"
                "        doc = (\n"
                '            "See [docs](https://zeta.example/docs), "\n'
                '            "[api](http://alpha.example/api), and "\n'
                '            "[again](https://zeta.example/docs)."\n'
                "        )\n"
                "        self.assertEqual(\n"
                "            extract_links(doc),\n"
                '            ["https://zeta.example/docs", "http://alpha.example/api"],\n'
                "        )\n"
            ),
        },
        hidden_tests={
            "tests/test_links_hidden.py": (
                "import unittest\n"
                "from links import extract_links\n\n\n"
                "class TestLinksHidden(unittest.TestCase):\n"
                "    def test_ignores_code_spans_and_relative_links(self) -> None:\n"
                "        doc = (\n"
                '            "Ignore `[code](https://skip.example)` and [local](./rel.md). "\n'
                '            "Keep [real](https://real.example/x)."\n'
                "        )\n"
                '        self.assertEqual(extract_links(doc), ["https://real.example/x"])\n'
            ),
        },
        incomplete_files={
            "links.py": (
                '"""Markdown link extractor."""\n\n'
                "import re\n"
                "from scanner import strip_code_spans\n\n"
                '_LINK_RE = re.compile(r"\\[[^\\]]+\\]\\((https?://[^)\\s]+)\\)")\n\n\n'
                "def extract_links(markdown: str) -> list[str]:\n"
                "    cleaned = strip_code_spans(markdown)\n"
                "    return list(dict.fromkeys(_LINK_RE.findall(cleaned)))\n"
            ),
        },
        known_good_files={
            "scanner.py": (
                '"""Markdown pre-processing helpers."""\n\n'
                "import re\n\n"
                '_CODE_SPAN_RE = re.compile(r"`[^`]*`")\n\n\n'
                "def strip_code_spans(text: str) -> str:\n"
                '    return _CODE_SPAN_RE.sub("", text)\n'
            ),
            "links.py": (
                '"""Markdown link extractor."""\n\n'
                "import re\n"
                "from scanner import strip_code_spans\n\n"
                '_LINK_RE = re.compile(r"\\[[^\\]]+\\]\\((https?://[^)\\s]+)\\)")\n\n\n'
                "def extract_links(markdown: str) -> list[str]:\n"
                "    cleaned = strip_code_spans(markdown)\n"
                "    return list(dict.fromkeys(_LINK_RE.findall(cleaned)))\n"
            ),
        },
    ),
    BenchmarkTask(
        id="fix-env-template-expander",
        title="Expand template variables with defaults and literal escaping",
        prompt=(
            "Inspect template.py and tests/test_template.py. Fix render_template(text, env) to "
            "expand ${VAR} and ${VAR:-default} safely in a single pass, turn $${VAR} into literal "
            "${VAR}, and raise KeyError when a variable without default is missing."
        ),
        verify_command=VERIFY_CMD,
        suite="full",
        files={
            "template.py": (
                '"""Environment variable template expander."""\n\n'
                "def render_template(text: str, env: dict[str, str]) -> str:\n"
                "    out = text\n"
                "    for key, val in env.items():\n"
                '        out = out.replace("${" + key + "}", val)\n'
                "    return out\n"
            ),
        },
        tests={
            "tests/__init__.py": "",
            "tests/test_template.py": (
                "import unittest\n"
                "from template import render_template\n\n\n"
                "class TestTemplate(unittest.TestCase):\n"
                "    def test_expands_and_uses_default(self) -> None:\n"
                "        self.assertEqual(\n"
                '            render_template("${HOST}:${PORT:-8080}", {"HOST": "localhost"}),\n'
                '            "localhost:8080",\n'
                "        )\n\n"
                "    def test_missing_raises_key_error(self) -> None:\n"
                "        with self.assertRaises(KeyError):\n"
                '            render_template("${MISSING}", {})\n'
            ),
        },
        hidden_tests={
            "tests/test_template_hidden.py": (
                "import unittest\n"
                "from template import render_template\n\n\n"
                "class TestTemplateHidden(unittest.TestCase):\n"
                "    def test_escape_empty_default_and_no_double_expansion(self) -> None:\n"
                "        self.assertEqual(\n"
                "            render_template(\n"
                '                "$${HOST}=${HOST}", {"HOST": "${INNER}", "INNER": "bad"}\n'
                "            ),\n"
                '            "${HOST}=${INNER}",\n'
                "        )\n"
                "        self.assertEqual(\n"
                '            render_template("${PORT:-8080}", {"PORT": ""}),\n'
                '            "8080",\n'
                "        )\n"
            ),
        },
        incomplete_files={
            "template.py": (
                '"""Environment variable template expander."""\n\n'
                "import re\n\n"
                '_TOKEN_RE = re.compile(r"\\$\\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\\}")\n\n\n'
                "def render_template(text: str, env: dict[str, str]) -> str:\n"
                "    def _sub(match: re.Match[str]) -> str:\n"
                "        name, default = match.group(1), match.group(2)\n"
                "        if name in env:\n"
                "            return env[name]\n"
                "        if default is not None:\n"
                "            return default\n"
                "        raise KeyError(name)\n\n"
                "    return _TOKEN_RE.sub(_sub, text)\n"
            ),
        },
        known_good_files={
            "template.py": (
                '"""Environment variable template expander."""\n\n'
                "import re\n\n"
                "_TOKEN_RE = re.compile(\n"
                '    r"(\\$)?\\$\\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\\}"\n'
                ")\n\n\n"
                "def render_template(text: str, env: dict[str, str]) -> str:\n"
                "    def _sub(match: re.Match[str]) -> str:\n"
                "        escaped, name, default = match.group(1), match.group(2), match.group(3)\n"
                "        if escaped:\n"
                "            if default is not None:\n"
                "                return f'${{{name}:-{default}}}'\n"
                "            return f'${{{name}}}'\n"
                "        val = env.get(name)\n"
                "        if val not in (None, ''):\n"
                "            return val\n"
                "        if default is not None:\n"
                "            return default\n"
                "        if name in env:\n"
                "            return ''\n"
                "        raise KeyError(name)\n\n"
                "    return _TOKEN_RE.sub(_sub, text)\n"
            ),
        },
    ),
)


def resolve_tasks(
    task_ids: list[str] | None = None,
    *,
    suite: Literal["smoke", "full"] | None = None,
) -> list[BenchmarkTask]:
    """Select built-in benchmark tasks by explicit IDs or suite tier."""
    by_id = {task.id: task for task in BUILTIN_TASKS}
    if task_ids:
        selected: list[BenchmarkTask] = []
        for task_id in task_ids:
            if task_id not in by_id:
                available = ", ".join(sorted(by_id))
                msg = f"Unknown benchmark task '{task_id}'. Available: {available}"
                raise ValueError(msg)
            selected.append(by_id[task_id])
        return selected
    if suite == "smoke":
        return [task for task in BUILTIN_TASKS if task.suite == "smoke"]
    return list(BUILTIN_TASKS)
