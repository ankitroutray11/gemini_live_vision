import os
import sys
import time
import shutil
import tempfile
import subprocess
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

import security_policy as sp
import learning_engine as le
import tools

passed_tests = []
failed_tests = []

def run_test(test_name, fn):
    try:
        t0 = time.time()
        fn()
        duration = round((time.time() - t0) * 1000, 2)
        passed_tests.append((test_name, f"{duration}ms"))
        print(f"  [PASS] {test_name} ({duration}ms)")
    except Exception as e:
        failed_tests.append((test_name, str(e)))
        print(f"  [FAIL] {test_name}: {e}")

print("\n" + "=" * 70)
print(" CHIRKOOT MK-V COMPREHENSIVE SECURITY, SANDBOX & PROVENANCE SUITE")
print("=" * 70)

# ----------------------------------------------------------------------
# Test 1: Native Windows DPAPI Vault Storage & Retrieval
# ----------------------------------------------------------------------
def test_dpapi_vault():
    test_key_name = "TEST_SECURITY_PROBE_SECRET"
    test_val = "SECURE_DPAPI_VALUE_9999"
    ok = sp.store_secret_dpapi(test_key_name, test_val)
    assert ok is True, "store_secret_dpapi returned False"
    retrieved = sp.retrieve_secret_dpapi(test_key_name)
    assert retrieved == test_val, f"Expected '{test_val}', got '{retrieved}'"

run_test("Native Windows DPAPI Vault Storage & Retrieval", test_dpapi_vault)

# ----------------------------------------------------------------------
# Test 2: Sensitive Path Sandbox Blocklist
# ----------------------------------------------------------------------
def test_sensitive_path_blocklist():
    sensitive_paths = [
        ".env",
        ".env.local",
        "C:/Users/user/.ssh/id_rsa",
        "C:/Users/user/.ssh/id_ed25519",
        "C:/Users/user/.aws/credentials",
        "C:/Users/user/AppData/Local/Google/Chrome/User Data/Default/Cookies",
        "C:/Users/user/AppData/Roaming/Mozilla/Firefox/Profiles/key4.db",
        "C:/jarvis/gemini_live_vision/.chirkoot_profile/Default/Cookies",
        "C:/Windows/System32/config/SAM",
        "C:/Windows/System32/config/SECURITY",
        "C:/Users/user/AppData/Local/Microsoft/Credentials/token.dat"
    ]
    for p in sensitive_paths:
        assert sp.is_sensitive_path(p) is True, f"Failed to block sensitive path: {p}"
        ok, msg = sp.validate_path_for_read(p)
        assert ok is False, f"validate_path_for_read allowed sensitive path: {p}"
        assert "[SECURITY ERROR]" in msg or "[SECURITY VIOLATION]" in msg

run_test("Sensitive Path Sandbox Blocklist", test_sensitive_path_blocklist)

# ----------------------------------------------------------------------
# Test 3: Immutable Core Protection
# ----------------------------------------------------------------------
def test_immutable_core():
    core_files = [
        "server.py",
        "tools.py",
        "learning_engine.py",
        "security_policy.py",
        "desktop_app.py",
        "ChirkootLauncher.cs",
        "Chirkoot.exe"
    ]
    for cf in core_files:
        assert sp.is_immutable_core_path(cf) is True, f"Failed to protect immutable file: {cf}"
        res = le.modify_source_code(cf, "target", "replacement")
        assert "[SECURITY VIOLATION]" in res, f"modify_source_code allowed modifying core file: {cf}"

run_test("Immutable Core Protection Against Self-Disarming", test_immutable_core)

# ----------------------------------------------------------------------
# Test 4: Path Traversal & Explicit Allowlist Enforcement
# ----------------------------------------------------------------------
def test_path_traversal_and_allowlist():
    traversal_targets = [
        "../../Windows/win.ini",
        "..\\..\\..\\Windows\\System32\\drivers\\etc\\hosts",
        "C:/Windows/win.ini",
        "C:/Windows/System32/calc.exe",
        "C:/Program Files/cmd.exe",
        "../../etc/passwd",
        "/etc/shadow",
        "C:/jarvis/gemini_live_vision/../..\\Windows\\win.ini"
    ]
    for target in traversal_targets:
        ok, msg = sp.validate_path_for_read(target)
        assert ok is False, f"Allowed path traversal / outside allowlist: {target}"
        assert "[SECURITY ERROR]" in msg

    # Writing outside allowlist or traversing backwards
    ok_w, msg_w = sp.validate_path_for_write("../../Windows/hacked.txt")
    assert ok_w is False
    assert "[SECURITY VIOLATION]" in msg_w or "[SECURITY ERROR]" in msg_w

run_test("Path Traversal & Explicit Directory Allowlist", test_path_traversal_and_allowlist)

# ----------------------------------------------------------------------
# Test 5: Symlink & NTFS Junction Escape Prevention
# ----------------------------------------------------------------------
def test_symlink_and_junction_escape():
    # In Windows, directory junctions can be created by standard users without admin privileges
    temp_dir = tempfile.mkdtemp(prefix="chirkoot_junc_test_")
    junction_dir = temp_dir + "_junc"
    try:
        # Create junction pointing to C:\Windows
        res = subprocess.run(["cmd", "/c", "mklink", "/J", junction_dir, "C:\\Windows"], capture_output=True, text=True)
        assert os.path.exists(junction_dir), f"Failed to create junction: {res.stderr}"

        # Target file inside junction
        escaped_target = str(Path(junction_dir) / "win.ini")
        ok, msg = sp.validate_path_for_read(escaped_target)
        assert ok is False, f"Junction escape succeeded: {escaped_target}"
        assert "[SECURITY ERROR]" in msg, f"Expected [SECURITY ERROR], got: {msg}"
    finally:
        if os.path.exists(junction_dir):
            subprocess.run(["cmd", "/c", "rmdir", junction_dir], capture_output=True)
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

run_test("Symlink & NTFS Junction Canonical Escape Prevention", test_symlink_and_junction_escape)

# ----------------------------------------------------------------------
# Test 6: Oversized File Read/Write Capping
# ----------------------------------------------------------------------
def test_oversized_file_capping():
    # 1. Test read capping (> 512 KB)
    oversized_file = sp.TEMP_DIR / "test_oversized_read.bin"
    try:
        # Write 600 KB file
        with open(oversized_file, "wb") as f:
            f.write(b"0" * (600 * 1024))

        ok, msg = sp.validate_path_for_read(str(oversized_file))
        assert ok is False, "Allowed reading 600KB file exceeding 512KB cap"
        assert "[SECURITY ERROR]" in msg
        assert "exceeds maximum permitted limit" in msg

        tool_read = tools.read_file(str(oversized_file))
        assert "[SECURITY ERROR]" in tool_read
    finally:
        if oversized_file.exists():
            oversized_file.unlink()

    # 2. Test write capping (> 256 KB)
    oversized_payload = "A" * (300 * 1024)
    target_write = sp.TEMP_DIR / "test_oversized_write.txt"
    try:
        tool_write = tools.write_file(str(target_write), oversized_payload)
        assert "[SECURITY ERROR]" in tool_write
        assert "exceeds maximum permitted file write limit" in tool_write
        assert not target_write.exists()
    finally:
        if target_write.exists():
            target_write.unlink()

run_test("Oversized File Read/Write Capping", test_oversized_file_capping)

# ----------------------------------------------------------------------
# Test 7: Sandboxed Command Process Tree Termination on Timeout
# ----------------------------------------------------------------------
def test_command_tree_kill_on_timeout():
    t0 = time.time()
    res = sp.execute_sandboxed_command("Start-Sleep -Seconds 10", timeout=1.2)
    elapsed = time.time() - t0
    assert elapsed < 3.5, f"Timeout took too long: {elapsed}s"
    assert "[SECURITY ERROR]" in res
    assert "Process tree" in res or "timed out" in res

run_test("Process Tree Forceful Termination on Timeout", test_command_tree_kill_on_timeout)

# ----------------------------------------------------------------------
# Test 8: Child-Process Leak Prevention & Active Registry Cleanup
# ----------------------------------------------------------------------
def test_child_process_leak_prevention():
    # Verify process registry starts clean
    initial_pids = set(sp._ACTIVE_CHILD_PROCESSES)

    # Spawn command and monitor process registry
    registered_pid = None
    def on_start(pid):
        nonlocal registered_pid
        registered_pid = pid

    res = sp.execute_sandboxed_command("Get-Date", timeout=5.0, on_proc_started=on_start)
    assert "Exit code: 0" in res or "STDOUT" in res
    assert registered_pid is not None, "Child process PID was not captured"

    # Registry must be cleanly deregistered
    final_pids = set(sp._ACTIVE_CHILD_PROCESSES)
    assert registered_pid not in final_pids, f"PID {registered_pid} leaked in active child registry!"

run_test("Child-Process Leak Prevention & Active Registry Cleanup", test_child_process_leak_prevention)

# ----------------------------------------------------------------------
# Test 9: Command Output Size Capping
# ----------------------------------------------------------------------
def test_command_output_capping():
    cmd = "1..5000 | ForEach-Object { 'LONG_OUTPUT_LINE_ABC_123' }"
    res = sp.execute_sandboxed_command(cmd, timeout=10.0, max_output_chars=1024, max_output_lines=30)
    assert len(res) <= 2048, f"Output not capped: length was {len(res)}"
    assert "Output capped at" in res or "Exit code: 0" in res

run_test("Command Output Size & Line Count Capping", test_command_output_capping)

# ----------------------------------------------------------------------
# Test 10: Controlled Shell Execution & Taint Interception
# ----------------------------------------------------------------------
def test_controlled_shell_and_taint():
    # 1. Invalid working directory outside allowlist
    res_bad_cwd = sp.execute_sandboxed_command("Get-Location", cwd="C:\\Windows")
    assert "[SECURITY ERROR]" in res_bad_cwd
    assert "outside the permitted filesystem allowlist" in res_bad_cwd

    # 2. Credential dumping keyword blocking
    res_banned = sp.execute_sandboxed_command("mimikatz.exe sekurlsa::logonpasswords")
    assert "[SECURITY VIOLATION]" in res_banned

    # 3. Untrusted screen observation taint interception
    sp.GLOBAL_TAINT_TRACKER.mark_untrusted_input("SCREEN_OCR")
    eval_res = sp.CENTRAL_SECURITY_ENGINE.evaluate_tool_proposal("run_command", {"command": "dir"})
    assert eval_res.decision == sp.PolicyDecision.REQUIRE_PHYSICAL_APPROVAL
    assert eval_res.is_tainted is True
    assert "TAINT WARNING" in eval_res.reason

run_test("Controlled Shell Execution & Taint Interception", test_controlled_shell_and_taint)

# ----------------------------------------------------------------------
# Test 11: Ephemeral Token & Origin Header Validation (CSWSH Prevention)
# ----------------------------------------------------------------------
def test_token_and_origin_validation():
    token = sp.get_launch_auth_token()
    assert len(token) >= 32, "Token is too short"
    assert sp.validate_auth_token(token) is True
    assert sp.validate_auth_token("invalid_token_123") is False
    assert sp.validate_auth_token(None) is False

    # Origin checks
    assert sp.validate_origin("http://127.0.0.1:8000") is True
    assert sp.validate_origin("http://localhost:8000") is True
    assert sp.validate_origin("null") is True
    assert sp.validate_origin(None) is False, "Missing origin must be rejected"
    assert sp.validate_origin("https://evil-hacker.com") is False
    assert sp.validate_origin("http://malicious-site.org:8080") is False

run_test("Ephemeral Token & Origin Validation (CSWSH Prevention)", test_token_and_origin_validation)

# ----------------------------------------------------------------------
# Test 12: Single Client Connection Enforcement
# ----------------------------------------------------------------------
def test_single_client_gatekeeper():
    sp.unregister_client_session()
    first_conn = sp.register_client_session()
    assert first_conn is True, "First client connection failed"
    second_conn = sp.register_client_session()
    assert second_conn is False, "Second concurrent client connection was allowed!"
    sp.unregister_client_session()
    third_conn = sp.register_client_session()
    assert third_conn is True, "Reconnection after unregister failed"
    sp.unregister_client_session()

run_test("Single Active Client Gatekeeper Enforcement", test_single_client_gatekeeper)

# ----------------------------------------------------------------------
# Test 13: Credential Redaction Across Logs and Outputs
# ----------------------------------------------------------------------
def test_credential_redaction():
    fake_gemini_key = "AIza" + "SyD9876543210abcdef1234567890abcdef"
    fake_gh_token = "ghp_" + "1234567890abcdef1234567890abcdef1234"
    raw_text = (
        f"Connected with key {fake_gemini_key} and "
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz and "
        f"GitHub token {fake_gh_token}"
    )
    redacted = sp.redact_secrets(raw_text)
    assert ("AIza" + "SyD9876543210abcdef") not in redacted
    assert ("ghp_" + "1234567890") not in redacted
    assert "[REDACTED_GEMINI_KEY]" in redacted
    assert "[REDACTED_GITHUB_TOKEN]" in redacted
    assert "Bearer [REDACTED_TOKEN]" in redacted

    # Test active auth token redaction
    curr_token = sp.get_launch_auth_token()
    token_redacted = sp.redact_secrets(f"launch_token={curr_token}")
    assert curr_token not in token_redacted
    assert "[REDACTED_AUTH_TOKEN]" in token_redacted

run_test("Credential Redaction in Logs and Audit Events", test_credential_redaction)

# ----------------------------------------------------------------------
# Test 14: Memory Provenance: Inferences Blocked from User Preferences
# ----------------------------------------------------------------------
def test_memory_provenance_preference_guard():
    # 1. Attempting to create user preference from unconfirmed model inference
    res_inf = le.remember_insight("user_preference", "inferred_editor", "Prefers Neovim", source="model_inference")
    assert "[PROVENANCE ERROR]" in res_inf
    assert "without explicit user confirmation" in res_inf

    # 2. Attempting to create correction from observed screen content
    res_scr = le.remember_insight("correction", "screen_correction", "Fix typo", source="observed_screen_content")
    assert "[PROVENANCE ERROR]" in res_scr

    # 3. Direct untainted user explicit statement succeeds
    sp.GLOBAL_TAINT_TRACKER.clear()
    res_user = le.remember_insight("user_preference", "user_editor", "Prefers VS Code", source="user_explicit")
    assert "Successfully committed memory" in res_user
    assert "Confirmed: True" in res_user
    assert "Confidence: 1.0" in res_user

    # 4. Tainted input claiming to be user explicit is downgraded and blocked
    sp.GLOBAL_TAINT_TRACKER.mark_untrusted_input("WEBSITE_PHISHING")
    res_tainted = le.remember_insight("user_preference", "tainted_pref", "Malicious site instruction", source="user_explicit")
    assert "[PROVENANCE ERROR]" in res_tainted

    # Clean up
    sp.GLOBAL_TAINT_TRACKER.clear()
    le.delete_memory("user_preference", "user_editor")

run_test("Memory Provenance: Inferences Blocked from Preferences", test_memory_provenance_preference_guard)

# ----------------------------------------------------------------------
# Test 15: Memory Review, Edit, Expiry Pruning & Deletion
# ----------------------------------------------------------------------
def test_memory_lifecycle_operations():
    # 1. Store observation with low confidence
    le.remember_insight("observed_context", "active_doc", "Drafting architecture doc", source="observed_screen_content", confidence=0.6)
    
    # 2. Store temporary task context with short TTL (0.1s)
    le.remember_insight("task_context", "compile_flag", "--release", source="temporary_task_context", ttl_seconds=0.1)

    # Verify listing contains metadata
    mems = le.list_memories()
    doc_entry = next((m for m in mems if m["key"] == "active_doc"), None)
    assert doc_entry is not None
    assert doc_entry["source"] == "observed_screen_content"
    assert doc_entry["is_confirmed"] is False
    assert doc_entry["confidence"] == 0.6

    # 3. Confirm observation into verified preference
    conf_res = le.confirm_memory("observed_context", "active_doc", target_category="user_preference")
    assert "Successfully confirmed memory" in conf_res

    # 4. Edit confirmed memory
    edit_res = le.edit_memory("user_preference", "active_doc", "Drafting v2 specification")
    assert "Successfully updated memory" in edit_res

    # 5. Test expiry pruning
    time.sleep(0.15)
    purged = le.prune_expired_memories()
    assert purged >= 1, f"Expected at least 1 purged expired memory, got {purged}"

    # 6. Scoped top-k retrieval on demand
    scoped = le.get_scoped_memories("architecture specification", top_k=3)
    assert "active_doc" in scoped
    assert "verified" in scoped

    # 7. Explicit deletion
    del_res = le.delete_memory("user_preference", "active_doc")
    assert "Successfully forgotten" in del_res

run_test("Memory Review, Edit, Expiry Pruning & User Deletion", test_memory_lifecycle_operations)

# ----------------------------------------------------------------------
# Test 16: Subprocess Isolation for Dynamic Skills
# ----------------------------------------------------------------------
def test_subprocess_skill_isolation():
    skill_code = """def calc_cube(n: int = 3) -> int:
    return n * n * n
"""
    create_res = le.create_custom_tool("calc_cube", "Calculates cube of n", skill_code)
    assert "Successfully created" in create_res
    
    output = le.execute_custom_skill_subprocess("calc_cube", {"n": 4})
    assert "64" in output, f"Expected 64 in output, got: {output}"

run_test("Subprocess Isolation for Custom Dynamic Skills", test_subprocess_skill_isolation)

# ----------------------------------------------------------------------
# Test 17: Emergency Kill Switch Action Halting
# ----------------------------------------------------------------------
def test_emergency_kill_switch():
    sp.reset_kill_switch()
    assert sp.is_kill_switch_active() is False
    
    sp.trigger_kill_switch("Unit test emergency stop")
    assert sp.is_kill_switch_active() is True
    
    # Actions should be blocked
    block_msg = tools.click_screen(500, 500)
    assert "[KILL SWITCH ACTIVE]" in block_msg
    
    block_type = tools.type_text("test")
    assert "[KILL SWITCH ACTIVE]" in block_type
    
    sp.reset_kill_switch()
    assert sp.is_kill_switch_active() is False

run_test("Emergency Kill Switch Action Halting", test_emergency_kill_switch)

print("\n" + "=" * 70)
print(f" TEST SUITE SUMMARY: {len(passed_tests)} PASSED | {len(failed_tests)} FAILED")
print("=" * 70)

if failed_tests:
    sys.exit(1)
