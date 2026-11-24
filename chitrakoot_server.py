"""
CHITRAKOOT // Dedicated Offline AI Interactive Chat Server
100% Offline Local GPU Neural Core powered by Ollama.
Independent, lightweight, air-gapped web & WebSocket server.
"""

import os
import sys
import json
import time
import socket
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

import offline_llm

logger = logging.getLogger("chitrakoot_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="Chitrakoot Neural Core", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# Static directory
STATIC_DIR = ROOT_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

class ChatRequest(BaseModel):
    message: str
    history: Optional[List[Dict[str, str]]] = None
    model: Optional[str] = None

class ModelRequest(BaseModel):
    model: str

@app.get("/", response_class=HTMLResponse)
async def serve_chitrakoot_ui():
    """Serves the dedicated futuristic interactive chat UI."""
    html_path = STATIC_DIR / "chitrakoot.html"
    if html_path.exists():
        res = FileResponse(str(html_path))
        res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        res.headers["Pragma"] = "no-cache"
        res.headers["Expires"] = "0"
        return res
    return HTMLResponse("<h1>Chitrakoot Neural Core UI loading...</h1>")

@app.get("/favicon.ico")
async def serve_favicon():
    ico_path = STATIC_DIR / "chitrakoot.ico"
    if not ico_path.exists():
        ico_path = STATIC_DIR / "chirkoot.ico"
    if ico_path.exists():
        return FileResponse(str(ico_path))
    raise HTTPException(status_code=404)

@app.get("/api/status")
async def get_system_status():
    """Returns local GPU specs, Ollama daemon status, and model catalogue."""
    is_live = offline_llm.is_ollama_running()
    if not is_live:
        is_live = offline_llm.ensure_ollama_running(2)

    gpu = offline_llm.get_gpu_info()
    catalog = offline_llm.get_model_catalog() if is_live else {"active_model": "llama3.2:latest", "models": []}

    return {
        "app": "CHITRAKOOT",
        "mode": "100% OFFLINE LOCAL GPU",
        "ollama_online": is_live,
        "gpu": gpu,
        "catalog": catalog,
        "active_model": catalog.get("active_model", "llama3.2:latest"),
        "status": "NOMINAL" if is_live else "WAITING_OLLAMA"
    }

@app.get("/api/models")
async def list_models():
    """Returns the full model catalog with installation state and active model."""
    return offline_llm.get_model_catalog()

@app.post("/api/models/select")
async def select_model_endpoint(req: ModelRequest):
    """Sets the active model for Chitrakoot."""
    offline_llm.set_active_model(req.model)
    return {
        "success": True,
        "active_model": req.model,
        "catalog": offline_llm.get_model_catalog()
    }

@app.post("/api/models/pull")
async def pull_model_endpoint(req: ModelRequest):
    """Streams Ollama model download progress."""
    model_tag = req.model.strip()
    if not model_tag:
        raise HTTPException(status_code=400, detail="Model name required")

    def generate_pull_stream():
        for chunk in offline_llm.pull_model_stream(model_tag):
            yield f"data: {json.dumps(chunk)}\n\n"

    return StreamingResponse(generate_pull_stream(), media_type="text/event-stream")

@app.get("/api/downloads/status")
async def get_download_status():
    """Returns the current background model downloader status."""
    status_file = ROOT_DIR / "model_batch_download_status.json"
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"status": "idle", "percent": 0}

@app.post("/api/chat")
async def chat_http_endpoint(req: ChatRequest):
    """Fallback HTTP endpoint for prompt-response chat."""
    res = offline_llm.query_offline_llm(
        prompt=req.message,
        history=req.history,
        model=req.model,
        with_tools=False
    )
    return res

@app.websocket("/ws/chat")
async def chat_websocket_endpoint(websocket: WebSocket):
    """High-speed streaming WebSocket connection for Chitrakoot's interactive chat."""
    await websocket.accept()
    logger.info("⚡ [Chitrakoot WS] Client connected to Neural Core.")

    # Send initial greeting and system status
    gpu = offline_llm.get_gpu_info()
    active_model = offline_llm.get_best_offline_model()
    catalog = offline_llm.get_model_catalog()
    await websocket.send_json({
        "type": "ready",
        "message": "Chitrakoot Neural Core online and synchronized.",
        "gpu": gpu,
        "model": active_model,
        "catalog": catalog
    })

    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                data = json.loads(raw_data)
            except Exception:
                data = {"type": "chat", "message": raw_data}

            msg_type = data.get("type", "chat")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong", "time": time.time()})
                continue

            if msg_type == "select_model":
                target_model = data.get("model", "").strip()
                if target_model:
                    offline_llm.set_active_model(target_model)
                    active_model = target_model
                    await websocket.send_json({
                        "type": "model_selected",
                        "model": target_model,
                        "catalog": offline_llm.get_model_catalog()
                    })
                continue

            if msg_type in ("chat", "message"):
                user_msg = data.get("message", "").strip()
                if not user_msg:
                    continue

                history = data.get("history", [])
                chosen_model = data.get("model") or active_model

                logger.info(f"⚡ [Chitrakoot WS] Query received: '{user_msg[:50]}...' (Model: {chosen_model})")

                # Stream response tokens
                full_reply = []
                last_metadata = {}
                initial_start_sent = False

                for chunk in offline_llm.stream_offline_chat(
                    prompt=user_msg,
                    history=history,
                    model=chosen_model
                ):
                    if not initial_start_sent:
                        # Send start message with model and Copilot Fusion details
                        await websocket.send_json({
                            "type": "start",
                            "model": chunk.get("model", chosen_model),
                            "fusion": chunk.get("fusion")
                        })
                        initial_start_sent = True

                    if "error" in chunk:
                        await websocket.send_json({
                            "type": "error",
                            "error": chunk["error"]
                        })
                        break

                    token = chunk.get("token", "")
                    if token:
                        full_reply.append(token)
                        await websocket.send_json({
                            "type": "token",
                            "token": token
                        })

                    if chunk.get("done", False):
                        last_metadata = {
                            "elapsed_sec": chunk.get("elapsed_sec", 0),
                            "eval_count": chunk.get("eval_count", 0),
                            "eval_duration": chunk.get("eval_duration", 0),
                            "model": chunk.get("model", chosen_model),
                            "gpu": chunk.get("gpu", gpu),
                            "fusion": chunk.get("fusion")
                        }

                # Complete transmission
                await websocket.send_json({
                    "type": "done",
                    "full_text": "".join(full_reply),
                    "meta": last_metadata
                })

    except WebSocketDisconnect:
        logger.info("⚡ [Chitrakoot WS] Client disconnected cleanly.")
    except Exception as e:
        logger.error(f"⚡ [Chitrakoot WS] Error: {e}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("CHITRAKOOT_PORT", "8001"))
    logger.info(f"Starting Chitrakoot standalone server on http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
