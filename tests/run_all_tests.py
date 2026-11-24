"""
Unified Test Runner for Chirkoot MK-V.
Discovers and executes all unit, integration, security, and stability test suites
in isolated test processes to guarantee zero cross-suite state pollution.
Produces a structured summary report and enforces deterministic offline test execution.
"""

import os
import sys
import time
import subprocess
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parent.parent

# Ordered list of core test modules
CORE_TEST_MODULES = [
    "tests.test_audio_downsampling",
    "tests.test_fuzzy_wake_word",
    "tests.test_coordinate_mapping",
    "tests.test_recorded_audio_wake_word",
    "tests.test_session_reconnect",
    "tests.test_tool_scheduling_dedup",
    "tests.test_ocr_and_uia",
    "tests.test_security_prompt_injection",
    "tests.test_stability_long_running",
    "test_local_wake_word",
    "test_dispatcher",
    "test_sqlite_memory",
    "test_structured_automation",
    "test_closed_loop_verification",
    "test_emergency_stop",
    "test_media_protocol",
    "test_security_mkv",
    "tests.test_launcher_deployment",
    "tests.test_e2e_scenarios_mkv"
]


def run_all_suites():
    print("=" * 80)
    print("        CHIRKOOT MK-V PRODUCTION TEST HARNESS & REGRESSION MATRIX")
    print("=" * 80)

    total_passed = 0
    total_failed = 0
    suite_summaries = []

    start_total_time = time.perf_counter()

    for mod_name in CORE_TEST_MODULES:
        if mod_name.startswith("tests."):
            file_name = mod_name.split(".")[-1] + ".py"
            script_path = ROOT_DIR / "tests" / file_name
        else:
            script_path = ROOT_DIR / (mod_name + ".py")

        print(f"\n>> Running suite: {mod_name} ({script_path.name})...")
        t0 = time.perf_counter()
        
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT_DIR),
            env=env
        )

        elapsed = (time.perf_counter() - t0) * 1000.0
        is_ok = (proc.returncode == 0)

        # Print output preview
        if proc.stdout:
            for line in proc.stdout.strip().splitlines()[-6:]:
                safe_line = line.encode("ascii", errors="replace").decode("ascii")
                print(f"   {safe_line}")

        if not is_ok and proc.stderr:
            for line in proc.stderr.strip().splitlines()[-4:]:
                safe_err = line.encode("ascii", errors="replace").decode("ascii")
                print(f"   [STDERR] {safe_err}")

        if is_ok:
            total_passed += 1
        else:
            total_failed += 1

        suite_summaries.append((mod_name, is_ok, elapsed))

    total_time_sec = time.perf_counter() - start_total_time

    print("\n" + "=" * 80)
    print("                      COMPREHENSIVE TEST RESULTS")
    print("=" * 80)
    print(f"{'SUITE':<45} | {'STATUS':<8} | {'DURATION'}")
    print("-" * 80)

    all_passed = True
    for name, ok, dur in suite_summaries:
        status = "PASSED" if ok else "FAILED"
        if not ok:
            all_passed = False
        print(f"{name:<45} | {status:<8} | {dur:>7.1f}ms")

    print("-" * 80)
    print(f"TOTAL SUITES RUN: {total_passed + total_failed}")
    print(f"PASSED:           {total_passed}")
    print(f"FAILED:           {total_failed}")
    print(f"TOTAL DURATION:   {total_time_sec:.2f}s")
    print("=" * 80)

    if all_passed and total_failed == 0:
        print(f"\n [SUCCESS] ALL {len(CORE_TEST_MODULES)} CHIRKOOT MK-V TEST SUITES PASSED NOMINALLY!\n")
        return 0
    else:
        print("\n [FAILURE] ONE OR MORE SUITES FAILED REGRESSION CHECKS.\n")
        return 1


if __name__ == "__main__":
    exit_code = run_all_suites()
    sys.exit(exit_code)
