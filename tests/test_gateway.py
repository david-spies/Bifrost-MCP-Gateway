"""
Bifrost-MCP Gateway — Test Suite
Run: pytest tests/ -v
"""

import pytest
from fastapi.testclient import TestClient

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import gateway as gw
from gateway import app

# Pre-seed the registry so tests don't depend on lifespan
config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
import json
with open(config_path) as f:
    gw.TOOL_REGISTRY = json.load(f)

client = TestClient(app)


# ── Health ────────────────────────────────────────────────────────────────────

def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def test_mcp_init_returns_bootstrap_tools():
    r = client.get("/mcp/init")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "connected"
    assert len(body["injectable_tools"]) == 2
    tool_names = [t["name"] for t in body["injectable_tools"]]
    assert "request_tool_schemas" in tool_names
    assert "execute_orchestration_script" in tool_names


def test_mcp_init_token_audit():
    r = client.get("/mcp/init")
    audit = r.json()["token_audit"]
    assert audit["tokens_saved"] >= 0
    assert audit["full_registry_tokens"] > audit["compressed_tokens"]


def test_mcp_init_catalog_compressed():
    """Compressed catalog must not contain raw 'schema' blobs."""
    r = client.get("/mcp/init")
    catalog = r.json()["compressed_catalog"]
    for entry in catalog:
        assert "schema" not in entry
        assert "input_schema" not in entry
        assert "purpose" in entry


# ── Schema Resolution ─────────────────────────────────────────────────────────

def test_resolve_github_intent():
    r = client.post("/mcp/tools/resolve", json={"user_intent": "github"})
    assert r.status_code == 200
    body = r.json()
    assert body["match_count"] > 0
    for tool in body["matched_tools"]:
        assert "input_schema" in tool


def test_resolve_filesystem_intent():
    r = client.post("/mcp/tools/resolve", json={"user_intent": "filesystem"})
    assert r.status_code == 200
    assert r.json()["match_count"] >= 2


def test_resolve_database_intent():
    r = client.post("/mcp/tools/resolve", json={"user_intent": "database postgres"})
    assert r.status_code == 200
    assert r.json()["match_count"] > 0


def test_resolve_no_match_returns_empty():
    r = client.post("/mcp/tools/resolve", json={"user_intent": "zzznomatch99999"})
    assert r.status_code == 200
    assert r.json()["match_count"] == 0


def test_resolve_token_savings_positive():
    r = client.post("/mcp/tools/resolve", json={"user_intent": "slack"})
    audit = r.json()["token_audit"]
    assert audit["tokens_saved"] >= 0
    assert 0 <= audit["reduction_pct"] <= 100


# ── Orchestration ─────────────────────────────────────────────────────────────

def test_orchestrate_simple_script():
    r = client.post("/mcp/tools/orchestrate", json={"script": "print('bifrost ok')"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert "bifrost ok" in body["stdout"]


def test_orchestrate_multi_line():
    script = "x = 2 + 2\nprint(f'result={x}')"
    r = client.post("/mcp/tools/orchestrate", json={"script": script})
    assert r.status_code == 200
    assert "result=4" in r.json()["stdout"]


def test_orchestrate_blocks_rm_rf():
    r = client.post("/mcp/tools/orchestrate", json={"script": "import os\nrm -rf /"})
    assert r.status_code == 403


def test_orchestrate_blocks_os_system():
    r = client.post("/mcp/tools/orchestrate", json={"script": "os.system('ls')"})
    assert r.status_code == 403


def test_orchestrate_blocks_eval():
    r = client.post("/mcp/tools/orchestrate", json={"script": "eval('1+1')"})
    assert r.status_code == 403


def test_orchestrate_blocks_subprocess_popen():
    r = client.post("/mcp/tools/orchestrate", json={"script": "subprocess.Popen(['ls'])"})
    assert r.status_code == 403


def test_orchestrate_syntax_error_returns_error():
    r = client.post("/mcp/tools/orchestrate", json={"script": "def broken(:\n  pass"})
    assert r.status_code == 200
    assert r.json()["status"] == "error"


# ── Registry CRUD ─────────────────────────────────────────────────────────────

def test_list_registry_not_empty():
    r = client.get("/mcp/registry")
    assert r.status_code == 200
    assert r.json()["tool_count"] > 0


def test_add_and_remove_tool():
    new_tool = {
        "name": "test_dummy_tool",
        "category": "Testing",
        "description": "A transient tool used only for unit testing.",
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    }
    # Add
    r = client.post("/mcp/registry/add", json=new_tool)
    assert r.status_code == 200
    assert r.json()["status"] == "registered"

    # Duplicate add should 409
    r2 = client.post("/mcp/registry/add", json=new_tool)
    assert r2.status_code == 409

    # Remove
    r3 = client.delete("/mcp/registry/test_dummy_tool")
    assert r3.status_code == 200
    assert r3.json()["status"] == "removed"

    # Second remove should 404
    r4 = client.delete("/mcp/registry/test_dummy_tool")
    assert r4.status_code == 404


# ── Analytics ─────────────────────────────────────────────────────────────────

def test_analytics_endpoint():
    r = client.get("/analytics")
    assert r.status_code == 200
    body = r.json()
    for key in ("total_requests", "tokens_saved_total", "registry_size"):
        assert key in body
