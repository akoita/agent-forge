"""Minimal Flask app for agent testing.

Contains a deliberate bug: the /health endpoint returns 500
instead of 200 when the app starts. The agent's job is to find and fix it.
"""

from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify({"message": "Hello from Agent Forge sample app"})


@app.route("/health")
def health():
    # BUG: should return 200, but returns 500
    return jsonify({"status": "error"}), 500


@app.route("/greet/<name>")
def greet(name):
    return jsonify({"greeting": f"Hello, {name}!"})


if __name__ == "__main__":
    app.run(debug=True)
