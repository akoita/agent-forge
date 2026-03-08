"""Utility functions for the sample Flask app.

These functions deliberately lack type hints —
the agent's job is to add them without breaking tests.
"""


def calculate_total(items, tax_rate=0.1):
    """Calculate total price with tax."""
    subtotal = sum(item["price"] * item["quantity"] for item in items)
    return round(subtotal * (1 + tax_rate), 2)


def format_greeting(name, title=None):
    """Format a greeting string."""
    if title:
        return f"Hello, {title} {name}!"
    return f"Hello, {name}!"


def parse_csv_line(line, delimiter=","):
    """Split a CSV line into fields, stripping whitespace."""
    return [field.strip() for field in line.split(delimiter)]
