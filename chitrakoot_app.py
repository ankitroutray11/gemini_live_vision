"""
CHITRAKOOT // Desktop Application Launcher
Dedicated 100% Offline AI Interactive Chat Application.
Launches the Chitrakoot Neural Core in edge-to-edge futuristic app mode.
"""

import os
import sys
import time
import socket
import threading
import subprocess
from pathlib import Path

# Setup paths
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))
os.chdir(str(ROOT_DIR))

# Ensure UTF-8 console output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PORT = 8001

def is_port_in_use(port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def start_server():
    try:
        import uvicorn
        from chitrakoot_server import app
        config = uvicorn.Config(app=app, host="127.0.0.1", port=PORT, log_level="warning")
        server = uvicorn.Server(config)
        server.run()
    except Exception as e:
        print(f"Chitrakoot server error: {e}")

def ensure_window_maximized(app_title: str = "CHITRAKOOT", delays=(0.5, 1.0, 1.8, 3.0)):
    """Monitors for the Chitrakoot application window and maximizes it."""
    def _worker():
        for d in delays:
            time.sleep(d)
            try:
                import ctypes
                user32 = ctypes.windll.user32
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                found_hwnds = []

                def enum_cb(hwnd, extra):
                    if user32.IsWindowVisible(hwnd):
                        length = user32.GetWindowTextLengthW(hwnd)
                        if length > 0:
                            buff = ctypes.create_unicode_buffer(length + 1)
                            user32.GetWindowTextW(hwnd, buff, length + 1)
                            val = buff.value.lower()
                            if app_title.lower() in val or str(PORT) in val:
                                found_hwnds.append(hwnd)
                    return True

                cb = WNDENUMPROC(enum_cb)
                user32.EnumWindows(cb, 0)
                for h in found_hwnds:
                    user32.ShowWindow(h, 3)  # SW_MAXIMIZE = 3
                    user32.SetForegroundWindow(h)
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()

def launch_chitrakoot():
    print("========================================================")
    print(" ✦ CHITRAKOOT // NEURAL CORE")
    print(" 100% Offline Local GPU AI Interactive Chat")
    print("========================================================")

    # 1. Start local Chitrakoot server if not running
    if not is_port_in_use(PORT):
        print(f" [SYSTEM] Initializing Chitrakoot Neural Core server on port {PORT}...")
        srv_thread = threading.Thread(target=start_server, daemon=True)
        srv_thread.start()

        # Wait for port to open
        for _ in range(60):
            if is_port_in_use(PORT):
                break
            time.sleep(0.1)

    url = f"http://127.0.0.1:{PORT}/?_cb={int(time.time())}"
    print(f" [SYSTEM] Chitrakoot is ready at {url}")

    # 2. Pre-warm local GPU engine
    try:
        from offline_llm import ensure_ollama_running
        threading.Thread(target=ensure_ollama_running, daemon=True).start()
    except Exception:
        pass

    # 3. Locate Chrome or Edge for native App Mode
    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    browser_exe = next((p for p in chrome_candidates if os.path.exists(p)), None)

    if browser_exe:
        print(f" [SYSTEM] Launching native Chitrakoot window via {os.path.basename(browser_exe)} in FULL SCREEN mode...")
        profile_dir = ROOT_DIR / ".chitrakoot_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            browser_exe,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--start-fullscreen",
            "--start-maximized",
            "--window-size=1920,1080",
            "--window-position=0,0",
            "--test-type",
            "--disable-infobars",
            "--disable-application-cache",
            "--disk-cache-size=1",
            "--media-cache-size=1",
        ]
        ensure_window_maximized("CHITRAKOOT")
        proc = subprocess.Popen(cmd)
        proc.wait()

        # Keep server alive while window is active
        while is_port_in_use(PORT):
            time.sleep(1)
        return

    # Fallback to default browser
    import webbrowser
    webbrowser.open(url)

if __name__ == "__main__":
    launch_chitrakoot()
