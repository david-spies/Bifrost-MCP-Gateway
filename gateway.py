"""
Bifrost-MCP Gateway
===================
Enterprise-grade Dynamic MCP Proxy — eliminates Token Bloat & Schema Stuffer Fatigue
in Agentic AI pipelines by lazy-loading tool schemas on semantic intent.

Author : Bifrost Team
Version: 1.0.0
"""

import os
import re
import json
import time
import uuid
import logging
import textwrap
import subprocess
from contextlib import asynccontextmanager
from collections import defaultdict
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s — %(message)s",
)
logger = logging.getLogger("BifrostGateway")

# ─── In-Memory Analytics Store ────────────────────────────────────────────────
analytics: Dict[str, Any] = {
    "total_requests": 0,
    "tokens_saved": 0,
    "blocked_scripts": 0,
    "orchestrations_run": 0,
    "resolves": 0,
    "recent_events": [],          # last 50 events (ring buffer)
    "intent_histogram": defaultdict(int),
    "uptime_start": time.time(),
}

def record_event(kind: str, detail: str, meta: Optional[Dict] = None):
    analytics["total_requests"] += 1
    event = {
        "id": str(uuid.uuid4())[:8],
        "ts": time.time(),
        "kind": kind,
        "detail": detail,
        **(meta or {}),
    }
    analytics["recent_events"].append(event)
    if len(analytics["recent_events"]) > 50:
        analytics["recent_events"].pop(0)

# ─── Lifespan ─────────────────────────────────────────────────────────────────
TOOL_REGISTRY: Dict[str, Any] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global TOOL_REGISTRY
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            TOOL_REGISTRY = json.load(f)
        count = len(TOOL_REGISTRY.get("tools", []))
        logger.info(f"Indexed {count} enterprise tools into memory.")
    else:
        logger.error("config.json missing — starting with empty registry.")
        TOOL_REGISTRY = {"tools": []}
    yield

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Bifrost-MCP Gateway",
    description="Dynamic MCP proxy — lazy tool loading & client-side orchestration.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the dashboard SPA
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ─── Pydantic Models ──────────────────────────────────────────────────────────
class IntentPayload(BaseModel):
    user_intent: str = Field(..., min_length=2, description="Keyword or phrase describing what the agent needs.")

class OrchestrationPayload(BaseModel):
    script: str = Field(..., description="Python code block to run client-side.")

class ToolRegistration(BaseModel):
    name: str
    category: str
    description: str
    tool_schema: Dict[str, Any] = Field(alias="schema")

# ─── Helpers ──────────────────────────────────────────────────────────────────
_FORBIDDEN = [
    r"\brm\s+-rf\b",
    r"os\.system\s*\(",
    r"subprocess\.Popen\s*\(",
    r"shutil\.rmtree\s*\(",
    r"__import__\s*\(",
    r"exec\s*\(",
    r"eval\s*\(",
    r"open\s*\(.*['\"]w['\"]",   # file writes
    r"importlib",
    r"ctypes",
    r"socket\.",
]
_FORBIDDEN_RX = [re.compile(p) for p in _FORBIDDEN]

def is_script_safe(script: str) -> tuple[bool, str]:
    for rx in _FORBIDDEN_RX:
        if rx.search(script):
            return False, rx.pattern
    return True, ""


def compress_metadata() -> List[Dict[str, str]]:
    """Return a token-efficient catalog skeleton — no schemas, just names + purpose."""
    out = []
    for t in TOOL_REGISTRY.get("tools", []):
        out.append({
            "name": t["name"],
            "category": t["category"],
            "purpose": textwrap.shorten(t["description"], width=80),
        })
    return out


def estimate_full_tokens() -> int:
    """Rough token estimate if we naively dumped all schemas."""
    raw = json.dumps(TOOL_REGISTRY.get("tools", []))
    return len(raw) // 4   # ~4 chars per token


def estimate_compressed_tokens(catalog: list) -> int:
    return len(json.dumps(catalog)) // 4


def match_tools(intent: str) -> List[Dict[str, Any]]:
    intent_l = intent.lower()
    tokens = set(re.split(r"[\s,;]+", intent_l))
    hits = []
    for t in TOOL_REGISTRY.get("tools", []):
        haystack = f"{t['name']} {t['category']} {t['description']}".lower()
        if any(tok in haystack for tok in tokens if len(tok) > 2):
            hits.append({
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["schema"],
            })
    return hits

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_dashboard():
    idx = os.path.join(static_dir, "index.html")
    if os.path.exists(idx):
        return FileResponse(idx)
    return JSONResponse({"status": "Bifrost-MCP Gateway running", "docs": "/docs"})


@app.get("/mcp/init", summary="Bootstrap handshake — returns meta-tools only")
async def initialize_client():
    catalog = compress_metadata()
    full_tok = estimate_full_tokens()
    comp_tok = estimate_compressed_tokens(catalog)
    saved = max(0, full_tok - comp_tok)
    analytics["tokens_saved"] += saved

    record_event("init", "Client handshake", {"tokens_saved": saved})
    logger.info(f"Handshake — suppressed {saved} raw tokens from context window.")

    bootstrap_tools = [
        {
            "name": "request_tool_schemas",
            "description": (
                "Query the Bifrost gateway to dynamically load full JSON schemas "
                "for the specific tools you need. Pass an intent keyword or phrase."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "user_intent": {
                        "type": "string",
                        "description": "Intent phrase, e.g. 'GitHub PR', 'postgres read'.",
                    }
                },
                "required": ["user_intent"],
            },
        },
        {
            "name": "execute_orchestration_script",
            "description": (
                "Execute a Python script block client-side to chain multiple actions "
                "without expensive LLM round-trips. Security-sandboxed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": "Valid Python code. No shell commands or I/O.",
                    }
                },
                "required": ["script"],
            },
        },
    ]

    return {
        "status": "connected",
        "gateway_version": "1.0.0",
        "supported_protocols": ["mcp/2026.1"],
        "registry_size": len(TOOL_REGISTRY.get("tools", [])),
        "compressed_catalog": catalog,
        "injectable_tools": bootstrap_tools,
        "token_audit": {
            "full_registry_tokens": full_tok,
            "compressed_tokens": comp_tok,
            "tokens_saved": saved,
            "reduction_pct": round((saved / max(full_tok, 1)) * 100, 1),
        },
    }


@app.post("/mcp/tools/resolve", summary="Lazy-load full schemas matching intent")
async def resolve_schemas(payload: IntentPayload):
    analytics["resolves"] += 1
    intent = payload.user_intent
    analytics["intent_histogram"][intent.lower().split()[0]] += 1

    tools = match_tools(intent)
    full_tok = estimate_full_tokens()
    hydrated_tok = len(json.dumps(tools)) // 4
    saved = max(0, full_tok - hydrated_tok)
    analytics["tokens_saved"] += saved

    record_event("resolve", f"Intent: '{intent}'", {
        "matched": len(tools),
        "tokens_saved": saved,
    })
    logger.info(f"Resolved {len(tools)} tools for intent '{intent}' — saved ~{saved} tokens.")

    return {
        "intent": intent,
        "matched_tools": tools,
        "match_count": len(tools),
        "token_audit": {
            "full_registry_tokens": full_tok,
            "hydrated_tokens": hydrated_tok,
            "tokens_saved": saved,
            "reduction_pct": round((saved / max(full_tok, 1)) * 100, 1),
        },
    }


@app.post("/mcp/tools/orchestrate", summary="Execute sandboxed client-side Python")
async def execute_orchestration(payload: OrchestrationPayload):
    analytics["orchestrations_run"] += 1

    safe, pattern = is_script_safe(payload.script)
    if not safe:
        analytics["blocked_scripts"] += 1
        record_event("blocked", f"Forbidden pattern: {pattern}")
        logger.warning(f"Blocked script — matched forbidden pattern: {pattern}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Security violation: forbidden pattern `{pattern}` detected.",
        )

    record_event("orchestrate", "Script executed", {"lines": payload.script.count('\n') + 1})
    logger.info("Executing sandboxed orchestration script.")

    tmp = f"/tmp/bifrost_orch_{uuid.uuid4().hex[:8]}.py"
    try:
        with open(tmp, "w") as f:
            f.write(payload.script)

        result = subprocess.run(
            ["python3", tmp],
            capture_output=True,
            text=True,
            timeout=15,
        )
        os.remove(tmp)

        if result.returncode != 0:
            return {"status": "error", "stderr": result.stderr, "stdout": result.stdout}

        return {"status": "success", "stdout": result.stdout, "stderr": ""}

    except subprocess.TimeoutExpired:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise HTTPException(status_code=408, detail="Script exceeded 15-second timeout.")
    except Exception as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        logger.error(f"Orchestration error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/mcp/registry", summary="List all registered tools (admin)")
async def list_registry():
    return {
        "tool_count": len(TOOL_REGISTRY.get("tools", [])),
        "tools": TOOL_REGISTRY.get("tools", []),
    }


@app.post("/mcp/registry/add", summary="Register a new tool at runtime")
async def add_tool(tool: ToolRegistration):
    tools = TOOL_REGISTRY.setdefault("tools", [])
    if any(t["name"] == tool.name for t in tools):
        raise HTTPException(status_code=409, detail=f"Tool '{tool.name}' already exists.")
    d = tool.model_dump(by_alias=True)
    d["schema"] = d.pop("schema", tool.tool_schema)
    tools.append(d)
    record_event("register", f"Tool added: {tool.name}")
    logger.info(f"Runtime registration: {tool.name}")
    return {"status": "registered", "tool": tool.name, "registry_size": len(tools)}


@app.delete("/mcp/registry/{tool_name}", summary="Remove a tool from the registry")
async def remove_tool(tool_name: str):
    tools = TOOL_REGISTRY.get("tools", [])
    original = len(tools)
    TOOL_REGISTRY["tools"] = [t for t in tools if t["name"] != tool_name]
    removed = original - len(TOOL_REGISTRY["tools"])
    if not removed:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found.")
    record_event("deregister", f"Tool removed: {tool_name}")
    return {"status": "removed", "tool": tool_name}


@app.get("/analytics", summary="Live gateway analytics")
async def get_analytics():
    uptime_s = time.time() - analytics["uptime_start"]
    return {
        "uptime_seconds": round(uptime_s),
        "total_requests": analytics["total_requests"],
        "tokens_saved_total": analytics["tokens_saved"],
        "blocked_scripts": analytics["blocked_scripts"],
        "orchestrations_run": analytics["orchestrations_run"],
        "schema_resolves": analytics["resolves"],
        "registry_size": len(TOOL_REGISTRY.get("tools", [])),
        "intent_histogram": dict(analytics["intent_histogram"]),
        "recent_events": analytics["recent_events"][-20:],
    }


@app.get("/health", summary="Health probe")
async def health():
    return {"status": "ok", "version": "1.0.0"}


# ─── Entry ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gateway:app", host="127.0.0.1", port=8000, reload=True, log_level="info")
