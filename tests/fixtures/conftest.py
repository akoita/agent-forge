"""Prevent pytest from collecting files in the fixtures directory.

These files are sample application code used as test fixtures,
not actual test modules.
"""

collect_ignore_glob = ["*"]
