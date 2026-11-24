import os
import sys
import time
import socket
import signal
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

from learning_engine import StartupWatchdog
from deployment_manager import (
    acquire_single_instance_mutex,
    release_single_instance_mutex,
    bring_existing_instance_to_foreground,
    wait_for_backend_health,
    check_backend_health_http,
    get_user_data_dir,
    ensure_user_data_directories
)

# Initialize user data folders in %LOCALAPPDATA%\Chirkoot
ensure_user_data_directories()

# Single-Instance Mutex Enforcement
is_primary, _mutex_handle = acquire_single_instance_mutex("Local\\Chirkoot_MKV_Desktop_Mutex")
if not is_primary:
    print("[SYSTEM] Another Chirkoot desktop instance is already active. Activating window...")
    bring_existing_instance_to_foreground("CHIRKOOT")
    sys.exit(0)

# Graceful OS shutdown signal handling
def _handle_exit_signal(*args):
    print("\n[SYSTEM] Received shutdown signal. Initiating clean desktop shutdown...")
    if _mutex_handle:
        release_single_instance_mutex(_mutex_handle)
    sys.exit(0)

if sys.platform == "win32":
    try:
        import ctypes
        PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)
        def _win_ctrl_handler(ctrl_type):
            _handle_exit_signal()
            return True
        _global_handler = PHANDLER_ROUTINE(_win_ctrl_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_global_handler, True)
    except Exception:
        pass
signal.signal(signal.SIGINT, _handle_exit_signal)
signal.signal(signal.SIGTERM, _handle_exit_signal)

def is_port_in_use(port: int = 8000) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def start_server():
    try:
        import uvicorn
        from server import app
        config = uvicorn.Config(app=app, host="127.0.0.1", port=8000, log_level="warning")
        server = uvicorn.Server(config)
        server.run()
    except Exception as e:
        print(f"Server startup error: {e}")

def ensure_window_maximized(app_title: str = "CHIRKOOT", delays=(0.5, 1.0, 1.8, 3.0)):
    """Monitors for the newly launched Chirkoot application window and forces Windows to maximize it."""
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
                            if app_title.lower() in val or "8000" in val:
                                found_hwnds.append(hwnd)
                    return True

                cb = WNDENUMPROC(enum_cb)
                user32.EnumWindows(cb, 0)
                for h in found_hwnds:
                    user32.ShowWindow(h, 3)  # SW_MAXIMIZE = 3
                    user32.SetForegroundWindow(h)
                    try:
                        from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER, HostType
                        GLOBAL_HUD_EXCLUSION_MANAGER.register_hud_window(h, HostType.CHROME_APP_MODE)
                    except Exception:
                        pass
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()

def launch_desktop():
    print("========================================================")
    print(" ✦ CHIRKOOT DESKTOP ASSISTANT")
    print(" Multimodal Screen & Vision AI Agent")
    print("========================================================")

    # Watchdog boot check
    watchdog_state = StartupWatchdog.record_startup_attempt()
    if "--safe-mode" in sys.argv or StartupWatchdog.is_safe_mode_forced():
        os.environ["CHIRKOOT_SAFE_MODE"] = "1"
        if StartupWatchdog.is_safe_mode_forced():
            print(f"🛡️ [WATCHDOG RECOVERY ENGAGED] Detected {watchdog_state.get('consecutive_crashes')} consecutive crashes. Booting into Safe Mode and restoring LKG.")
        else:
            print("🛡️ [SAFE MODE ENABLED] Starting Chirkoot without custom dynamic skills or self-modification.")

    # 1. Start backend server if not running
    health_url = "http://127.0.0.1:8000/api/health"
    if not check_backend_health_http(health_url):
        print(" [SYSTEM] Starting local Chirkoot backend on port 8000...")
        srv_thread = threading.Thread(target=start_server, daemon=True)
        srv_thread.start()

        # Reliable HTTP health polling
        is_ready = wait_for_backend_health(health_url, max_wait_seconds=12.0)
        if is_ready:
            StartupWatchdog.record_successful_boot()
            print(" [SYSTEM] Chirkoot backend confirmed healthy (HTTP 200 OK).")
        else:
            print(" [WARNING] Backend startup health check timed out. Attempting recovery...")
    else:
        StartupWatchdog.record_successful_boot()
        print(" [SYSTEM] Connected to existing healthy Chirkoot backend.")

    url = f"http://127.0.0.1:8000/?_cb={int(time.time())}"
    print(f" [SYSTEM] Starting Chirkoot Cloud Live Assistant ({url})...")

    # 2. Check for native browser app-mode paths (Chrome or Edge)
    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    browser_exe = next((p for p in chrome_candidates if os.path.exists(p)), None)

    # User profile directory strictly in %LOCALAPPDATA%\Chirkoot\browser_profile
    user_data_dir = get_user_data_dir()
    profile_dir = user_data_dir / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Priority A: Google Chrome / Chromium Native App-Mode Window
    if browser_exe:
        print(f" [SYSTEM] Launching native tactical HUD window via {os.path.basename(browser_exe)} in FULL SCREEN mode...")
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
            "--use-fake-ui-for-media-stream",
            "--disable-application-cache",
            "--disk-cache-size=1",
            "--media-cache-size=1",
        ]
        ensure_window_maximized("CHIRKOOT")
        proc = subprocess.Popen(cmd)
        proc.wait()

        # If the browser launcher process exited immediately (attached to an existing Chrome instance),
        # keep the backend server alive so the HUD remains responsive.
        while check_backend_health_http(health_url):
            time.sleep(1)
        return

    # Priority B: Fallback to pywebview only if no Chromium browser found
    use_pywebview = False
    try:
        import webview
        use_pywebview = True
    except Exception:
        use_pywebview = False

    if use_pywebview:
        try:
            print(" [SYSTEM] Launching native pywebview tactical HUD window in MAXIMIZED mode...")
            from hud_capture_exclusion import GLOBAL_HUD_EXCLUSION_MANAGER, HostType
            GLOBAL_HUD_EXCLUSION_MANAGER.host_type = HostType.PYWEBVIEW

            def _on_pywebview_ready():
                time.sleep(1.0)
                try:
                    import ctypes
                    user32 = ctypes.windll.user32
                    hwnd = user32.FindWindowW(None, "CHIRKOOT MK-V // Tactical Multimodal AI Assistant")
                    if hwnd:
                        GLOBAL_HUD_EXCLUSION_MANAGER.register_hud_window(hwnd, HostType.PYWEBVIEW)
                except Exception as ex:
                    print(f"PyWebView exclusion setup notice: {ex}")

            threading.Thread(target=_on_pywebview_ready, daemon=True).start()

            window = webview.create_window(
                title="CHIRKOOT MK-V // Tactical Multimodal AI Assistant",
                url=url,
                maximized=True,
                resizable=True,
                background_color="#040711"
            )
            webview.start()
            return
        except Exception as e:
            print(f" [NOTICE] pywebview window notice: {e}, using native App Mode window...")

    import webbrowser
    webbrowser.open(url)

if __name__ == "__main__":
    try:
        launch_desktop()
    finally:
        if _mutex_handle:
            release_single_instance_mutex(_mutex_handle)
