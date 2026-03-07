"""Tests for the sample Flask app."""

from app import app


def test_index():
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "Hello from Agent Forge sample app"


def test_health_returns_200():
    """This test SHOULD pass once the bug is fixed."""
    client = app.test_client()
    resp = client.get("/health")
    # Currently fails — the bug returns 500
    assert resp.status_code == 200


def test_greet():
    client = app.test_client()
    resp = client.get("/greet/World")
    assert resp.status_code == 200
    assert resp.get_json()["greeting"] == "Hello, World!"
