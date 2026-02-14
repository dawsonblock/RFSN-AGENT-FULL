import hashlib
import json
import os
import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]
import requests  # type: ignore[import-untyped]
from fastapi import FastAPI  # type: ignore[import-not-found]
from fastapi.responses import JSONResponse  # type: ignore[import-not-found]
from pydantic import BaseModel  # type: ignore[import-not-found]

from typing import Optional

import sys

sys.path.insert(0, "/shared")
try:
    from auth import ServiceAuthMiddleware  # type: ignore[import-not-found]

    _HAS_AUTH = True
except ImportError:
    _HAS_AUTH = False

# ── Auth-required guard ────────────────────────────────
_AUTH_REQUIRED = os.getenv("RFSN_AUTH_REQUIRED", "1") == "1"
if not _HAS_AUTH and _AUTH_REQUIRED:
    if os.getenv("RFSN_DEV_MODE", "0") != "1":
        raise SystemExit(
            "FATAL: auth module not available and RFSN_AUTH_REQUIRED=1. "
            "Set RFSN_DEV_MODE=1 to bypass (dev only)."
        )

# ── Logging & Metrics ──────────────────────────────────
import time
from system.logging import configure_logger, get_logger
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

configure_logger()
logger = get_logger("llm_service")

# Metrics
REQUEST_LATENCY = Histogram(
    "llm_request_latency_seconds",
    "Time spent processing LLM requests",
    ["model", "status"],
)
REQUEST_COUNT = Counter("llm_requests_total", "Total LLM requests", ["model", "status"])

app = FastAPI()
if _HAS_AUTH:
    app.add_middleware(ServiceAuthMiddleware)  # type: ignore[possibly-unbound]


@app.middleware("http")
async def monitor_requests(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time

    # Extract route tag if possible
    status_code = str(response.status_code)

    # Log request
    logger.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=status_code,
        duration=duration,
    )

    return response


DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv(
    "DEEPSEEK_BASE_URL",
    "https://api.deepseek.com",
).rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-reasoner")

try:
    with open("/policies/llm_cassette.yaml", "r", encoding="utf-8") as _f:
        CASSETTE = yaml.safe_load(_f) or {}
except (FileNotFoundError, PermissionError) as _exc:
    logger.error("cassette_load_failed", error=str(_exc))
    raise SystemExit(1) from _exc

SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _canon(obj) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _key(payload: dict) -> str:
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()


def _resolve_path(
    repo_id: Optional[str],
    scenario: Optional[str],
) -> Path:
    tpl = CASSETTE.get("path_template")
    if not tpl:
        return Path(
            CASSETTE.get(
                "path",
                "/data/cassettes/deepseek_chat.jsonl",
            )
        )
    rid = repo_id or "unknown"
    scn = scenario or CASSETTE.get("default_scenario", "golden")
    if not SAFE_NAME.match(rid):
        rid = "unknown"
    if not SAFE_NAME.match(scn):
        scn = CASSETTE.get("default_scenario", "golden")
    p = Path(tpl.format(repo_id=rid, scenario=scn))
    root = Path("/data/cassettes").resolve()
    rp = p.resolve()
    if root not in rp.parents and rp != root:
        raise RuntimeError("cassette path escaped /data/cassettes")
    return p


def _load_cassette(p: Path) -> dict:
    if not p.exists():
        return {}
    m = {}
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec["key"] in m:
                raise RuntimeError(f"duplicate cassette key: {rec['key']}")
            m[rec["key"]] = rec
    return m


def _append_cassette(p: Path, rec: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) >= int(CASSETTE.get("max_records", 500)):
            keep = lines[int(len(lines) * 0.2) :]
            p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class ChatReq(BaseModel):
    messages: list
    temperature: float = 0.2
    max_tokens: int = 1600
    run_id: Optional[str] = None
    call_index: Optional[int] = None
    repo_id: Optional[str] = None
    scenario: Optional[str] = None


@app.get("/health")
def health():
    return {"ok": True, "cassette_mode": (CASSETTE.get("mode") or "off")}


@app.get("/metrics")
def metrics():
    return JSONResponse(
        content=generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST
    )


def deepseek_chat(messages, temperature, max_tokens):
    if not DEEPSEEK_API_KEY:
        logger.error("missing_api_key")
        return JSONResponse(
            status_code=500,
            content={
                "error": "missing_deepseek_api_key",
            },
        )

    start = time.time()
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        r = requests.post(url, headers=headers, json=body, timeout=120)
        duration = time.time() - start

        status = str(r.status_code)
        REQUEST_LATENCY.labels(model=DEEPSEEK_MODEL, status=status).observe(duration)
        REQUEST_COUNT.labels(model=DEEPSEEK_MODEL, status=status).inc()

        if r.status_code != 200:
            logger.error("upstream_error", status=status, body=r.text)
            return JSONResponse(
                status_code=r.status_code,
                content={
                    "error": "deepseek_error",
                    "details": r.text,
                },
            )

        j = r.json()
        content = j["choices"][0]["message"]["content"]
        return {"content": content, "raw": j}

    except Exception as e:
        logger.exception("upstream_call_failed", error=str(e))
        REQUEST_COUNT.labels(model=DEEPSEEK_MODEL, status="exception").inc()
        return JSONResponse(
            status_code=500, content={"error": "upstream_exception", "details": str(e)}
        )


@app.post("/chat")
def chat(req: ChatReq):
    mode = (CASSETTE.get("mode") or "off").lower()
    cass_path = _resolve_path(req.repo_id, req.scenario)

    logger.info("chat_request", repo=req.repo_id, mode=mode)

    payload = {
        "messages": req.messages,
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
    }

    if CASSETTE.get("use_run_scope", True) and req.run_id:
        payload["run_id"] = req.run_id
    if CASSETTE.get("use_call_index", True) and (req.call_index is not None):
        payload["call_index"] = int(req.call_index)
    if req.repo_id:
        payload["repo_id"] = req.repo_id
    if req.scenario:
        payload["scenario"] = req.scenario

    k = _key(payload)

    if mode == "replay":
        table = _load_cassette(cass_path)
        if k in table:
            rec = table[k]
            return {
                "content": rec["content"],
                "raw": {
                    "cassette": "replay",
                    "key": k,
                },
            }
        if CASSETTE.get("strict", True):
            logger.warning("cassette_miss", key=k)
            return JSONResponse(
                status_code=500,
                content={
                    "error": "cassette_miss",
                    "key": k,
                    "cassette": str(cass_path),
                },
            )
        # non-strict: fall through to live

    live = deepseek_chat(req.messages, req.temperature, req.max_tokens)
    if isinstance(live, JSONResponse):
        return live
    content = live["content"]

    if mode == "record":
        rec = {"key": k, "request": payload, "content": content}
        _append_cassette(cass_path, rec)
        return {
            "content": content,
            "raw": {
                "cassette": "record",
                "key": k,
            },
        }

    return {"content": content, "raw": live.get("raw", {})}
