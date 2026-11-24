import urllib.request
import json
import time
import subprocess
import sys

proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8008"],
    cwd=r"C:\jarvis\gemini_live_vision"
)
time.sleep(2.5)
try:
    # 1. Test HTML
    with urllib.request.urlopen("http://127.0.0.1:8008/") as resp:
        html = resp.read().decode("utf-8")
        assert "CHIRKOOT" in html
        assert "MK-V" in html
        print("  [PASS] Root HUD HTML served (MK-V brand confirmed)")

    # 2. Test Auth Token
    with urllib.request.urlopen("http://127.0.0.1:8008/api/auth/token") as resp:
        data = json.loads(resp.read().decode("utf-8"))
        assert "token" in data
        assert len(data["token"]) >= 32
        print(f"  [PASS] Ephemeral auth token endpoint returned token: {data['token'][:10]}...")

    # 3. Test Security Status
    with urllib.request.urlopen("http://127.0.0.1:8008/api/security/status") as resp:
        data = json.loads(resp.read().decode("utf-8"))
        assert data["status"] == "nominal"
        assert data["kill_switch_active"] is False
        print(f"  [PASS] Security status nominal (taint_active={data.get('is_tainted')})")

    # 4. Test Memories
    with urllib.request.urlopen("http://127.0.0.1:8008/api/memories") as resp:
        data = json.loads(resp.read().decode("utf-8"))
        assert "memories" in data
        print(f"  [PASS] Memories API returned {len(data['memories'])} stored items")

    # 5. Test Kill Switch POST
    req = urllib.request.Request("http://127.0.0.1:8008/api/kill-switch", data=b"", method="POST")
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        assert data["kill_switch_active"] is True
        print("  [PASS] Emergency Kill Switch engaged via API (POST /api/kill-switch)")

    # 6. Test Kill Switch Reset
    req = urllib.request.Request("http://127.0.0.1:8008/api/kill-switch/reset", data=b"", method="POST")
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        assert data["kill_switch_active"] is False
        print("  [PASS] Emergency Kill Switch reset via API (POST /api/kill-switch/reset)")

    print("\n=================================================================")
    print(" ALL CHIRKOOT MK-V HTTP & API SECURITY ENDPOINTS VERIFIED!")
    print("=================================================================")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()
