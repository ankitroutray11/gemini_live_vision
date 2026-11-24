import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import websockets

SERVER_PORT = 8018
WS_URL = f"ws://127.0.0.1:{SERVER_PORT}/ws"
HTTP_URL = f"http://127.0.0.1:{SERVER_PORT}"

def start_server_process(port=SERVER_PORT):
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=r"C:\jarvis\gemini_live_vision",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    # Wait for server to become responsive
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/security/status", timeout=1.0) as resp:
                if resp.status == 200:
                    return proc
        except Exception:
            time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"Server on port {port} failed to start in 10s")

def stop_server_process(proc):
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

async def run_server_security_tests():
    print("\n" + "=" * 70)
    print(" CHIRKOOT MK-V FASTAPI & WEBSOCKET SECURITY VERIFICATION SUITE")
    print("=" * 70)

    server_proc = start_server_process(SERVER_PORT)
    passed_tests = 0
    total_tests = 9

    try:
        # Step 0: Obtain launch auth token from HTML or token endpoint
        token = None
        req = urllib.request.Request(f"{HTTP_URL}/", headers={"Origin": "http://127.0.0.1:8000"})
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8")
            assert 'name="chirkoot-auth-token"' in html, "HTML must contain chirkoot-auth-token meta tag"
            import re
            m = re.search(r'name="chirkoot-auth-token"\s+content="([^"]+)"', html)
            assert m, "Auth token meta tag must contain non-empty content"
            token = m.group(1)
            assert len(token) >= 32, "Auth token must be at least 32 characters"
        print(f"[*] Ephemeral launch auth token delivered via HTML meta: {token[:6]}... (entropy: {len(token)*6} bits)")

        # Verify HTTP endpoint security
        # Test HTTP CSRF / Cross-site protection
        csrf_req = urllib.request.Request(
            f"{HTTP_URL}/api/kill-switch",
            data=b"",
            headers={"Origin": "https://evil-attacker.com", "Sec-Fetch-Site": "cross-site"},
            method="POST"
        )
        try:
            urllib.request.urlopen(csrf_req)
            raise AssertionError("Cross-site request to /api/kill-switch should be rejected")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403 Forbidden for cross-site HTTP request, got {e.code}"
        print("  [PASS] HTTP API cross-site CSRF rejection verified (403 Forbidden)")

        # -------------------------------------------------------------
        # TEST 1: Malicious website attempting localhost connection
        # -------------------------------------------------------------
        t0 = time.time()
        rejected = False
        try:
            async with websockets.connect(
                f"{WS_URL}?token={token}",
                additional_headers={"Origin": "https://evil-attacker.com"},
                subprotocols=["chirkoot", token]
            ) as ws:
                await ws.recv()
        except Exception as e:
            rejected = True
        assert rejected, "Malicious website Origin must be rejected"
        passed_tests += 1
        print(f"  [PASS] 1. Malicious website attempting localhost connection ({(time.time()-t0)*1000:.2f}ms) - Origin: https://evil-attacker.com -> REJECTED")

        # -------------------------------------------------------------
        # TEST 2: Missing Origin
        # -------------------------------------------------------------
        t0 = time.time()
        rejected = False
        try:
            # websockets defaults to omitting Origin if not specified
            async with websockets.connect(
                f"{WS_URL}?token={token}",
                subprotocols=["chirkoot", token]
            ) as ws:
                await ws.recv()
        except Exception as e:
            rejected = True
        assert rejected, "Missing Origin must be rejected"
        passed_tests += 1
        print(f"  [PASS] 2. Missing Origin ({(time.time()-t0)*1000:.2f}ms) - Origin: None -> REJECTED")

        # -------------------------------------------------------------
        # TEST 3: Invalid Origin
        # -------------------------------------------------------------
        t0 = time.time()
        rejected = False
        try:
            async with websockets.connect(
                f"{WS_URL}?token={token}",
                additional_headers={"Origin": "http://malicious-site.org:8080"}
            ) as ws:
                await ws.recv()
        except Exception as e:
            rejected = True
        assert rejected, "Invalid Origin http://malicious-site.org:8080 must be rejected"
        passed_tests += 1
        print(f"  [PASS] 3. Invalid Origin ({(time.time()-t0)*1000:.2f}ms) - Origin: http://malicious-site.org:8080 -> REJECTED")

        # -------------------------------------------------------------
        # TEST 4: Missing token
        # -------------------------------------------------------------
        t0 = time.time()
        rejected = False
        try:
            async with websockets.connect(
                WS_URL,
                additional_headers={"Origin": "http://127.0.0.1:8000"}
            ) as ws:
                await ws.recv()
        except Exception as e:
            rejected = True
        assert rejected, "Connection with missing token must be rejected"
        passed_tests += 1
        print(f"  [PASS] 4. Missing token ({(time.time()-t0)*1000:.2f}ms) - Token: None -> REJECTED (Unauthorized Token)")

        # -------------------------------------------------------------
        # TEST 5: Incorrect token
        # -------------------------------------------------------------
        t0 = time.time()
        rejected = False
        try:
            async with websockets.connect(
                f"{WS_URL}?token=forged_unauthorized_token_xyz",
                additional_headers={"Origin": "http://127.0.0.1:8000"}
            ) as ws:
                await ws.recv()
        except Exception as e:
            rejected = True
        assert rejected, "Connection with forged token must be rejected"
        passed_tests += 1
        print(f"  [PASS] 5. Incorrect token ({(time.time()-t0)*1000:.2f}ms) - Token: forged_xyz -> REJECTED (Unauthorized Token)")

        # -------------------------------------------------------------
        # TEST 6: Duplicate client (Single client session gatekeeper)
        # -------------------------------------------------------------
        t0 = time.time()
        async with websockets.connect(
            WS_URL,
            additional_headers={"Origin": "http://127.0.0.1:8000"},
            subprotocols=["chirkoot", token]
        ) as ws_primary:
            # Client 1 is connected and active. Send auth frame / ping to verify alive
            await ws_primary.send(json.dumps({"type": "ping"}))
            resp = json.loads(await ws_primary.recv())
            assert resp.get("type") == "pong"

            # Client 2 attempts to connect concurrently
            client2_rejected = False
            try:
                async with websockets.connect(
                    WS_URL,
                    additional_headers={"Origin": "http://127.0.0.1:8000"},
                    subprotocols=["chirkoot", token]
                ) as ws_secondary:
                    await ws_secondary.recv()
            except Exception as e:
                client2_rejected = True
            assert client2_rejected, "Duplicate client connection must be rejected while primary is connected"

        # Now that ws_primary is closed, subsequent client connection should be permitted
        await asyncio.sleep(0.2)
        async with websockets.connect(
            WS_URL,
            additional_headers={"Origin": "http://127.0.0.1:8000"},
            subprotocols=["chirkoot", token]
        ) as ws_reconnected:
            await ws_reconnected.send(json.dumps({"type": "ping"}))
            resp = json.loads(await ws_reconnected.recv())
            assert resp.get("type") == "pong"

        passed_tests += 1
        print(f"  [PASS] 6. Duplicate client concurrency lock ({(time.time()-t0)*1000:.2f}ms) - Single client active enforced; released on disconnect")

        # -------------------------------------------------------------
        # TEST 7: Oversized payload and malformed messages
        # -------------------------------------------------------------
        t0 = time.time()
        async with websockets.connect(
            WS_URL,
            additional_headers={"Origin": "http://127.0.0.1:8000"},
            subprotocols=["chirkoot", token]
        ) as ws:
            # 7a: Oversized text (> 64 KB)
            oversized_text = "A" * 70000
            await ws.send(json.dumps({"type": "text", "text": oversized_text}))
            err_resp = json.loads(await ws.recv())
            assert err_resp.get("type") == "error"
            assert "exceeds maximum" in err_resp.get("message", "").lower() or "limit" in err_resp.get("message", "").lower()

            # 7b: Oversized binary (> 1 MB)
            oversized_binary = b"\x00" * (1024 * 1024 + 1024)
            await ws.send(oversized_binary)
            err_resp_bin = json.loads(await ws.recv())
            assert err_resp_bin.get("type") == "error"
            assert "binary payload exceeds" in err_resp_bin.get("message", "").lower()

            # 7c: Malformed non-JSON payload
            await ws.send("This is not valid JSON {{{{")
            err_resp_json = json.loads(await ws.recv())
            assert err_resp_json.get("type") == "error"
            assert "malformed" in err_resp_json.get("message", "").lower()

            # 7d: Non-dict JSON payload
            await ws.send("[1, 2, 3, 4]")
            err_resp_arr = json.loads(await ws.recv())
            assert err_resp_arr.get("type") == "error"
            assert "expected json object" in err_resp_arr.get("message", "").lower()

            # 7e: Unexpected message type
            await ws.send(json.dumps({"type": "unauthorized_malicious_type"}))
            err_resp_type = json.loads(await ws.recv())
            assert err_resp_type.get("type") == "error"
            assert "unexpected message type" in err_resp_type.get("message", "").lower()

        passed_tests += 1
        print(f"  [PASS] 7. Oversized & malformed payload rejection ({(time.time()-t0)*1000:.2f}ms) - Text >64KB, Binary >1MB, Non-JSON, Non-Dict safely rejected")

        # -------------------------------------------------------------
        # TEST 8: Rapid connection attempts (Rate limiter enforcement)
        # -------------------------------------------------------------
        t0 = time.time()
        rate_limit_triggered = False
        for i in range(25):
            try:
                async with websockets.connect(
                    WS_URL,
                    additional_headers={"Origin": "http://127.0.0.1:8000"},
                    subprotocols=["chirkoot", token]
                ) as ws_flood:
                    pass
            except Exception:
                rate_limit_triggered = True
                break
        assert rate_limit_triggered, "Rate limiter must trigger on 25 rapid consecutive connections"
        passed_tests += 1
        print(f"  [PASS] 8. Rapid connection rate limiting ({(time.time()-t0)*1000:.2f}ms) - Connection flood triggered rate limiter rejection")

        # -------------------------------------------------------------
        # TEST 9: Backend restart (Token rotation and invalidation)
        # -------------------------------------------------------------
        t0 = time.time()
        # Save old token
        old_token = token
        # Stop initial server instance
        stop_server_process(server_proc)
        await asyncio.sleep(1.0)

        # Launch fresh server instance on different or same port
        server_proc = start_server_process(SERVER_PORT)
        await asyncio.sleep(0.5)

        # 9a: Attempt connection using OLD token from prior session
        old_token_rejected = False
        try:
            async with websockets.connect(
                WS_URL,
                additional_headers={"Origin": "http://127.0.0.1:8000"},
                subprotocols=["chirkoot", old_token]
            ) as ws_old:
                await ws_old.recv()
        except Exception:
            old_token_rejected = True
        assert old_token_rejected, "Old auth token from prior server session must be rejected after restart"

        # 9b: Obtain new token from restarted server instance
        req = urllib.request.Request(f"{HTTP_URL}/", headers={"Origin": "http://127.0.0.1:8000"})
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8")
            import re
            m = re.search(r'name="chirkoot-auth-token"\s+content="([^"]+)"', html)
            new_token = m.group(1)
        assert new_token != old_token, "New session token must differ from old token"

        # 9c: Connect with new token
        async with websockets.connect(
            WS_URL,
            additional_headers={"Origin": "http://127.0.0.1:8000"},
            subprotocols=["chirkoot", new_token]
        ) as ws_new:
            await ws_new.send(json.dumps({"type": "ping"}))
            resp = json.loads(await ws_new.recv())
            assert resp.get("type") == "pong"

        passed_tests += 1
        print(f"  [PASS] 9. Backend restart & token rotation ({(time.time()-t0)*1000:.2f}ms) - Old token invalidated, fresh token verified")

    finally:
        stop_server_process(server_proc)

    print("\n" + "=" * 70)
    print(f" SERVER SECURITY TEST SUITE SUMMARY: {passed_tests}/{total_tests} PASSED | 0 FAILED")
    print("=" * 70 + "\n")

if __name__ == "__main__":
    asyncio.run(run_server_security_tests())
