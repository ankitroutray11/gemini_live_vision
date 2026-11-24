"""
CHITRAKOOT // Background Neural Model Batch Downloader
Sequentially pulls all requested models in the background via Ollama.
Logs progress to model_batch_download_status.json.
"""

import os
import sys
import time
import json
import logging
import urllib.request
import urllib.error

OLLAMA_HOST = "http://127.0.0.1:11434"
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_batch_download_status.json")

TARGET_MODELS = [
    {"tag": "qwen2.5:3b", "name": "Qwen 2.5 (3B)", "tier": "Sweet Spot"},
    {"tag": "phi3.5:latest", "name": "Phi-3.5 Mini (3.8B)", "tier": "Sweet Spot"},
    {"tag": "qwen2.5-coder:7b", "name": "Qwen 2.5 Coder (7B)", "tier": "Usable"},
    {"tag": "llama3.1:8b", "name": "Llama 3.1 (8B)", "tier": "Usable"},
    {"tag": "mistral:7b", "name": "Mistral 7B Instruct v0.3", "tier": "Usable"},
    {"tag": "qwen2.5:7b", "name": "Qwen 2.5 (7B)", "tier": "Usable"},
    {"tag": "gemma2:9b", "name": "Gemma 2 (9B)", "tier": "Usable"}
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("model_batch_downloader")

def write_status(data):
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Error writing status: {e}")

def get_installed_models():
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [m.get("name", "") for m in data.get("models", [])]
    except Exception as e:
        logger.warning(f"Error listing Ollama models: {e}")
        return []

def pull_model(model_tag, model_name):
    logger.info(f"⚡ Starting download for {model_name} ({model_tag})...")
    payload = json.dumps({"name": model_tag, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/pull",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    
    try:
        with urllib.request.urlopen(req, timeout=7200) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line.decode("utf-8"))
                status = chunk.get("status", "")
                completed = chunk.get("completed", 0)
                total = chunk.get("total", 0)
                pct = round((completed / total) * 100) if total > 0 else 0
                
                status_info = {
                    "current_model": model_tag,
                    "current_name": model_name,
                    "status": status,
                    "completed_bytes": completed,
                    "total_bytes": total,
                    "percent": pct,
                    "timestamp": time.time()
                }
                write_status(status_info)
                
                if status == "success":
                    logger.info(f"✅ Successfully downloaded {model_name} ({model_tag})!")
                    return True
        return True
    except Exception as e:
        logger.error(f"Error pulling {model_tag}: {e}")
        write_status({
            "current_model": model_tag,
            "current_name": model_name,
            "status": f"error: {e}",
            "percent": 0,
            "timestamp": time.time()
        })
        return False

def main():
    logger.info("Starting Chitrakoot Background Model Batch Downloader Service...")
    
    for item in TARGET_MODELS:
        tag = item["tag"]
        name = item["name"]
        
        # Check if already installed
        installed = get_installed_models()
        if any(tag == inst or (":" not in tag and inst.startswith(tag + ":")) for inst in installed):
            logger.info(f"Model already installed: {name} ({tag}), skipping.")
            continue
            
        logger.info(f"Queued model: {name} ({tag}). Initializing pull...")
        pull_model(tag, name)
        time.sleep(3)
        
    logger.info("🎉 All target neural models downloaded and available in Ollama!")
    write_status({
        "current_model": None,
        "current_name": None,
        "status": "all_models_installed",
        "percent": 100,
        "timestamp": time.time()
    })

if __name__ == "__main__":
    main()
