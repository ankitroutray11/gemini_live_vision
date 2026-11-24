"""
Chirkoot MK-IV // Offline Local GPU LLM Engine
Provides 100% offline local inference using Ollama and the user's NVIDIA GPU.
Supports local tool execution, automatic daemon startup, and seamless failover.
"""

import os
import sys
import json
import time
import socket
import logging
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("chirkoot_offline_llm")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

OLLAMA_HOST = "http://127.0.0.1:11434"
PREFERRED_MODELS = [
    "llama3.2:latest",
    "qwen2.5:1.5b",
    "qwen2.5:3b",
    "phi3.5:latest",
    "qwen3.5:4b",
    "llama3.1:8b",
    "qwen2.5-coder:7b",
    "qwen2.5:7b",
    "mistral:7b",
    "gemma2:9b"
]

MODEL_CATALOG: List[Dict[str, Any]] = [
    # ✨ Copilot Fusion (Auto-Route Intelligent Neural Engine)
    {
        "id": "copilot-fusion",
        "name": "✨ Copilot Fusion (Auto-Route)",
        "tier": "fusion",
        "tier_name": "✨ Copilot Fusion Engine",
        "ram": "Smart Adaptive",
        "speed": "Smart Adaptive (~15–30+ tok/s)",
        "description": "Neural router that dynamically inspects prompt intent and complexity in real-time, routing to the optimal specialist model (Coding -> Qwen Coder, Deep Logic -> Llama 3.1/Gemma 2, Fast Chat -> Llama 3.2).",
        "tag": "copilot-fusion",
        "size_gb": 0.0,
        "is_fusion": True
    },
    # 🟢 Sweet Spot (Fastest & Most Responsive: ~15–30+ tokens/sec)
    {
        "id": "llama3.2:latest",
        "name": "Llama 3.2 (3B)",
        "tier": "sweet_spot",
        "tier_name": "🟢 Sweet Spot (15–30+ tok/s)",
        "ram": "~2.0 GB",
        "speed": "Fastest (~15-30+ tok/s)",
        "description": "Great general knowledge and conversation. Responds almost instantly.",
        "tag": "llama3.2:latest",
        "size_gb": 2.0
    },
    {
        "id": "qwen2.5:3b",
        "name": "Qwen 2.5 (3B)",
        "tier": "sweet_spot",
        "tier_name": "🟢 Sweet Spot (15–30+ tok/s)",
        "ram": "~2.0 GB",
        "speed": "Fastest (~15-30+ tok/s)",
        "description": "Strong compact reasoning and quick coding for its size.",
        "tag": "qwen2.5:3b",
        "size_gb": 1.9
    },
    {
        "id": "qwen2.5:1.5b",
        "name": "Qwen 2.5 (1.5B)",
        "tier": "sweet_spot",
        "tier_name": "🟢 Sweet Spot (15–30+ tok/s)",
        "ram": "~1.5 GB",
        "speed": "Ultra-Fast (~30+ tok/s)",
        "description": "Blisteringly responsive, ideal for fast coding assistance and instant chat.",
        "tag": "qwen2.5:1.5b",
        "size_gb": 1.0
    },
    {
        "id": "phi3.5:latest",
        "name": "Phi-3.5 Mini (3.8B)",
        "tier": "sweet_spot",
        "tier_name": "🟢 Sweet Spot (15–30+ tok/s)",
        "ram": "~2.4 GB",
        "speed": "Fastest (~15-30+ tok/s)",
        "description": "Microsoft's highly optimized logic and reasoning model.",
        "tag": "phi3.5:latest",
        "size_gb": 2.2
    },
    # 🟡 Good / Usable Tier (~5–9 tokens/sec — reading speed)
    {
        "id": "llama3.1:8b",
        "name": "Llama 3.1 (8B - Q4_K_M)",
        "tier": "usable",
        "tier_name": "🟡 High Reasoning (~5–9 tok/s)",
        "ram": "~5.0 GB",
        "speed": "Reading Speed (~5-9 tok/s)",
        "description": "Standard benchmark for open-source AI with deep knowledge and reasoning.",
        "tag": "llama3.1:8b",
        "size_gb": 4.7
    },
    {
        "id": "qwen2.5-coder:7b",
        "name": "Qwen 2.5 Coder (7B - Q4_K_M)",
        "tier": "usable",
        "tier_name": "🟡 High Reasoning (~5–9 tok/s)",
        "ram": "~4.8 GB",
        "speed": "Reading Speed (~5-9 tok/s)",
        "description": "Excellent for code generation, software architecture, and technical troubleshooting.",
        "tag": "qwen2.5-coder:7b",
        "size_gb": 4.7
    },
    {
        "id": "qwen2.5:7b",
        "name": "Qwen 2.5 (7B - Q4_K_M)",
        "tier": "usable",
        "tier_name": "🟡 High Reasoning (~5–9 tok/s)",
        "ram": "~4.8 GB",
        "speed": "Reading Speed (~5-9 tok/s)",
        "description": "High-quality output, mathematical reasoning, and nuanced problem solving.",
        "tag": "qwen2.5:7b",
        "size_gb": 4.7
    },
    {
        "id": "mistral:7b",
        "name": "Mistral 7B Instruct v0.3 (Q4_K_M)",
        "tier": "usable",
        "tier_name": "🟡 High Reasoning (~5–9 tok/s)",
        "ram": "~4.5 GB",
        "speed": "Reading Speed (~5-9 tok/s)",
        "description": "Fast, versatile, and well-rounded instruction following.",
        "tag": "mistral:7b",
        "size_gb": 4.1
    },
    {
        "id": "gemma2:9b",
        "name": "Gemma 2 (9B - Q4_K_M)",
        "tier": "usable",
        "tier_name": "🟡 High Reasoning (~3–5 tok/s)",
        "ram": "~5.5 GB",
        "speed": "Complex Reasoning (~3-5 tok/s)",
        "description": "Very smart Google model with exceptional output quality.",
        "tag": "gemma2:9b",
        "size_gb": 5.4
    }
]

_active_model: Optional[str] = "copilot-fusion"

_cached_gpu_info: Optional[Dict[str, Any]] = None
_ollama_process: Optional[subprocess.Popen] = None

def get_gpu_info() -> Dict[str, Any]:
    """Inspects the local GPU specs via nvidia-smi."""
    global _cached_gpu_info
    if _cached_gpu_info is not None:
        return _cached_gpu_info

    info = {
        "name": "NVIDIA GPU",
        "vram_total_mb": 0,
        "vram_used_mb": 0,
        "cuda_version": "N/A",
        "driver_version": "N/A",
        "available": False
    }

    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.strip().split(",")]
            if len(parts) >= 4:
                info["name"] = parts[0]
                info["vram_total_mb"] = int(float(parts[1]))
                info["vram_used_mb"] = int(float(parts[2]))
                info["driver_version"] = parts[3]
                info["available"] = True
    except Exception as e:
        logger.debug(f"nvidia-smi check notice: {e}")

    _cached_gpu_info = info
    return info

def is_ollama_running() -> bool:
    """Checks if Ollama HTTP service is listening on port 11434."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", 11434)) == 0
    except Exception:
        return False

def ensure_ollama_running(max_wait_seconds: int = 8) -> bool:
    """Ensures the local Ollama server is running, spawning it if necessary."""
    global _ollama_process
    if is_ollama_running():
        return True

    logger.info("⚡ [Offline GPU Engine] Starting Ollama local server...")
    try:
        # Launch ollama serve in background
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE

        _ollama_process = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo
        )

        for _ in range(max_wait_seconds * 10):
            if is_ollama_running():
                logger.info("✅ [Offline GPU Engine] Ollama server is live.")
                return True
            time.sleep(0.1)
    except Exception as e:
        logger.error(f"Error starting Ollama serve: {e}")

    return is_ollama_running()

def get_available_models() -> List[str]:
    """Returns the list of locally pulled Ollama models."""
    if not is_ollama_running() and not ensure_ollama_running(3):
        return []

    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        logger.warning(f"Notice listing Ollama models: {e}")
        return []

# ==============================================================================
# COPILOT FUSION // NEURAL INTENT ROUTER
# ==============================================================================

def route_prompt_with_fusion(prompt: str, available_models: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Copilot Fusion Neural Router.
    Analyzes prompt semantics, code patterns, and reasoning depth to automatically
    select the best installed offline model.
    """
    if available_models is None:
        available_models = get_available_models()

    p_lower = prompt.lower().strip()

    # 1. Evaluate Code Intent
    code_score = 0
    code_indicators = [
        "```", "def ", "class ", "import ", "from ", "function", "const ", "let ",
        "var ", "return ", "async ", "await ", "public class", "static void", "#include",
        "<!doctype", "<html", "select ", "from ", "where ", "join ", "create table",
        "code", "script", "python", "javascript", "typescript", "c++", "c#", "rust",
        "sql", "powershell", "bash", "regex", "api", "endpoint", "bug", "debug",
        "error", "exception", "traceback", "refactor", "syntax", "algorithm", "json",
        "git", "docker", "npm", "pip", "compile", "method"
    ]
    for ci in code_indicators:
        if ci in p_lower:
            code_score += 3 if ci in ("```", "def ", "class ", "script", "python", "sql", "bug", "debug", "function") else 1

    # 2. Evaluate Deep Reasoning & Logic Intent
    reasoning_score = 0
    reasoning_indicators = [
        "why is", "why does", "prove", "proof", "theorem", "calculate", "math",
        "equation", "logic", "riddle", "puzzle", "step-by-step", "step by step",
        "analyze", "evaluate", "compare and contrast", "philosophical", "ethics",
        "deduce", "deduction", "derive", "probability", "statistics", "trade-off",
        "implications", "pros and cons", "root cause"
    ]
    for ri in reasoning_indicators:
        if ri in p_lower:
            reasoning_score += 3 if ri in ("prove", "proof", "calculate", "step-by-step", "theorem", "math", "deduce") else 1

    # 3. Evaluate Creative Composition Intent
    creative_score = 0
    creative_indicators = [
        "story", "poem", "poetry", "narrative", "fiction", "novel", "dialogue",
        "roleplay", "character", "metaphor", "rhyme", "essay", "compose", "draft an email",
        "speech", "creative", "brainstorm", "slogan", "write a letter"
    ]
    for cri in creative_indicators:
        if cri in p_lower:
            creative_score += 3 if cri in ("story", "poem", "poetry", "dialogue", "novel", "rhyme") else 1

    # Classify Intent
    if code_score >= 2 and code_score >= reasoning_score and code_score >= creative_score:
        intent_type = "code"
        intent_name = "Code & System Engineering"
        badge = "⚡ CODE SPECIALIST"
        ideal_target = "qwen2.5-coder:7b"
        priorities = ["qwen2.5-coder:7b", "qwen2.5:3b", "qwen2.5:1.5b", "qwen3.5:4b", "llama3.2:latest"]
        reason = "Detected programming syntax, script directive, or debugging request"
    elif reasoning_score >= 2 and reasoning_score >= creative_score:
        intent_type = "reasoning"
        intent_name = "Deep Reasoning & Logic"
        badge = "🧠 REASONING SPECIALIST"
        ideal_target = "llama3.1:8b"
        priorities = ["llama3.1:8b", "gemma2:9b", "phi3.5:latest", "qwen2.5:7b", "qwen3.5:4b", "llama3.2:latest"]
        reason = "Detected mathematical derivation, complex logical deduction, or deep comparative analysis"
    elif creative_score >= 2:
        intent_type = "creative"
        intent_name = "Creative Composition"
        badge = "🎨 CREATIVE SPECIALIST"
        ideal_target = "mistral:7b"
        priorities = ["mistral:7b", "llama3.1:8b", "llama3.2:latest", "qwen2.5:3b"]
        reason = "Detected creative storytelling, metaphorical prose, or narrative generation"
    else:
        intent_type = "chat"
        intent_name = "High-Speed General Chat"
        badge = "⚡ HIGH-SPEED CORE"
        ideal_target = "llama3.2:latest"
        priorities = ["llama3.2:latest", "qwen2.5:1.5b", "qwen2.5:3b", "phi3.5:latest"]
        reason = "Detected conversational inquiry, rapid definition, or general knowledge request"

    # Select the highest-priority installed model
    selected = None
    is_fallback = False

    for candidate in priorities:
        cand_base = candidate.split(":")[0]
        for inst in available_models:
            if inst == candidate or inst.split(":")[0] == cand_base:
                selected = inst
                break
        if selected:
            break

    if not selected:
        selected = available_models[0] if available_models else "llama3.2:latest"
        is_fallback = True
    elif selected.split(":")[0] != ideal_target.split(":")[0]:
        is_fallback = True

    confidence = min(99, 88 + max(code_score, reasoning_score, creative_score) * 2)

    return {
        "intent_type": intent_type,
        "intent_name": intent_name,
        "badge": badge,
        "ideal_model": ideal_target,
        "selected_model": selected,
        "is_fallback": is_fallback,
        "confidence": f"{confidence}%",
        "reasoning": reason
    }

def get_best_offline_model() -> str:
    """Selects the active or best installed offline LLM model for GPU processing."""
    global _active_model
    if _active_model in ("copilot-fusion", "fusion:auto", "fusion"):
        return "copilot-fusion"

    installed = get_available_models()
    if _active_model and (_active_model in installed or any(m.startswith(_active_model.split(":")[0]) for m in installed)):
        return _active_model

    return "copilot-fusion"

def set_active_model(model_name: str) -> bool:
    """Sets the active model for Chitrakoot."""
    global _active_model
    _active_model = model_name
    return True

def get_model_catalog() -> Dict[str, Any]:
    """Returns the full model catalog with installation state and active indicator."""
    installed = get_available_models()
    active = get_best_offline_model()

    catalog_out = []
    seen_tags = set()

    for item in MODEL_CATALOG:
        m = dict(item)
        if m.get("is_fusion") or m["id"] == "copilot-fusion":
            m["installed"] = True
            m["active"] = (active == "copilot-fusion")
            catalog_out.append(m)
            seen_tags.add("copilot-fusion")
            continue

        is_inst = False
        actual_name = m["id"]
        for inst in installed:
            # Match exact tag, or if tag is :latest match base name
            if inst == m["id"] or inst == m.get("tag"):
                is_inst = True
                actual_name = inst
                break
            elif m["id"].endswith(":latest") and inst.split(":")[0] == m["id"].split(":")[0]:
                is_inst = True
                actual_name = inst
                break
            elif ":" not in m["id"] and inst.split(":")[0] == m["id"]:
                is_inst = True
                actual_name = inst
                break

        m["installed"] = is_inst
        m["active"] = (actual_name == active or m["id"] == active)
        catalog_out.append(m)

        # Track seen model tags to prevent duplicates in custom section
        seen_tags.add(m["id"])
        seen_tags.add(m.get("tag", ""))
        seen_tags.add(m["id"].split(":")[0])
        if is_inst:
            seen_tags.add(actual_name)
            seen_tags.add(actual_name.split(":")[0])

    # Include any other models installed in Ollama (e.g. qwen3.5:4b)
    for inst in installed:
        base_name = inst.split(":")[0]
        if inst not in seen_tags and base_name not in seen_tags:
            catalog_out.append({
                "id": inst,
                "name": inst.upper(),
                "tier": "custom",
                "tier_name": "⭐ Local Custom",
                "ram": "Local GPU",
                "speed": "Local GPU",
                "description": "Locally installed custom Ollama model.",
                "tag": inst,
                "size_gb": 3.0,
                "installed": True,
                "active": (inst == active)
            })
            seen_tags.add(inst)
            seen_tags.add(base_name)

    return {
        "active_model": active,
        "models": catalog_out
    }

def pull_model_stream(model_tag: str):
    """Generator streaming Ollama download progress chunks."""
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/pull",
        data=json.dumps({"name": model_tag, "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        for line in resp:
            if line.strip():
                yield json.loads(line.decode("utf-8"))

def build_ollama_tools() -> List[Dict[str, Any]]:
    """Converts tools from tools.py into OpenAI-compatible tool definitions for Ollama."""
    return [
        {
            "type": "function",
            "function": {
                "name": "open_app_or_url",
                "description": "Opens an application or website URL on the user's Windows computer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "Application name or URL (e.g. 'notepad', 'calc', 'chrome', 'https://youtube.com')"}
                    },
                    "required": ["target"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "toggle_fullscreen_window",
                "description": "Toggles borderless full screen mode on or off for the Chirkoot window.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "maximize_active_window",
                "description": "Maximizes the Chirkoot window to fill the entire desktop screen.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "restore_active_window",
                "description": "Restores the Chirkoot window down from fullscreen or maximize to a floating window.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "minimize_active_window",
                "description": "Minimizes the current window to the Windows taskbar.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "press_hotkey",
                "description": "Executes a native Windows keyboard shortcut (e.g. 'f11', 'win', 'alt,tab', 'ctrl,s').",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keys": {"type": "string", "description": "Key or hotkey combination separated by commas."}
                    },
                    "required": ["keys"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "type_text",
                "description": "Types text or code into the currently active Windows input field.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "The text to type."},
                        "press_enter": {"type": "boolean", "description": "Whether to press Enter after typing."}
                    },
                    "required": ["text"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "click_screen",
                "description": "Clicks at normalized coordinates (0-1000) on the user's screen.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer", "description": "X coordinate from 0 to 1000 (0=left, 1000=right)."},
                        "y": {"type": "integer", "description": "Y coordinate from 0 to 1000 (0=top, 1000=bottom)."}
                    },
                    "required": ["x", "y"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "ocr_screen",
                "description": "Scans and extracts text and UI labels currently visible on the user's desktop screen.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "Executes a PowerShell terminal command on the host machine and returns stdout/stderr.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The exact PowerShell command to run."}
                    },
                    "required": ["command"]
                }
            }
        }
    ]

SYSTEM_PROMPT_OFFLINE = """You are CHIRKOOT MK-IV, a tactical multimodal desktop AI assistant operating in 100% OFFLINE LOCAL GPU MODE.
You are powered locally by the user's NVIDIA GPU via Ollama. You have full access to control the Windows desktop, open applications, toggle fullscreen, click, type, and scan the screen.
When the user asks you to perform an action (e.g. open an application, go fullscreen, maximize, minimize, scan screen, click, type), call the appropriate tool.
Keep your conversational answers crisp, direct, confident, and tactical (1-2 sentences). You are helpful, intelligent, and humorous when appropriate."""

SYSTEM_PROMPT_CHITRAKOOT = """You are CHITRAKOOT, an advanced futuristic AI neural core running 100% offline locally on the user's NVIDIA GPU.
You are articulate, intelligent, sharp, and helpful. You excel at conversational analysis, coding, system architecture, problem solving, creative thinking, and Windows automation.
Provide insightful, beautifully formatted responses with markdown formatting, code snippets, and structured bullet points where helpful.
You operate completely privately, securely, and offline with zero cloud telemetry."""

def stream_offline_chat(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None
):
    """Streams response tokens from local Ollama LLM for Chitrakoot.
    
    Yields dictionary chunks with token content, metadata, and Copilot Fusion routing.
    """
    if not is_ollama_running() and not ensure_ollama_running(5):
        yield {
            "error": "Offline Local GPU Engine could not connect to Ollama. Please ensure Ollama is installed and running.",
            "done": True
        }
        return

    fusion_meta = None
    chosen_model = model or get_best_offline_model()
    if chosen_model in ("copilot-fusion", "fusion:auto", "fusion") or (model is None and _active_model == "copilot-fusion"):
        fusion_meta = route_prompt_with_fusion(prompt)
        chosen_model = fusion_meta["selected_model"]
        logger.info(f"⚡ [Copilot Fusion Router] Intent: {fusion_meta['intent_name']} -> Selected: {chosen_model}")

    gpu = get_gpu_info()

    messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT_CHITRAKOOT}]
    if history:
        for msg in history[-8:]:
            messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": chosen_model,
        "messages": messages,
        "stream": True
    }

    t0 = time.time()
    try:
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line.decode("utf-8"))
                token = chunk.get("message", {}).get("content", "")
                done = chunk.get("done", False)

                out = {
                    "token": token,
                    "done": done,
                    "model": chosen_model,
                    "gpu": gpu,
                    "fusion": fusion_meta
                }
                if done:
                    out["elapsed_sec"] = round(time.time() - t0, 2)
                    out["eval_count"] = chunk.get("eval_count", 0)
                    out["eval_duration"] = chunk.get("eval_duration", 0)
                yield out

    except Exception as e:
        logger.error(f"Error in stream_offline_chat: {e}")
        yield {
            "error": f"Local inference error: {e}",
            "done": True,
            "model": chosen_model,
            "gpu": gpu,
            "fusion": fusion_meta
        }

def query_offline_llm(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    model: Optional[str] = None,
    with_tools: bool = True
) -> Dict[str, Any]:
    """Queries the local Ollama LLM running on the user's GPU.
    
    Executes tool calls automatically and returns the final assistant answer.
    """
    if not is_ollama_running() and not ensure_ollama_running(5):
        return {
            "success": False,
            "response": "Offline Local GPU Engine could not connect to Ollama. Please ensure Ollama is installed and running.",
            "model": "None",
            "gpu": get_gpu_info(),
            "tool_calls": []
        }

    fusion_meta = None
    chosen_model = model or get_best_offline_model()
    if chosen_model in ("copilot-fusion", "fusion:auto", "fusion") or (model is None and _active_model == "copilot-fusion"):
        fusion_meta = route_prompt_with_fusion(prompt)
        chosen_model = fusion_meta["selected_model"]

    gpu = get_gpu_info()

    messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT_OFFLINE}]
    if history:
        for msg in history[-6:]:  # Keep recent context
            messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
    messages.append({"role": "user", "content": prompt})

    tools = build_ollama_tools() if with_tools else None

    executed_tool_calls: List[Dict[str, Any]] = []

    try:
        payload: Dict[str, Any] = {
            "model": chosen_model,
            "messages": messages,
            "stream": False
        }
        if tools:
            payload["tools"] = tools

        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )

        t0 = time.time()
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed = round(time.time() - t0, 2)

        msg = data.get("message", {})
        tool_calls = msg.get("tool_calls", [])

        # If model returned tool calls, execute them locally!
        if tool_calls:
            from tools import TOOL_MAP

            for tc in tool_calls:
                func_obj = tc.get("function", {})
                tool_name = func_obj.get("name")
                tool_args = func_obj.get("arguments", {})

                tool_result = f"Error: Tool '{tool_name}' not found."
                if tool_name in TOOL_MAP:
                    try:
                        logger.info(f"⚡ [Offline GPU Engine] Executing tool: {tool_name}({tool_args})")
                        res = TOOL_MAP[tool_name](**tool_args)
                        tool_result = str(res)
                    except Exception as err:
                        tool_result = f"Execution error: {err}"

                executed_tool_calls.append({
                    "name": tool_name,
                    "args": tool_args,
                    "result": tool_result
                })

                # Append tool call and result to message history for final reply
                messages.append(msg)
                messages.append({
                    "role": "tool",
                    "content": tool_result
                })

            # Get final conversational response after tool execution
            followup_payload = {
                "model": chosen_model,
                "messages": messages,
                "stream": False
            }
            req_followup = urllib.request.Request(
                f"{OLLAMA_HOST}/api/chat",
                data=json.dumps(followup_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req_followup, timeout=30) as resp2:
                data2 = json.loads(resp2.read().decode("utf-8"))
                final_text = data2.get("message", {}).get("content", "").strip()
                if not final_text:
                    final_text = f"Executed {len(executed_tool_calls)} action(s) successfully on your Windows system."
                return {
                    "success": True,
                    "response": final_text,
                    "model": chosen_model,
                    "gpu": gpu,
                    "elapsed_sec": elapsed,
                    "tool_calls": executed_tool_calls,
                    "offline": True
                }

        # Direct text answer without tools
        final_text = msg.get("content", "").strip()
        return {
            "success": True,
            "response": final_text,
            "model": chosen_model,
            "gpu": gpu,
            "elapsed_sec": elapsed,
            "tool_calls": executed_tool_calls,
            "offline": True
        }

    except Exception as e:
        logger.error(f"Error querying offline LLM: {e}")
        return {
            "success": False,
            "response": f"Offline Local GPU Error: {e}",
            "model": chosen_model,
            "gpu": gpu,
            "tool_calls": executed_tool_calls,
            "offline": True
        }
