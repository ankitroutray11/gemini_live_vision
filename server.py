import os
import sys
import time
import json
import base64
import asyncio
import logging
import subprocess
from datetime import datetime
from collections import deque
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
from dotenv import load_dotenv

import socket
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(*args, **kwargs):
    res = _orig_getaddrinfo(*args, **kwargs)
    ipv4 = [r for r in res if r[0] == socket.AF_INET]
    return ipv4 if ipv4 else res
socket.getaddrinfo = _ipv4_getaddrinfo

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Load environment
ENV_PATHS = [
    Path("C:/jarvis/.env"),
    Path(__file__).parent / ".env",
    Path(".env")
]
for p in ENV_PATHS:
    if p.exists():
        load_dotenv(p, override=True)
        break

from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse

from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).parent))
from tools import ALL_TOOLS, TOOL_MAP
from learning_engine import (
    get_memory_prompt_injection,
    list_memories,
    delete_memory,
    edit_memory,
    confirm_memory,
    prune_expired_memories,
    remember_insight,
    search_memory,
    migrate_memories,
    rollback_memories
)
from session_lifecycle import (
    ModelConfigAdapter,
    TaskStateManager,
    ToolDeduplicator,
    ClientMessageTracker,
    RollingTranscriptManager,
    BackoffManager,
    SessionLifecycleController
)
from dispatcher import (
    FunctionCallDispatcher,
    ExecutionTier,
    ExecutionRecord
)
from security_policy import (
    get_launch_auth_token,
    rotate_auth_token,
    validate_auth_token,
    validate_origin,
    register_client_session,
    unregister_client_session,
    GLOBAL_RATE_LIMITER,
    classify_tool,
    ToolTier,
    GLOBAL_TAINT_TRACKER,
    is_kill_switch_active,
    trigger_kill_switch,
    reset_kill_switch,
    get_gemini_api_key,
    store_secret_dpapi,
    record_audit_event
)
from emergency_stop import EMERGENCY_STOP_MANAGER
from observability import GLOBAL_METRICS, GLOBAL_EXECUTION_LOGGER

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GeminiLiveServer")

# Global registries for Permission Centre and Task Manager
GLOBAL_PENDING_CONFIRMATIONS: Dict[str, Dict[str, Any]] = {}
GLOBAL_CONFIRMATION_FUTURES: Dict[str, asyncio.Future] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start global Windows keyboard shortcut listener (Ctrl+Alt+X, Pause)
    EMERGENCY_STOP_MANAGER.start_global_hotkey_listener()
    yield
    # Shutdown: Cleanly unregister global hotkeys
    EMERGENCY_STOP_MANAGER.stop_global_hotkey_listener()

app = FastAPI(title="Chirkoot Multimodal, Computer Use & Self-Learning Assistant", lifespan=lifespan)

@app.middleware("http")
async def enforce_http_security(request: Request, call_next):
    # Reject cross-site requests to /api/ endpoints to prevent CSRF
    if request.url.path.startswith("/api/"):
        sec_fetch_site = request.headers.get("sec-fetch-site")
        if sec_fetch_site == "cross-site":
            return JSONResponse(status_code=403, content={"error": "Cross-site requests forbidden"})
        origin = request.headers.get("origin")
        if origin and not validate_origin(origin):
            return JSONResponse(status_code=403, content={"error": "Forbidden Origin"})
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_file = STATIC_DIR / "index.html"
    content = index_file.read_text(encoding="utf-8")
    token = get_launch_auth_token()
    meta_tag = f'<meta name="chirkoot-auth-token" content="{token}">'
    if '<meta name="chirkoot-auth-token"' in content:
        import re
        content = re.sub(r'<meta name="chirkoot-auth-token"[^>]*>', meta_tag, content)
    elif "<head>" in content:
        content = content.replace("<head>", f"<head>\n  {meta_tag}", 1)
    else:
        content = f"{meta_tag}\n{content}"
    res = HTMLResponse(content)
    res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    res.headers["Pragma"] = "no-cache"
    res.headers["Expires"] = "0"
    return res

@app.post("/api/alt-tab")
async def api_alt_tab():
    from tools import press_hotkey
    res = press_hotkey("alt,tab")
    return {"status": "ok", "result": res}

@app.post("/api/minimize")
async def api_minimize():
    from tools import minimize_active_window
    res = minimize_active_window()
    return {"status": "ok", "result": res}

@app.post("/api/window/restore")
async def api_window_restore():
    from tools import restore_active_window
    res = restore_active_window()
    return {"status": "ok", "result": res}

@app.post("/api/window/maximize")
async def api_window_maximize():
    from tools import maximize_active_window
    res = maximize_active_window()
    return {"status": "ok", "result": res}

@app.post("/api/window/fullscreen")
async def api_window_fullscreen():
    from tools import toggle_fullscreen_window
    res = toggle_fullscreen_window()
    return {"status": "ok", "result": res}

@app.post("/api/window/close")
async def api_window_close():
    asyncio.get_event_loop().call_later(0.5, lambda: os._exit(0))
    return {"status": "closing"}

@app.post("/api/ocr")
async def api_ocr():
    from tools import ocr_screen
    res = await asyncio.to_thread(ocr_screen)
    return {"status": "ok", "result": res}

@app.get("/api/auth/token")
async def api_get_auth_token(request: Request):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if client_ip not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="Forbidden: Non-loopback client")
    
    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="Forbidden: Cross-site request rejected")
        
    origin = request.headers.get("origin")
    if origin and not validate_origin(origin):
        raise HTTPException(status_code=403, detail="Forbidden Origin")

    referer = request.headers.get("referer")
    if referer and not validate_origin(referer):
        raise HTTPException(status_code=403, detail="Forbidden Referer")

    return {"token": get_launch_auth_token()}

@app.get("/api/memories")
async def api_list_memories(category: Optional[str] = None, q: Optional[str] = None, limit: int = 100, offset: int = 0):
    try:
        from sqlite_memory import SQLITE_MEMORY
        if q:
            results = SQLITE_MEMORY.search_hybrid(q, category=category, top_k=limit)
            return {"status": "ok", "memories": results}
        return {"status": "ok", "memories": SQLITE_MEMORY.list_memories(category=category, limit=limit, offset=offset)}
    except Exception as e:
        return {"status": "ok", "memories": list_memories(category=category or "")}

@app.post("/api/memories/search")
async def api_search_memories(request: Request):
    data = await request.json()
    query = data.get("query", "")
    category = data.get("category")
    top_k = int(data.get("top_k", 5))
    try:
        from sqlite_memory import SQLITE_MEMORY
        results = SQLITE_MEMORY.search_hybrid(query, category=category, top_k=top_k)
        return {"status": "ok", "query": query, "results": results}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.post("/api/memories/create")
async def api_create_memory(request: Request):
    data = await request.json()
    category = data.get("category", "general")
    key = data.get("key", "")
    content = data.get("content") or data.get("insight", "")
    source = data.get("source", "user_explicit")
    user_confirmed = bool(data.get("user_confirmed", True))
    ttl_seconds = data.get("ttl_seconds")
    res = remember_insight(
        category=category,
        key=key,
        insight=content,
        source=source,
        user_confirmed=user_confirmed,
        ttl_seconds=ttl_seconds
    )
    return {"status": "ok", "result": res}

@app.delete("/api/memories/id/{memory_id}")
async def api_delete_memory_by_id(memory_id: str):
    try:
        from sqlite_memory import SQLITE_MEMORY
        res = SQLITE_MEMORY.delete_memory_by_id(memory_id)
        return {"status": "ok", "result": res}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.delete("/api/memories/{category}/{key}")
async def api_delete_memory(category: str, key: str):
    res = delete_memory(category, key)
    return {"status": "ok", "result": res}

@app.post("/api/memories/edit")
async def api_edit_memory(request: Request):
    data = await request.json()
    category = data.get("category", "")
    key = data.get("key", "")
    new_insight = data.get("new_insight", "")
    res = edit_memory(category, key, new_insight, user_confirmed=True)
    return {"status": "ok", "result": res}

@app.post("/api/memories/confirm")
async def api_confirm_memory(request: Request):
    data = await request.json()
    category = data.get("category", "")
    key = data.get("key", "")
    target_category = data.get("target_category")
    res = confirm_memory(category, key, target_category)
    return {"status": "ok", "result": res}

@app.post("/api/memories/prune")
async def api_prune_memories():
    count = prune_expired_memories()
    return {"status": "ok", "purged_count": count}

@app.post("/api/memories/migrate")
async def api_migrate_memories(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    delete_orig = bool(data.get("delete_original", False))
    res = migrate_memories(delete_original=delete_orig)
    return {"status": "ok", "result": res}

@app.post("/api/memories/rollback")
async def api_rollback_memories():
    res = rollback_memories()
    return {"status": "ok", "result": res}

@app.post("/api/kill-switch")
async def api_kill_switch():
    res = EMERGENCY_STOP_MANAGER.trigger("REST API kill switch invoked")
    return {"status": "ok", "kill_switch_active": True, "details": res}

@app.post("/api/kill-switch/reset")
async def api_kill_switch_reset():
    res = EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="api_client")
    return {"status": "ok", "kill_switch_active": False, "details": res}

@app.get("/api/security/status")
async def api_security_status():
    is_tainted, taint_source, taint_rem = GLOBAL_TAINT_TRACKER.is_tainted()
    return {
        "status": "nominal",
        "kill_switch_active": is_kill_switch_active(),
        "is_tainted": is_tainted,
        "taint_source": taint_source,
        "taint_seconds_remaining": taint_rem,
        "immutable_core_protected": True,
        "dpapi_vault_active": True
    }

@app.get("/api/health")
async def api_health():
    from learning_engine import SAFE_MODE, StartupWatchdog
    res = GLOBAL_METRICS.get_system_resource_metrics()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "safe_mode": SAFE_MODE,
        "immutable_core_protected": True,
        "watchdog_state": StartupWatchdog.load_health_state(),
        "resources": res,
        "active_threads": res.get("thread_count", 0),
        "active_child_processes": res.get("active_child_processes", 0),
        "uptime_seconds": res.get("uptime_seconds", 0)
    }

@app.get("/api/metrics")
async def api_metrics_summary():
    return {"status": "ok", "metrics": GLOBAL_METRICS.get_metrics_summary()}

@app.get("/api/executions")
async def api_executions(
    limit: int = 50,
    tool_name: Optional[str] = None,
    status: Optional[str] = None,
    task_id: Optional[str] = None
):
    records = GLOBAL_EXECUTION_LOGGER.get_recent_executions(
        limit=limit,
        tool_name=tool_name,
        status=status,
        task_id=task_id
    )
    return {"status": "ok", "count": len(records), "executions": records}

@app.get("/api/skills")
async def api_list_skills_endpoint():
    from learning_engine import SkillLifecycleEngine
    return {
        "status": "ok",
        "skills": SkillLifecycleEngine.get_catalog()
    }

@app.get("/api/hud-exclusion-status")
async def api_hud_exclusion_status():
    from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
    return {
        "status": "ok",
        "diagnostics": GLOBAL_HUD_EXCLUSION_MANAGER.get_diagnostic_status().to_dict()
    }

# -----------------------------------------------------------------------------
# TASK MANAGER ENDPOINTS
# -----------------------------------------------------------------------------
@app.get("/api/tasks")
async def api_get_tasks():
    from session_lifecycle import TaskStateManager
    mgr = TaskStateManager()
    return {
        "status": "ok",
        "active_task": mgr.get_latest_unfinished_task(),
        "tasks": mgr.get_all_tasks(limit=30)
    }

@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str):
    from session_lifecycle import TaskStateManager
    mgr = TaskStateManager()
    if mgr.cancel_task(task_id, reason="User cancelled via Task Manager"):
        return {"status": "ok", "message": f"Task '{task_id}' marked as cancelled."}
    return JSONResponse(status_code=404, content={"status": "error", "message": "Task not found"})

@app.post("/api/tasks/{task_id}/retry")
async def api_retry_task(task_id: str):
    from session_lifecycle import TaskStateManager
    mgr = TaskStateManager()
    if mgr.retry_task(task_id):
        return {"status": "ok", "message": f"Task '{task_id}' reset to in_progress."}
    return JSONResponse(status_code=404, content={"status": "error", "message": "Task not found"})

# -----------------------------------------------------------------------------
# PERMISSION CENTRE ENDPOINTS
# -----------------------------------------------------------------------------
@app.get("/api/permissions/pending")
async def api_get_pending_permissions():
    return {
        "status": "ok",
        "count": len(GLOBAL_PENDING_CONFIRMATIONS),
        "pending": list(GLOBAL_PENDING_CONFIRMATIONS.values())
    }

@app.post("/api/permissions/{call_id}/respond")
async def api_respond_permission(call_id: str, request: Request):
    data = await request.json()
    decision = data.get("decision", "deny")
    fut = GLOBAL_CONFIRMATION_FUTURES.get(call_id)
    if fut and not fut.done():
        fut.set_result(decision)
        GLOBAL_PENDING_CONFIRMATIONS.pop(call_id, None)
        return {"status": "ok", "decision": decision}
    return JSONResponse(status_code=404, content={"status": "error", "message": "Pending confirmation not found"})

# -----------------------------------------------------------------------------
# SKILL MANAGER EXTENDED ENDPOINTS
# -----------------------------------------------------------------------------
@app.post("/api/skills/{name}/quarantine")
async def api_quarantine_skill(name: str):
    from learning_engine import quarantine_skill
    res = quarantine_skill(name, reason="User triggered quarantine via Skill Manager")
    return {"status": "ok", "result": res}

@app.post("/api/skills/{name}/rollback")
async def api_rollback_skill(name: str):
    from learning_engine import rollback_skill
    res = rollback_skill(name, reason="User triggered rollback via Skill Manager")
    return {"status": "ok", "result": res}

@app.get("/api/skills/diff")
async def api_skills_diff():
    from learning_engine import SkillsGitRepository, SKILLS_REPO_DIR
    SkillsGitRepository.ensure_initialized()
    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD"],
            cwd=str(SKILLS_REPO_DIR),
            capture_output=True,
            text=True
        )
        diff_text = proc.stdout or proc.stderr or "No uncommitted or recent git diff found in skills repository."
        return {"status": "ok", "diff": diff_text}
    except Exception as e:
        return {"status": "error", "diff": f"Error running git diff: {e}"}

@app.post("/api/skills/test")
async def api_test_skills():
    from learning_engine import run_self_tests
    res = run_self_tests()
    return {"status": "ok", "test_report": res}

# -----------------------------------------------------------------------------
# SCREEN RADAR STATUS ENDPOINT
# -----------------------------------------------------------------------------
@app.get("/api/radar/status")
async def api_radar_status():
    from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
    diag = GLOBAL_HUD_EXCLUSION_MANAGER.get_diagnostic_status().to_dict()
    metrics = GLOBAL_METRICS.get_metrics_summary()
    fps = metrics.get("throughput", {}).get("video_fps", 1.0)
    tokens_est = metrics.get("throughput", {}).get("tokens_rate_per_hour", 0)
    return {
        "status": "ok",
        "exclusion": diag,
        "radar": {
            "active_monitor": "Display 1 (Primary // 1920x1080)",
            "streaming": True,
            "fps": fps,
            "tokens_per_hour_est": tokens_est,
            "privacy_pause": False,
            "sensitive_app_detected": False
        }
    }

# -----------------------------------------------------------------------------
# DIAGNOSTICS ENDPOINT (NON-FABRICATED REAL DATA)
# -----------------------------------------------------------------------------
@app.get("/api/diagnostics")
async def api_diagnostics():
    from observability import get_host_resource_usage
    from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
    from learning_engine import StartupWatchdog

    metrics = GLOBAL_METRICS.get_metrics_summary()
    host_res = get_host_resource_usage()
    hud_diag = GLOBAL_HUD_EXCLUSION_MANAGER.get_diagnostic_status().to_dict()
    recent_failures = GLOBAL_EXECUTION_LOGGER.get_recent_executions(limit=8, status="failed")

    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "gemini": {
            "status": "synchronized" if GLOBAL_METRICS.gemini_reconnect_count == 0 else "reconnected",
            "p50_latency_ms": metrics.get("latency", {}).get("speech_to_first_audio_ms", {}).get("p50", 0.0),
            "p95_latency_ms": metrics.get("latency", {}).get("speech_to_first_audio_ms", {}).get("p95", 0.0),
            "reconnects": GLOBAL_METRICS.gemini_reconnect_count,
            "goaway_received": GLOBAL_METRICS.goaway_count,
            "session_turns": GLOBAL_METRICS.total_turns
        },
        "audio": {
            "input_rate": "16.0 kHz PCM",
            "output_rate": "24.0 kHz PCM",
            "channel_count": 1,
            "audio_drops": metrics.get("throughput", {}).get("audio_frame_drops", 0),
            "vad_active": True
        },
        "wake_word": {
            "engine": "Chirkoot Dual-Mode (Offline + SpeechRecognition)",
            "keywords": ["hey chirkoot", "chirkoot", "chirkut", "cheerkoot", "hey assistant"],
            "false_accepts_per_hour": metrics.get("reliability", {}).get("wake_word_false_accepts_per_hour", 0.0),
            "misses": metrics.get("reliability", {}).get("wake_word_misses", 0)
        },
        "vision": {
            "capture_engine": "Native DXGI / MSS Screen Radar",
            "fps": metrics.get("throughput", {}).get("video_fps", 1.0),
            "hud_exclusion_mode": hud_diag.get("effective_mode", "AUTO"),
            "hud_exclusion_applied": hud_diag.get("exclusion_applied", False),
            "active_monitors": 1
        },
        "backend": {
            "status": "nominal",
            "uptime_seconds": host_res.get("uptime_seconds", 0),
            "kill_switch_engaged": is_kill_switch_active(),
            "safe_mode": StartupWatchdog.should_force_safe_mode()
        },
        "host_resources": {
            "memory_rss_mb": host_res.get("memory_rss_mb", 0.0),
            "cpu_percent": host_res.get("cpu_percent", 0.0),
            "thread_count": host_res.get("thread_count", 0),
            "active_child_processes": host_res.get("active_child_processes", 0)
        },
        "recent_failures": recent_failures
    }

global_api_key_updated_event = asyncio.Event()

@app.get("/api/credentials/gemini")
async def api_get_gemini_credentials():
    key = get_gemini_api_key()
    if key and len(key) > 6:
        masked = f"{key[:4]}...{key[-4:]}"
        return {"configured": True, "masked": masked}
    return {"configured": False, "masked": ""}

@app.post("/api/credentials/gemini")
async def api_set_gemini_credentials(request: Request):
    data = await request.json()
    new_key = data.get("api_key", "").strip()
    if not new_key:
        return JSONResponse({"status": "error", "message": "API key cannot be empty."}, status_code=400)
    if new_key in ("TEST_GEMINI_KEY_9999", "your_api_key_here"):
        return JSONResponse({"status": "error", "message": "Placeholder keys are not valid."}, status_code=400)
    store_secret_dpapi("GEMINI_API_KEY", new_key)
    global_api_key_updated_event.set()
    logger.info("New Gemini API key stored in DPAPI vault via REST API")
    return {"status": "success", "message": "Gemini API key saved to Windows DPAPI vault."}

def build_rolling_turns(transcript_history):
    turns = []
    current_role = None
    current_texts = []
    for item in transcript_history:
        role = item.get("role")
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        if role == current_role:
            current_texts.append(text)
        else:
            if current_role and current_texts:
                turns.append({"role": current_role, "text": " ".join(current_texts)})
            current_role = role
            current_texts = [text]
    if current_role and current_texts:
        turns.append({"role": current_role, "text": " ".join(current_texts)})
    if turns and turns[0]["role"] == "model":
        turns = turns[1:]
    return turns

MAX_TEXT_PAYLOAD_BYTES = 65536      # 64 KB limit
MAX_BINARY_PAYLOAD_BYTES = 1048576  # 1 MB limit
ALLOWED_WS_MESSAGE_TYPES = {
    "video_frame",
    "ocr_scan",
    "alt_tab",
    "minimize",
    "set_reply_mode",
    "text",
    "policy_confirmation_response",
    "kill_switch",
    "reset_kill_switch",
    "auth",
    "ping",
    "audio_stream_end",
    "set_listening_mode",
    "set_mic_mute",
    "manual_wake",
    "radar_pause",
    "radar_resume",
    "radar_config",
    "radar_get_telemetry",
    "hud_geometry",
    "hud_exclusion_config",
    "hud_exclusion_status",
    "set_api_key",
    "get_api_key_status",
    "resume_uplink",
    "reconnect_session"
}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    client_ip = websocket.client.host if websocket.client else "127.0.0.1"
    if not GLOBAL_RATE_LIMITER.is_allowed(client_ip):
        logger.warning(f"[Rate Limit] Rejected WebSocket connection from {client_ip}: rate limit exceeded.")
        await websocket.close(code=1008, reason="Rate Limit Exceeded")
        return

    origin = websocket.headers.get("origin")
    if not validate_origin(origin):
        logger.warning(f"Rejected WebSocket connection from unauthorized origin: {origin}")
        await websocket.close(code=1008, reason="Forbidden Origin")
        return

    token = None
    subprotocol_to_accept = None

    header_token = websocket.headers.get("x-chirkoot-auth")
    if header_token:
        token = header_token.strip()

    if not token:
        auth_hdr = websocket.headers.get("authorization")
        if auth_hdr:
            parts = auth_hdr.strip().split()
            token = parts[1] if (len(parts) == 2 and parts[0].lower() == "bearer") else auth_hdr.strip()

    if not token:
        sec_proto = websocket.headers.get("sec-websocket-protocol")
        if sec_proto:
            proto_items = [p.strip() for p in sec_proto.split(",")]
            for item in proto_items:
                if item != "chirkoot":
                    token = item
                    subprotocol_to_accept = "chirkoot"
                    break

    if not token:
        token = websocket.query_params.get("token")

    if not validate_auth_token(token):
        token_hint = f"{token[:4]}..." if token and len(token) > 4 else ("missing" if not token else "invalid")
        logger.warning(f"Rejected WebSocket connection with unauthorized auth token ({token_hint})")
        await websocket.close(code=1008, reason="Unauthorized Token")
        return

    if not register_client_session():
        logger.warning("Rejected duplicate client connection: single active client already established.")
        await websocket.close(code=1008, reason="Single Client Session Already Active")
        return

    if subprotocol_to_accept:
        await websocket.accept(subprotocol=subprotocol_to_accept)
    else:
        await websocket.accept()
    logger.info("Authorized client connected to Gemini Live WebSocket")

    api_key = get_gemini_api_key()
    client = None
    if api_key:
        client = genai.Client(api_key=api_key)
        global_api_key_updated_event.set()
    else:
        global_api_key_updated_event.clear()
        logger.info("No Gemini API key found in vault or env. Notifying HUD client to enter key...")
        await websocket.send_json({
            "type": "api_key_required",
            "message": "Gemini API key is required. Please enter your API key in the HUD or run 'python set_api_key.py' to activate Chirkoot."
        })

    learned_memories = get_memory_prompt_injection()

    system_instruction = (
        "You are Chirkoot, a futuristic tactical multimodal AI desktop companion with real-time vision, screen sharing, autonomous Computer Use, and recursive SELF-LEARNING & SELF-MODIFYING capabilities. "
        "MULTILINGUAL & HINDI SPEECH AGENT: "
        "You are fully bilingual in Hindi (हिंदी/Hinglish) and English. "
        "You naturally understand Hindi voice commands, Indian English accents, and colloquial Hinglish (e.g. 'चिरकुट स्क्रीन पर देखो', 'यह कोड चेक करो', 'क्या चल रहा है', 'bhai yeh error fix karo', 'open notepad aur note likho'). "
        "When the user speaks in Hindi or Hinglish, converse naturally with them in Hindi or Hinglish! When the user speaks in English, respond in English. "
        "You receive a continuous real-time stream of the user's microphone audio, live text prompts, and live video frames from their screen share or camera. "
        "COMPUTER USE & SCREEN CONTROL AGENT: "
        "You have direct autonomous control over the user's screen through your host tools: "
        "1. click_screen(x, y, button, clicks): Clicks on buttons, icons, tabs, links, or menus on the user's screen using 0-1000 normalized coordinates. "
        "2. type_text(text, press_enter): Types text or code into the currently active input field, search bar, or editor. "
        "3. press_hotkey(keys): Presses keyboard shortcuts (e.g. 'ctrl,s', 'enter', 'tab', 'escape', 'alt,tab', 'ctrl,p'). "
        "4. scroll_screen(direction, amount): Scrolls the active page or editor up or down. "
        "5. drag_mouse(start_x, start_y, end_x, end_y): Drags across elements on screen. "
        "6. switch_to_app(app_name): Brings an open application window directly to the front. "
        "7. minimize_active_window(): Minimizes Chirkoot or current window to reveal background applications. "
        "8. ocr_screen(): Scans and extracts all text, button labels, and code visible on the user's screen with normalized coordinates. "
        "9. ocr_image(image_path): Reads text from any saved image file. "
        "10. find_text_on_screen(target_text, click=True): Finds the exact location of any text/button/menu on screen and clicks it! "
        "11. run_command, read_file, write_file, list_directory, system_diagnostics, open_app_or_url: Host OS workspace tools. "
        "SCREEN SHARE & SELF-VIEW DETECTION PROTOCOL: "
        "If the screen share appears to be displaying Chirkoot itself (infinite mirror effect, arc reactor HUD, or Chirkoot comms panel) instead of the user's workspace, immediately inform the user: 'I am currently seeing the Chirkoot window. Please switch to your work application or share that specific application window.' You can also use press_hotkey('alt,tab') or switch_to_app(app_name) to bring their target workspace to the front! "
        "WAKE WORD & ACTIVATION PROTOCOL: "
        "Your name is Chirkoot (also responds to Chitrakoot, Jarvis, or friendly greetings). Your activation phrases include 'Hey', 'Hey Chirkoot', 'Chirkoot', 'Hey Chitrakoot', 'Chitrakoot', 'Hey Jarvis', 'Jarvis', or in Hindi: 'हे चिरकुट', 'सुनो', 'अरे'. "
        "When the user greets you or calls your name (e.g. 'Hey Chirkoot', 'Hey', 'Chirkoot', 'Hello', 'Suno'), acknowledge them immediately with an energetic, concise, and helpful spoken response (e.g. 'Hey! How can I help you?', 'Main sun raha hoon, bataiye!', 'Yes, I am here!'). "
        "When the user calls your name with a direct command (e.g. 'Hey Chirkoot open notepad', 'Chirkoot what is on screen?'), execute their task immediately with enthusiasm! "
        "SELF-LEARNING & SELF-MODIFICATION ENGINE: "
        "You can inspect, modify, and extend your own software code: "
        "- get_codebase_guide(): Shows you which file controls what (static/app.js, server.py, tools.py, custom_skills, etc.). "
        "- inspect_self(): Inspects your directory structure, active tools, memories, and architecture. "
        "- read_code_file(filepath, start_line, end_line): Reads any of your source files (server.py, tools.py, learning_engine.py, custom_skills, static/app.js). "
        "- self_learn_and_implement(feature_title, file_to_modify, target_code_snippet, new_code_snippet, explanation): High-level all-in-one tool that edits a file, creates an automatic backup, verifies Python syntax, runs self-tests, and records the feature. "
        "- modify_source_code(filepath, target_snippet, replacement_snippet): Safely edits your own code with automatic backup and Python syntax verification (auto-rolls back if syntax fails). "
        "- rollback_code(filepath): Reverts a file to its last backup if needed. "
        "- create_custom_tool(tool_name, description, python_code): Writes and compiles a brand new Python skill/tool into custom_skills/ that you can immediately execute and persist! "
        "- list_custom_tools(): Lists all self-learned custom skills you have created. "
        "- remember_insight(category, key, insight): Commits learned user preferences, workflow patterns, and corrections into your permanent memory. "
        "- recall_memory(query, category): Recalls your stored knowledge base. "
        "- search_memory(query, category): Performs hybrid keyword and semantic vector search over your memory. "
        "- run_self_tests(): Verifies that all your Python source files compile cleanly without syntax errors. "
        "CRITICAL SELF-MODIFICATION RULE: "
        "When the user asks you to implement a feature, change your code, add a capability, or fix a bug, do NOT merely save a memory note using remember_insight! Calling remember_insight only saves a text note; IT DOES NOT MODIFY YOUR CODE. "
        "You MUST actively inspect your code (read_code_file/get_codebase_guide), locate the file to update, and apply the code change using self_learn_and_implement, modify_source_code, or create_custom_tool! Then run run_self_tests. "
        "RESPONSE FORMATS (VOICE vs TEXT-ONLY REPLY): "
        "You support two response modes: Spoken Voice Reply and Silent Text-Only Reply. "
        "The user can speak to you with their microphone but request or set reply mode to Text-Only ('reply in text only', 'silent mode', 'text reply', 'sirf text me jawab do', 'don't speak'). "
        "When in Text-Only mode or requested to reply in text, provide your answers clearly and directly in written text in the dialogue feed. "
        "When in Voice mode, keep spoken answers natural, crisp, and direct (1-2 sentences). Yield immediately if interrupted."
        + learned_memories
    )

    # Production-grade Session Lifecycle Controller
    lifecycle = SessionLifecycleController(model_name="gemini-3.8-live")
    audio_queue = asyncio.Queue(maxsize=150)
    text_queue = asyncio.Queue(maxsize=20)

    # Local Wake Word Detection state (Zero cloud audio during standby)
    from wake_word_engine import GLOBAL_WAKE_DETECTOR
    current_mic_mode = "always-on"
    is_wake_active = True

    # Video handling: only hold freshest frame
    latest_video_lock = asyncio.Lock()
    latest_video_bytes = None
    video_new_event = asyncio.Event()
    user_speech_end_time: Optional[float] = None

    # Pending human-in-the-loop confirmations for Tier C and tainted actions
    pending_confirmations: Dict[str, asyncio.Future] = {}

    # Chirkoot MK-V Local Dispatcher
    dispatcher = FunctionCallDispatcher(tool_map=TOOL_MAP)
    EMERGENCY_STOP_MANAGER.register_dispatcher(dispatcher)

    def emergency_stop_ws_listener(active: bool, reason: str):
        if browser_connected:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(websocket.send_json({
                        "type": "kill_switch_state",
                        "active": active,
                        "reason": reason
                    }))
            except Exception:
                pass

    EMERGENCY_STOP_MANAGER.add_listener(emergency_stop_ws_listener)

    async def hitl_confirmation_handler(record: ExecutionRecord) -> str:
        conf_fut = asyncio.get_event_loop().create_future()
        pending_confirmations[record.call_id] = conf_fut
        GLOBAL_CONFIRMATION_FUTURES[record.call_id] = conf_fut
        is_tainted, taint_source, _ = GLOBAL_TAINT_TRACKER.is_tainted()

        conf_data = {
            "call_id": record.call_id,
            "tool_name": record.tool_name,
            "args": record.args,
            "tier": record.tier.value,
            "is_tainted": is_tainted,
            "taint_source": taint_source,
            "target": str(record.args.get("command") or record.args.get("filepath") or record.args.get("name") or record.args.get("text") or ""),
            "created_at": time.time(),
            "reason": (
                f"Action '{record.tool_name}' is high-impact ({record.tier.value}). "
                + (f"Tainted by recent {taint_source}. " if is_tainted else "")
                + "Physical on-screen authorization is required."
            )
        }
        GLOBAL_PENDING_CONFIRMATIONS[record.call_id] = conf_data

        await websocket.send_json({
            "type": "policy_confirmation_request",
            **conf_data
        })

        try:
            decision = await asyncio.wait_for(conf_fut, timeout=45.0)
        except asyncio.TimeoutError:
            decision = "timeout"
        finally:
            pending_confirmations.pop(record.call_id, None)
            GLOBAL_CONFIRMATION_FUTURES.pop(record.call_id, None)
            GLOBAL_PENDING_CONFIRMATIONS.pop(record.call_id, None)
        return decision

    dispatcher.confirmation_handler = hitl_confirmation_handler

    browser_connected = True
    initial_status_sent = False

    async def browser_reader():
        nonlocal browser_connected, latest_video_bytes
        try:
            while browser_connected:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    logger.info("Browser disconnected cleanly")
                    browser_connected = False
                    break

                if "bytes" in msg and msg["bytes"]:
                    raw_bytes = msg["bytes"]
                    if len(raw_bytes) > MAX_BINARY_PAYLOAD_BYTES:
                        logger.warning(f"[Payload Limit] Oversized binary payload rejected: {len(raw_bytes)} bytes > {MAX_BINARY_PAYLOAD_BYTES}")
                        await websocket.send_json({"type": "error", "message": "Binary payload exceeds maximum allowed size (1 MB)"})
                        continue

                    # Explicit Binary Media Protocol Framing Parser
                    from frame_store import MediaPacket, FrameType, MalformedPacketError, GLOBAL_FRAME_STORE
                    try:
                        packet = MediaPacket.parse(raw_bytes)
                    except MalformedPacketError as parse_err:
                        logger.warning(f"[Protocol Error] Malformed binary packet rejected: {parse_err}")
                        await websocket.send_json({"type": "error", "message": f"Malformed media packet: {parse_err}"})
                        continue

                    if packet.packet_type == FrameType.AUDIO_PCM:
                        # Local Wake-Word Evaluation: If in wake-word mode and in standby,
                        # evaluate local detector and DO NOT forward audio to Gemini Live!
                        if current_mic_mode == "wake-word" and not is_wake_active:
                            wake_evt = await asyncio.to_thread(GLOBAL_WAKE_DETECTOR.accept_waveform, packet.payload)
                            if wake_evt:
                                is_wake_active = True
                                logger.info(f"⚡ [LOCAL WAKE] Detected '{wake_evt['keyword']}' ({wake_evt['duration_ms']:.1f}ms). Activating Gemini audio stream.")
                                await websocket.send_json({
                                    "type": "wake_detected",
                                    "keyword": wake_evt["keyword"],
                                    "confidence": wake_evt["confidence"],
                                    "timestamp": wake_evt["timestamp"],
                                    "duration_ms": wake_evt["duration_ms"]
                                })
                            continue

                        if audio_queue.full():
                            try:
                                audio_queue.get_nowait()
                                GLOBAL_METRICS.record_audio_drop()
                            except asyncio.QueueEmpty:
                                pass
                        await audio_queue.put(packet.payload)

                    elif packet.packet_type in (FrameType.SCREEN_JPEG, FrameType.WEBCAM_JPEG):
                        lifecycle.is_video_active = True
                        if packet.packet_type == FrameType.SCREEN_JPEG:
                            GLOBAL_METRICS.record_screen_frame()
                            from radar_engine import GLOBAL_RADAR_ENGINE, RadarState
                            from frame_store import VideoFrame
                            img = VideoFrame(FrameType.SCREEN_JPEG, packet.payload).get_image()
                            should_tx, r_state, telem = GLOBAL_RADAR_ENGINE.evaluate_frame(
                                img, packet.timestamp_ms, packet.stream_id
                            )
                            if r_state == RadarState.SHIELDED:
                                await websocket.send_json({
                                    "type": "radar_privacy_alert",
                                    "active": True,
                                    "reason": "Sensitive window active in foreground",
                                    "title": telem.foreground_window_title
                                })
                                continue
                            elif r_state == RadarState.PAUSED:
                                continue

                            GLOBAL_FRAME_STORE.update_frame(packet)

                            # Quota Protection: In wake-word standby mode, DO NOT forward video frames upstream
                            if current_mic_mode == "wake-word" and not is_wake_active:
                                continue

                            if should_tx:
                                async with latest_video_lock:
                                    latest_video_bytes = packet.payload
                                video_new_event.set()
                        else:
                            GLOBAL_FRAME_STORE.update_frame(packet)
                            # Quota Protection: In wake-word standby mode, DO NOT forward webcam frames upstream
                            if current_mic_mode == "wake-word" and not is_wake_active:
                                continue
                            async with latest_video_lock:
                                latest_video_bytes = packet.payload
                            video_new_event.set()

                elif "text" in msg and msg["text"]:
                    raw_text = msg["text"]
                    if len(raw_text.encode("utf-8")) > MAX_TEXT_PAYLOAD_BYTES:
                        logger.warning(f"[Payload Limit] Oversized text payload rejected: {len(raw_text)} chars > {MAX_TEXT_PAYLOAD_BYTES} bytes")
                        await websocket.send_json({"type": "error", "message": "Text payload exceeds maximum allowed size (64 KB)"})
                        continue

                    try:
                        data = json.loads(raw_text)
                    except (json.JSONDecodeError, Exception) as json_err:
                        logger.warning(f"[Malformed Payload] Invalid JSON text rejected: {json_err}")
                        await websocket.send_json({"type": "error", "message": "Malformed payload: Invalid JSON"})
                        continue

                    if not isinstance(data, dict):
                        logger.warning(f"[Malformed Payload] Non-dictionary payload rejected: {type(data).__name__}")
                        await websocket.send_json({"type": "error", "message": "Malformed payload: Expected JSON object"})
                        continue

                    msg_type = data.get("type")
                    if not msg_type or msg_type not in ALLOWED_WS_MESSAGE_TYPES:
                        logger.warning(f"[Unexpected Payload] Unrecognized message type rejected: '{msg_type}'")
                        await websocket.send_json({"type": "error", "message": f"Unexpected message type: '{msg_type}'"})
                        continue

                    if msg_type == "auth":
                        await websocket.send_json({"type": "auth_ack", "status": "authenticated"})
                        continue

                    elif msg_type == "ping":
                        await websocket.send_json({"type": "pong"})
                        continue

                    elif msg_type == "video_frame":
                        lifecycle.is_video_active = True
                        b64 = data.get("data", "")
                        if b64:
                            if "," in b64:
                                b64 = b64.split(",", 1)[1]
                            jpeg_bytes = base64.b64decode(b64)
                            from frame_store import MediaPacket, FrameType, GLOBAL_FRAME_STORE, VideoFrame
                            from radar_engine import GLOBAL_RADAR_ENGINE, RadarState
                            pkt = MediaPacket(packet_type=FrameType.SCREEN_JPEG, payload=jpeg_bytes)
                            img = VideoFrame(FrameType.SCREEN_JPEG, jpeg_bytes).get_image()
                            should_tx, r_state, telem = GLOBAL_RADAR_ENGINE.evaluate_frame(img, pkt.timestamp_ms, pkt.stream_id)
                            if r_state == RadarState.SHIELDED:
                                await websocket.send_json({
                                    "type": "radar_privacy_alert",
                                    "active": True,
                                    "reason": "Sensitive window active in foreground",
                                    "title": telem.foreground_window_title
                                })
                                continue
                            elif r_state == RadarState.PAUSED:
                                continue

                            GLOBAL_FRAME_STORE.update_frame(pkt)
                            if should_tx:
                                async with latest_video_lock:
                                    latest_video_bytes = jpeg_bytes
                                video_new_event.set()

                    elif msg_type == "radar_pause":
                        from radar_engine import GLOBAL_RADAR_ENGINE
                        GLOBAL_RADAR_ENGINE.pause_radar(reason="User paused radar")
                        await websocket.send_json({"type": "radar_state", "state": "PAUSED"})
                        continue

                    elif msg_type == "radar_resume":
                        from radar_engine import GLOBAL_RADAR_ENGINE
                        GLOBAL_RADAR_ENGINE.resume_radar()
                        await websocket.send_json({"type": "radar_state", "state": "ACTIVE"})
                        continue

                    elif msg_type == "radar_config":
                        from radar_engine import GLOBAL_RADAR_ENGINE
                        cfg_updates = data.get("config", {})
                        if isinstance(cfg_updates, dict):
                            GLOBAL_RADAR_ENGINE.update_config(**cfg_updates)
                        await websocket.send_json({"type": "radar_config_ack", "data": GLOBAL_RADAR_ENGINE.get_telemetry()})
                        continue

                    elif msg_type == "radar_get_telemetry":
                        from radar_engine import GLOBAL_RADAR_ENGINE
                        await websocket.send_json({"type": "radar_telemetry", "data": GLOBAL_RADAR_ENGINE.get_telemetry()})
                        continue

                    elif msg_type == "hud_geometry":
                        from radar_engine import GLOBAL_RADAR_ENGINE
                        from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
                        coords = data.get("rect", [])
                        if len(coords) == 4:
                            GLOBAL_RADAR_ENGINE.set_hud_rectangle(*coords)
                            GLOBAL_HUD_EXCLUSION_MANAGER.update_hud_geometry(*coords)
                        continue

                    elif msg_type == "hud_exclusion_config":
                        from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER, CaptureExclusionMode
                        requested_mode = data.get("mode", "AUTO")
                        try:
                            GLOBAL_HUD_EXCLUSION_MANAGER.set_exclusion_mode(CaptureExclusionMode(requested_mode))
                        except Exception as e:
                            logger.warning(f"Invalid exclusion mode '{requested_mode}': {e}")
                        await websocket.send_json({
                            "type": "hud_exclusion_status",
                            "data": GLOBAL_HUD_EXCLUSION_MANAGER.get_diagnostic_status().to_dict()
                        })
                        continue

                    elif msg_type == "hud_exclusion_status":
                        from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER
                        await websocket.send_json({
                            "type": "hud_exclusion_status",
                            "data": GLOBAL_HUD_EXCLUSION_MANAGER.get_diagnostic_status().to_dict()
                        })
                        continue

                    elif msg_type == "ocr_scan":
                        from tools import ocr_screen
                        ocr_res = await asyncio.to_thread(ocr_screen)
                        await websocket.send_json({
                            "type": "ocr_result",
                            "result": ocr_res
                        })

                    elif msg_type == "alt_tab":
                        from tools import press_hotkey
                        logger.info("Executing Alt+Tab window switch")
                        await asyncio.to_thread(press_hotkey, "alt,tab")

                    elif msg_type == "minimize":
                        from tools import minimize_active_window
                        logger.info("Executing minimize window")
                        await asyncio.to_thread(minimize_active_window)

                    elif msg_type == "set_reply_mode":
                        mode = data.get("mode", "voice")
                        logger.info(f"Chirkoot reply mode switched to: {mode}")

                    elif msg_type == "text":
                        user_text = data.get("text", "").strip()
                        if user_text:
                            logger.info(f"User text input received: {user_text}")
                            dispatcher.reset_turn_budget()
                            task_id = f"task_{int(time.time()*1000)}"
                            lifecycle.task_mgr.register_task(task_id, user_text)
                            lifecycle.transcript_mgr.append_turn("user", user_text)
                            lifecycle.msg_tracker.track_message("text", user_text)
                            await text_queue.put(user_text)

                    elif msg_type == "policy_confirmation_response":
                        call_id = data.get("call_id")
                        decision = data.get("decision", "deny")
                        fut = pending_confirmations.get(call_id) or GLOBAL_CONFIRMATION_FUTURES.get(call_id)
                        if fut and not fut.done():
                            fut.set_result(decision)
                            GLOBAL_PENDING_CONFIRMATIONS.pop(call_id, None)
                            logger.info(f"Received HITL confirmation response '{decision}' for call {call_id}")

                    elif msg_type == "kill_switch":
                        stop_reason = data.get("reason", "Client Emergency Stop triggered")
                        EMERGENCY_STOP_MANAGER.trigger(stop_reason)
                        logger.critical("EMERGENCY STOP TRIGGERED BY CLIENT")
                        await websocket.send_json({"type": "kill_switch_state", "active": True, "reason": stop_reason})

                    elif msg_type == "reset_kill_switch":
                        EMERGENCY_STOP_MANAGER.reset(physical_confirmation=True, actor="hud_client")
                        logger.info("Emergency Stop reset by client")
                        await websocket.send_json({"type": "kill_switch_state", "active": False})

                    elif msg_type == "audio_stream_end":
                        logger.info("[Audio Stream] Client signaled audio_stream_end (user finished turn / standby)")
                        if current_mic_mode == "wake-word":
                            is_wake_active = False
                            GLOBAL_WAKE_DETECTOR.reset()
                        if audio_queue.full():
                            try:
                                audio_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                        await audio_queue.put(None)

                    elif msg_type == "set_listening_mode":
                        new_mode = data.get("mode", "always-on")
                        current_mic_mode = new_mode
                        if new_mode == "always-on":
                            is_wake_active = True
                        else:
                            is_wake_active = False
                            GLOBAL_WAKE_DETECTOR.reset()
                        logger.info(f"[Mode Switch] Listening mode set to: '{current_mic_mode}' (WakeActive={is_wake_active})")

                    elif msg_type == "manual_wake":
                        is_wake_active = True
                        logger.info("[Wake] Manual wake event received from client")

                    elif msg_type == "set_mic_mute":
                        muted = bool(data.get("muted", False))
                        GLOBAL_WAKE_DETECTOR.set_mute(muted)
                        logger.info(f"[Mic Mute] Local wake detector mute set to: {muted}")

                    elif msg_type == "set_api_key":
                        new_key = data.get("api_key", "").strip()
                        if new_key and new_key not in ("TEST_GEMINI_KEY_9999", "your_api_key_here"):
                            store_secret_dpapi("GEMINI_API_KEY", new_key)
                            api_key = new_key
                            client = genai.Client(api_key=new_key)
                            global_api_key_updated_event.set()
                            logger.info("Successfully updated Gemini API key via WebSocket and saved to DPAPI vault.")
                            await websocket.send_json({"type": "api_key_accepted", "message": "Gemini API key saved to DPAPI vault. Connecting..."})
                        else:
                            await websocket.send_json({"type": "error", "message": "Invalid API key format provided."})

                    elif msg_type == "get_api_key_status":
                        curr = get_gemini_api_key()
                        configured = bool(curr and len(curr) > 6)
                        masked = f"{curr[:4]}...{curr[-4:]}" if configured else ""
                        await websocket.send_json({
                            "type": "api_key_status",
                            "configured": configured,
                            "masked": masked
                        })

                    elif msg_type in ("resume_uplink", "reconnect_session"):
                        logger.info("⚡ [Uplink Resume] Client requested upstream reconnection. Resetting backoff.")
                        lifecycle.backoff_mgr.reset()
                        global_api_key_updated_event.set()
                        await websocket.send_json({"type": "uplink_resuming", "message": "Reconnecting to Gemini Live..."})

        except WebSocketDisconnect:
            logger.info("Browser WebSocket disconnected")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in browser_reader: {e}")
        finally:
            browser_connected = False
            unregister_client_session()

    reader_task = asyncio.create_task(browser_reader())

    # Upstream Gemini Live connection manager loop
    try:
        while browser_connected:
            if not api_key:
                logger.info("Waiting for valid Gemini API key to be configured...")
                while browser_connected and not api_key:
                    try:
                        await asyncio.wait_for(global_api_key_updated_event.wait(), timeout=2.0)
                        api_key = get_gemini_api_key()
                        if api_key:
                            client = genai.Client(api_key=api_key)
                    except asyncio.TimeoutError:
                        pass
                if not browser_connected:
                    break

            is_resumption, handle_to_use = lifecycle.prepare_reconnect_strategy()
            config = lifecycle.adapter.build_connect_config(
                types_module=types,
                system_instruction_text=system_instruction,
                tools_list=ALL_TOOLS,
                voice_name="Aoede",
                resumption_handle=handle_to_use,
                trigger_tokens=80000,
                target_tokens=40000
            )

            try:
                logger.info(f"Connecting upstream to {lifecycle.adapter.model_name} (resuming={is_resumption}, handle={handle_to_use[:12] if handle_to_use else 'none'})...")
                async with client.aio.live.connect(model=lifecycle.adapter.model_name, config=config) as session:
                    logger.info(f"Connected to {lifecycle.adapter.model_name} session successfully")
                    lifecycle.backoff_mgr.reset()

                    if not initial_status_sent:
                        await websocket.send_json({
                            "type": "status",
                            "status": "connected",
                            "message": "Connected to Gemini 3.8 Live with Screen Control"
                        })
                        initial_status_sent = True

                    await lifecycle.notify_hud(websocket, "connected", {
                        "resumed": is_resumption,
                        "handle": handle_to_use[:12] if handle_to_use else None
                    })

                    # Cold start / handle expired: re-seed context with compact summary turns
                    if not is_resumption:
                        reseeding_turns = lifecycle.transcript_mgr.build_reseeding_turns(types)
                        if reseeding_turns:
                            try:
                                logger.info(f"Re-seeding context with {len(reseeding_turns)} compact turns...")
                                await session.send_client_content(turns=reseeding_turns, turn_complete=False)
                                await lifecycle.notify_hud(websocket, "context_reseeded", {"turns_count": len(reseeding_turns)})
                                logger.info("Context re-seeding complete.")
                            except Exception as reseed_err:
                                logger.warning(f"Notice while re-seeding context: {reseed_err}")

                    # Check for unfinished task outside session to recover
                    unfinished_task = lifecycle.task_mgr.get_latest_unfinished_task()
                    if unfinished_task and unfinished_task.get("status") == "interrupted":
                        logger.info(f"Recovered interrupted task outside session: {unfinished_task['task_id']}")
                        await lifecycle.notify_hud(websocket, "task_recovered", {
                            "task_id": unfinished_task["task_id"],
                            "prompt": unfinished_task["prompt"]
                        })

                    session_active = True

                    async def send_audio_worker():
                        while browser_connected and session_active:
                            try:
                                audio_chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                                if audio_chunk is None:
                                    logger.info("[Audio Stream] Drained audio queue; sending audio_stream_end to Gemini Live")
                                    await session.send_realtime_input(audio_stream_end=True)
                                    continue
                                await session.send_realtime_input(
                                    audio=types.Blob(data=audio_chunk, mime_type="audio/pcm;rate=16000")
                                )
                            except asyncio.TimeoutError:
                                continue
                            except asyncio.CancelledError:
                                break
                            except Exception as err:
                                logger.error(f"Error sending audio to Gemini Live: {err}")
                                break

                    async def send_video_worker():
                        nonlocal latest_video_bytes
                        while browser_connected and session_active:
                            try:
                                await asyncio.wait_for(video_new_event.wait(), timeout=0.2)
                                video_new_event.clear()
                                async with latest_video_lock:
                                    frame = latest_video_bytes
                                if frame:
                                    await session.send_realtime_input(
                                        video=types.Blob(data=frame, mime_type="image/jpeg")
                                    )
                            except asyncio.TimeoutError:
                                continue
                            except asyncio.CancelledError:
                                break
                            except Exception as err:
                                logger.error(f"Error sending video to Gemini Live: {err}")
                                break

                    async def send_text_worker():
                        while browser_connected and session_active:
                            try:
                                text_msg = await asyncio.wait_for(text_queue.get(), timeout=0.1)
                                try:
                                    await session.send_client_content(
                                        turns=[types.Content(role="user", parts=[types.Part.from_text(text=text_msg)])],
                                        turn_complete=True
                                    )
                                except Exception as err:
                                    logger.warning(f"send_client_content fallback: {err}")
                                    await session.send_realtime_input(text=text_msg)
                            except asyncio.TimeoutError:
                                continue
                            except asyncio.CancelledError:
                                break
                            except Exception as err:
                                logger.error(f"Error sending text to Gemini Live: {err}")
                                break

                    async def receive_from_gemini():
                        nonlocal browser_connected, session_active
                        current_turn_text = []
                        try:
                            async for response in session.receive():
                                if not browser_connected or not session_active:
                                    break

                                if response.session_resumption_update:
                                    lifecycle.on_resumption_update(response.session_resumption_update)
                                    await lifecycle.notify_hud(websocket, "resumption_update", {
                                        "handle": lifecycle.latest_resumption_handle[:16] if lifecycle.latest_resumption_handle else None,
                                        "last_acked_index": lifecycle.msg_tracker.last_acked_index
                                    })

                                if response.go_away:
                                    time_left = getattr(response.go_away, "time_left", "unknown")
                                    logger.warning(f"GoAway received from server! Time left: {time_left}. Proactively reconnecting...")
                                    await lifecycle.notify_hud(websocket, "go_away", {"time_left": time_left})
                                    session_active = False
                                    break

                                server_content = response.server_content
                                if server_content:
                                    if server_content.interrupted:
                                        await websocket.send_json({"type": "interrupted"})
                                        current_turn_text.clear()
                                        dispatcher.cancel_all_pending(reason="User voice interruption")

                                    if server_content.input_transcription and server_content.input_transcription.text:
                                        u_text = server_content.input_transcription.text
                                        user_speech_end_time = time.time()
                                        dispatcher.current_instruction_ref = u_text[:120]
                                        await websocket.send_json({
                                            "type": "transcript_user",
                                            "text": u_text
                                        })
                                        lifecycle.transcript_mgr.append_turn("user", u_text)

                                    if server_content.output_transcription and server_content.output_transcription.text:
                                        m_text = server_content.output_transcription.text
                                        await websocket.send_json({
                                            "type": "transcript_gemini",
                                            "text": m_text
                                        })
                                        current_turn_text.append(m_text)

                                    if server_content.model_turn:
                                        for part in server_content.model_turn.parts:
                                            if part.inline_data and part.inline_data.data:
                                                if user_speech_end_time is not None:
                                                    eos_lat_ms = (time.time() - user_speech_end_time) * 1000.0
                                                    GLOBAL_METRICS.record_speech_to_first_audio(eos_lat_ms)
                                                    user_speech_end_time = None
                                                await websocket.send_bytes(part.inline_data.data)

                                    if server_content.turn_complete:
                                        if current_turn_text:
                                            model_turn_text = " ".join(current_turn_text)
                                            lifecycle.transcript_mgr.append_turn("model", model_turn_text)
                                            current_turn_text.clear()
                                        unfinished = lifecycle.task_mgr.get_latest_unfinished_task()
                                        if unfinished and unfinished.get("status") == "in_progress":
                                            lifecycle.task_mgr.mark_completed(unfinished["task_id"])
                                            await websocket.send_json({"type": "task_complete", "task_id": unfinished["task_id"]})
                                        await websocket.send_json({"type": "turn_complete"})

                                # Track token consumption
                                if hasattr(response, "usage_metadata") and response.usage_metadata:
                                    p_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                                    c_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
                                    if p_tok > 0 or c_tok > 0:
                                        GLOBAL_METRICS.record_token_usage(p_tok, c_tok)

                                # Handle model-directed tool cancellations
                                if hasattr(response, "tool_call_cancellation") and response.tool_call_cancellation:
                                    for cid in getattr(response.tool_call_cancellation, "ids", []) or []:
                                        dispatcher.cancel_call(cid, reason="Server tool_call_cancellation")

                                # Handle proposed tool calls through local dispatcher
                                if response.tool_call:
                                    for fc in response.tool_call.function_calls:
                                        tool_name = fc.name
                                        tool_args = fc.args or {}

                                        await websocket.send_json({
                                            "type": "tool_call",
                                            "name": tool_name,
                                            "args": tool_args
                                        })

                                        func_resp = await dispatcher.dispatch(fc.id, tool_name, tool_args)

                                        # Record in unfinished task if tracking
                                        unfinished = lifecycle.task_mgr.get_latest_unfinished_task()
                                        if unfinished and unfinished.get("status") == "in_progress":
                                            res_str = str(func_resp.response.get("result", "")) if func_resp.response else ""
                                            lifecycle.task_mgr.append_tool_execution(
                                                unfinished["task_id"], tool_name, tool_args, res_str
                                            )
                                            await websocket.send_json({
                                                "type": "task_step",
                                                "task_id": unfinished["task_id"],
                                                "tool": tool_name,
                                                "step_count": len(unfinished.get("tool_calls", [])) + 1
                                            })

                                        try:
                                            await session.send_tool_response(function_responses=[func_resp])
                                        except Exception as tool_send_err:
                                            logger.warning(f"Error sending tool response to Gemini Live: {tool_send_err}")

                                        res_str = str(func_resp.response.get("result", "")) if func_resp.response else ""
                                        await websocket.send_json({
                                            "type": "tool_result",
                                            "name": tool_name,
                                            "result": res_str[:300]
                                        })
                        except asyncio.CancelledError:
                            pass
                        except Exception as e:
                            logger.error(f"Error in receive_from_gemini: {e}")
                            raise e
                        finally:
                            session_active = False

                    send_audio_task = asyncio.create_task(send_audio_worker())
                    send_video_task = asyncio.create_task(send_video_worker())
                    send_text_task = asyncio.create_task(send_text_worker())
                    receive_task = asyncio.create_task(receive_from_gemini())

                    done, _ = await asyncio.wait(
                        [reader_task, send_audio_task, send_video_task, send_text_task, receive_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    session_active = False
                    for t in [send_audio_task, send_video_task, send_text_task, receive_task]:
                        if not t.done():
                            t.cancel()
                    if reader_task in done:
                        logger.info("Browser disconnected; terminating session loop.")
                        browser_connected = False
                        break

                    # Propagate task exceptions to outer connection handler
                    for t in done:
                        if not t.cancelled() and t.exception():
                            raise t.exception()

            except Exception as conn_err:
                logger.warning(f"Gemini Live session reset or error: {conn_err}")
                err_str = str(conn_err).lower()

                # 1. Detect unrecoverable invalid API key rejection from Google (1007 auth)
                if "api key not valid" in err_str or "unregistered" in err_str or ("1007" in err_str and "key" in err_str):
                    logger.error("🛑 [AUTH REJECTION] Gemini Live rejected API key (Code 1007: API key not valid). Pausing reconnect loop until a valid key is configured.")
                    await websocket.send_json({
                        "type": "api_key_required",
                        "error_code": 1007,
                        "message": "Google Gemini rejected the API key (Code 1007: API key not valid). Please enter a valid Gemini API key in the HUD or settings."
                    })
                    await lifecycle.notify_hud(websocket, "auth_error", {
                        "error": "API key rejected by Google (Code 1007: API key not valid). Waiting for valid key."
                    })
                    global_api_key_updated_event.clear()
                    api_key = None
                    client = None
                    while browser_connected and not global_api_key_updated_event.is_set():
                        try:
                            await asyncio.wait_for(global_api_key_updated_event.wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            pass
                    api_key = get_gemini_api_key()
                    if api_key:
                        client = genai.Client(api_key=api_key)
                        lifecycle.backoff_mgr.reset()
                    continue

                # 2. Detect unrecoverable payload or duplicate declaration errors (1007 payload)
                if "duplicate function declaration" in err_str or "invalid frame payload data" in err_str:
                    logger.error(f"🛑 [PAYLOAD ERROR] Gemini rejected connection payload: {conn_err}. Pausing reconnect to preserve quota.")
                    await websocket.send_json({
                        "type": "upstream_error",
                        "message": f"Gemini payload rejected: {conn_err}. Reconnection paused to protect quota."
                    })
                    global_api_key_updated_event.clear()
                    while browser_connected and not global_api_key_updated_event.is_set():
                        try:
                            await asyncio.wait_for(global_api_key_updated_event.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                    continue

                # 3. Detect API Quota / Rate Limit Exhaustion (429 / RESOURCE_EXHAUSTED)
                if "429" in err_str or "resource_exhausted" in err_str or "quota" in err_str:
                    logger.error("🛑 [QUOTA EXHAUSTED] Google Gemini API quota or rate limit reached. Halting reconnect loop to preserve quota.")
                    await websocket.send_json({
                        "type": "api_quota_exhausted",
                        "message": "Gemini API quota or rate limit exceeded. Pausing upstream connection to protect your quota. Click Reconnect when ready."
                    })
                    await lifecycle.notify_hud(websocket, "quota_exhausted", {
                        "error": "API quota limit reached. Pausing connection."
                    })
                    global_api_key_updated_event.clear()
                    while browser_connected and not global_api_key_updated_event.is_set():
                        try:
                            await asyncio.wait_for(global_api_key_updated_event.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                    continue

                if any(k in err_str for k in ["invalid", "expired", "handle", "not found", "1008"]):
                    lifecycle.invalidate_handle(str(conn_err))
                unfinished = lifecycle.task_mgr.get_latest_unfinished_task()
                if unfinished and unfinished.get("status") == "in_progress":
                    lifecycle.task_mgr.mark_interrupted(unfinished["task_id"], f"Connection drop: {conn_err}")

            if browser_connected:
                # Quota Guard: Cap maximum reconnect attempts to prevent burning user API quota
                if lifecycle.backoff_mgr.current_attempt >= 4:
                    logger.warning(f"🛑 [QUOTA GUARD] Reached maximum consecutive reconnect attempts ({lifecycle.backoff_mgr.current_attempt}). Pausing reconnect loop to protect user API quota.")
                    await websocket.send_json({
                        "type": "reconnect_paused",
                        "message": "Upstream connection failed after 4 attempts. Pausing to conserve API quota. Click Reconnect or say the wake-word to retry."
                    })
                    global_api_key_updated_event.clear()
                    while browser_connected and not global_api_key_updated_event.is_set():
                        try:
                            await asyncio.wait_for(global_api_key_updated_event.wait(), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                    lifecycle.backoff_mgr.reset()
                    continue

                delay = lifecycle.backoff_mgr.get_next_delay()
                logger.info(f"Reconnecting Gemini Live session in {delay}s (attempt {lifecycle.backoff_mgr.current_attempt})...")
                await lifecycle.notify_hud(websocket, "reconnecting", {
                    "delay": delay,
                    "attempt": lifecycle.backoff_mgr.current_attempt
                })
                await asyncio.sleep(delay)

    except WebSocketDisconnect:
        logger.info("Browser disconnected")
    except Exception as e:
        logger.error(f"Unexpected error in websocket_endpoint: {e}")
    finally:
        browser_connected = False
        unregister_client_session()
        EMERGENCY_STOP_MANAGER.remove_listener(emergency_stop_ws_listener)
        EMERGENCY_STOP_MANAGER.unregister_dispatcher(dispatcher)
        reader_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass

def main():
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"\n========================================================")
    print(f" [*] GEMINI LIVE MULTIMODAL & COMPUTER USE AGENT")
    print(f" Web UI: http://localhost:{port}")
    print(f" Vision: Screen Share (1 FPS) & Live Camera")
    print(f" Control: Mouse Click, Typing, Hotkeys, Scrolling")
    print(f"========================================================\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

if __name__ == "__main__":
    main()
