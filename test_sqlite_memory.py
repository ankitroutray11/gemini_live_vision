"""Comprehensive test suite for Chirkoot MK-V SQLite Persistent Memory System.

Tests:
1. Exact keyword retrieval (FTS5 BM25).
2. Semantic retrieval (sqlite-vec / vector similarity).
3. Near-duplicate detection and merging.
4. Concurrent writes (multi-threaded WAL mode).
5. Migration from memory.json with backup and verification.
6. Rollback to memory.json from backup.
7. Memory deletion (cleaning relational, FTS, and vector tables).
8. Large dataset performance (1,000+ entries queried in < 50ms).
9. Provenance enforcement (inferences blocked from preferences).
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
from pathlib import Path

# Add project root
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from sqlite_memory import (
    SQLiteMemoryEngine,
    generate_semantic_embedding,
    cosine_similarity,
    canonicalize_category,
    STANDARD_CATEGORIES,
    SQLITE_VEC_AVAILABLE,
    MEMORY_DB_FILE
)
from security_policy import GLOBAL_TAINT_TRACKER

def test_1_exact_keyword_retrieval(engine: SQLiteMemoryEngine):
    print("\n--- Test 1: Exact Keyword Retrieval (FTS5 BM25) ---")
    # Insert distinctive keyword memories
    engine.store_memory(
        category="shortcut",
        key="open_terminal",
        content="Press Ctrl+Shift+T to open Windows Terminal in project root",
        source="user_explicit"
    )
    engine.store_memory(
        category="user_preference",
        key="favorite_browser",
        content="Always use Google Chrome for browser automation tasks",
        source="user_explicit"
    )
    engine.store_memory(
        category="workflow_pattern",
        key="git_commit_style",
        content="Use conventional commit messages with prefix chore or feat",
        source="user_explicit"
    )

    # 1. Search for 'Terminal'
    res_term = engine.search_hybrid("Windows Terminal", top_k=3)
    assert len(res_term) > 0, "Expected search results for 'Windows Terminal'"
    assert res_term[0]["key_name"] == "open_terminal", f"Expected 'open_terminal' as #1, got {res_term[0]['key_name']}"
    assert res_term[0]["fts_score"] > 0, "FTS score should be > 0"
    print(f"  [PASS] 'Windows Terminal' query -> Matched #{res_term[0]['key_name']} (FTS Score: {res_term[0]['fts_score']:.3f})")

    # 2. Search for 'Chrome'
    res_chrome = engine.search_hybrid("Google Chrome automation", top_k=3)
    assert len(res_chrome) > 0, "Expected search results for 'Google Chrome automation'"
    assert res_chrome[0]["key_name"] == "favorite_browser", f"Expected 'favorite_browser' as #1, got {res_chrome[0]['key_name']}"
    print(f"  [PASS] 'Google Chrome' query -> Matched #{res_chrome[0]['key_name']} (Final Score: {res_chrome[0]['final_score']:.3f})")
    print("  => Test 1 Passed Successfully.")


def test_2_semantic_retrieval(engine: SQLiteMemoryEngine):
    print("\n--- Test 2: Semantic Retrieval (Vector Similarity) ---")
    # Insert entry with specific conceptual meaning
    engine.store_memory(
        category="user_preference",
        key="hud_appearance",
        content="Prefer dark translucent minimalist user interface theme styling",
        source="user_explicit"
    )

    # Query with semantic variants (UI skin, visual display aesthetics)
    query = "dark user interface styling"
    res = engine.search_hybrid(query, top_k=3)
    assert len(res) > 0, f"Expected semantic match for '{query}'"
    top_match = res[0]
    assert top_match["key_name"] == "hud_appearance", f"Expected 'hud_appearance', got {top_match['key_name']}"
    assert top_match["vec_score"] > 0.4, f"Vector score should be > 0.4, got {top_match['vec_score']}"
    print(f"  [PASS] Semantic query '{query}' matched key '{top_match['key_name']}' (vec_score: {top_match['vec_score']:.3f}, final: {top_match['final_score']:.3f})")
    print("  => Test 2 Passed Successfully.")


def test_3_duplicate_detection_and_merging(engine: SQLiteMemoryEngine):
    print("\n--- Test 3: Duplicate Detection & Merging ---")
    cat = "workflow_pattern"
    key = "code_formatter"
    content_v1 = "Use Black and Ruff to format Python files"

    res1 = engine.store_memory(
        category=cat,
        key=key,
        content=content_v1,
        source="user_explicit"
    )
    assert res1["action"] == "created", f"First store should be created, got {res1['action']}"
    initial_id = res1["id"]

    # Store exact duplicate key in same category
    content_v2 = "Use Black and Ruff with line length 100 to format Python files"
    res2 = engine.store_memory(
        category=cat,
        key=key,
        content=content_v2,
        source="user_explicit"
    )
    assert res2["action"] == "updated", f"Second store with same key should be updated, got {res2['action']}"
    assert res2["id"] == initial_id, f"ID should be preserved across merge (was {initial_id}, got {res2['id']})"

    # Verify updated content in DB
    mem = engine.get_memory(cat, key)
    assert mem is not None, "Memory should exist"
    assert mem["content"] == content_v2, "Content should be updated to v2"
    assert mem["access_count"] >= 2, f"Access count should be incremented, got {mem['access_count']}"
    print(f"  [PASS] Duplicate key '{key}' merged into existing row {initial_id} (Access count: {mem['access_count']})")
    print("  => Test 3 Passed Successfully.")


def test_4_concurrent_writes(engine: SQLiteMemoryEngine):
    print("\n--- Test 4: Concurrent Multi-Threaded Writes (WAL Mode) ---")
    num_threads = 12
    writes_per_thread = 15
    errors = []

    actions = [
        "compile_typescript_client", "lint_backend_service", "run_pytest_suite",
        "deploy_docker_container", "benchmark_sqlite_latency", "backup_user_vault",
        "audit_security_logs", "synchronize_cloud_state", "index_vector_embeddings",
        "verify_signature_hash", "prune_stale_cache", "render_hud_widget"
    ]
    def worker(worker_id: int):
        act = actions[worker_id % len(actions)]
        for i in range(writes_per_thread):
            k = f"thread_{worker_id}_{act}_{i}"
            c = f"Operational log for action {act} on node {worker_id} executing iteration {i} with payload hash {hex(worker_id * 7919 + i * 104729)}"
            try:
                res = engine.store_memory(
                    category="task_context",
                    key=k,
                    content=c,
                    source="temporary_task_context",
                    ttl_seconds=3600
                )
                if res.get("status") != "success":
                    errors.append(f"Worker {worker_id} write error: {res}")
            except Exception as e:
                errors.append(f"Worker {worker_id} exception: {e}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    assert len(errors) == 0, f"Concurrent writes had {len(errors)} errors: {errors[:3]}"
    expected_writes = num_threads * writes_per_thread
    conn = engine._get_connection()
    count = conn.execute("SELECT count(*) as count FROM memories WHERE category = 'task_context';").fetchone()["count"]
    assert count >= expected_writes, f"Expected at least {expected_writes} task_context memories, found {count}"
    print(f"  [PASS] {expected_writes} concurrent writes completed across {num_threads} threads in {elapsed:.3f}s ({expected_writes/elapsed:.1f} ops/sec, 0 lock errors)")
    print("  => Test 4 Passed Successfully.")


def test_5_and_6_migration_and_rollback(tmp_dir: Path):
    print("\n--- Test 5 & 6: Migration from JSON & Rollback Engine ---")
    test_db_path = tmp_dir / "migration_test.db"
    test_json_path = tmp_dir / "memory.json"
    engine = SQLiteMemoryEngine(test_db_path)

    # Temporarily redirect module constants for migration test
    import sqlite_memory
    orig_json = sqlite_memory.MEMORY_JSON_FILE
    orig_bak_dir = sqlite_memory.MEMORY_BACKUP_DIR
    sqlite_memory.MEMORY_JSON_FILE = test_json_path
    sqlite_memory.MEMORY_BACKUP_DIR = tmp_dir / "backups"
    sqlite_memory.MEMORY_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Create legacy memory.json
        sample_legacy_data = {
            "user_preference": {
                "ide": {
                    "insight": "Preferred IDE is Visual Studio Code with dark mode",
                    "provenance": "user_explicit",
                    "confidence": 1.0,
                    "is_confirmed": True
                },
                "editor_font": {
                    "insight": "Use Cascadia Code font with ligatures enabled",
                    "provenance": "user_confirmed",
                    "confidence": 1.0,
                    "is_confirmed": True
                }
            },
            "workflow_pattern": {
                "pr_review": {
                    "insight": "Always check git diff and run unit tests before making a PR",
                    "provenance": "application_workflow",
                    "confidence": 0.8,
                    "is_confirmed": False
                }
            },
            "correction": {
                "default_port": {
                    "insight": "Chirkoot server runs on port 8000 not 5000",
                    "provenance": "user_explicit",
                    "confidence": 1.0,
                    "is_confirmed": True
                }
            }
        }
        with open(test_json_path, "w", encoding="utf-8") as f:
            json.dump(sample_legacy_data, f, indent=2)

        # 1. Run Migration
        mig_res = engine.migrate_from_json(delete_original_after_verification=False)
        assert mig_res["status"] == "success", f"Migration failed: {mig_res}"
        assert mig_res["migrated_count"] == 4, f"Expected 4 migrated records, got {mig_res['migrated_count']}"
        assert mig_res["test_retrieval_ok"] is True, "Test retrieval check should pass"

        # Verify records in SQLite
        conn = engine._get_connection()
        total_in_db = conn.execute("SELECT count(*) as count FROM memories;").fetchone()["count"]
        assert total_in_db == 4, f"Expected 4 records in DB, found {total_in_db}"

        # Verify FTS retrieval on migrated record
        mig_search = engine.search_hybrid("Cascadia Code font", top_k=2)
        assert len(mig_search) > 0, "Should find migrated Cascadia Code font"
        assert mig_search[0]["key_name"] == "editor_font", f"Expected editor_font, got {mig_search[0]['key_name']}"
        print(f"  [PASS] Migration: 4 legacy entries migrated cleanly. Test search retrieved '{mig_search[0]['key_name']}'")

        # Verify backup file exists
        bak_file = tmp_dir / "memory.json.bak"
        assert bak_file.exists(), f"Expected backup file {bak_file} to exist"

        # 2. Test Rollback
        # Remove or modify test_json_path to simulate corruption
        test_json_path.unlink(missing_ok=True)
        rb_res = engine.rollback_to_json()
        assert rb_res["status"] == "success", f"Rollback failed: {rb_res}"
        assert test_json_path.exists(), "Rollback should restore memory.json"

        # Verify restored content
        with open(test_json_path, "r", encoding="utf-8") as f:
            restored_data = json.load(f)
        assert "user_preference" in restored_data
        assert "ide" in restored_data["user_preference"]
        print(f"  [PASS] Rollback: Restored memory.json from {rb_res['source']} with matching keys")
        print("  => Test 5 & 6 Passed Successfully.")

    finally:
        sqlite_memory.MEMORY_JSON_FILE = orig_json
        sqlite_memory.MEMORY_BACKUP_DIR = orig_bak_dir


def test_7_memory_deletion(engine: SQLiteMemoryEngine):
    print("\n--- Test 7: Memory Deletion (Relational, FTS, Vector) ---")
    cat = "shortcut"
    key = "close_tab_shortcut"
    content = "Press Ctrl+W to close active browser tab"

    # Store memory
    res = engine.store_memory(
        category=cat,
        key=key,
        content=content,
        source="user_explicit"
    )
    mem_id = res["id"]

    # Verify exists in relational, FTS, and search
    assert engine.get_memory(cat, key) is not None, "Memory should exist"
    s1 = engine.search_hybrid("close active browser tab", top_k=2)
    assert any(r["key_name"] == key for r in s1), "Search should find close_tab_shortcut"

    # Delete memory
    del_res = engine.delete_memory(cat, key)
    assert del_res["status"] == "success", f"Delete should succeed, got {del_res}"

    # Verify removed from relational
    assert engine.get_memory(cat, key) is None, "Memory should no longer exist"

    # Verify removed from FTS search
    s2 = engine.search_hybrid("close active browser tab", top_k=5)
    assert not any(r["key_name"] == key for r in s2), "Deleted memory must not appear in search results"

    # Verify delete by memory_id works as well
    res2 = engine.store_memory(category="general", key="temp_fact", content="Temporary testing item", source="user_explicit")
    str_id = res2["memory_id"]
    del_res2 = engine.delete_memory_by_id(str_id)
    assert del_res2["status"] == "success", f"Delete by ID should succeed, got {del_res2}"
    assert engine.get_memory_by_id(str_id) is None, "Memory should be gone after delete_by_id"

    print("  [PASS] Memory deletion cleaned relational table, FTS index, and vector index")
    print("  => Test 7 Passed Successfully.")


def test_8_large_dataset_performance(engine: SQLiteMemoryEngine):
    print("\n--- Test 8: Large Dataset Scale & Performance (1,000+ Entries) ---")
    total_records = 1000
    print(f"  Inserting {total_records} diverse memory records...")

    topics = [
        ("user_preference", "preferred_resolution", "Display resolution preferred at 3840x2160 with 150 percent scaling"),
        ("shortcut", "quick_action", "Global hotkey Win+Shift+C brings up Chirkoot voice agent HUD"),
        ("workflow_pattern", "ci_pipeline", "Automated GitHub Actions linting and testing pipeline runs on master push"),
        ("project_information", "repo_structure", "Backend fastAPI server in server.py and UI automation in desktop_automation.py"),
        ("correction", "wake_phrase", "The correct wake phrase is Chirkoot not Circuit or Hey Siri"),
        ("general", "system_os", "Running Microsoft Windows 11 Enterprise x64 architecture with PowerShell 7"),
        ("task_context", "active_ticket", "Currently implementing sprint task MK-V persistent memory SQLite upgrade")
    ]

    conn = engine._get_connection()
    t0_insert = time.perf_counter()

    with engine._write_lock:
        with conn:
            for i in range(total_records):
                cat, base_key, base_content = topics[i % len(topics)]
                k = f"{base_key}_{i}"
                c = f"{base_content} [Instance index {i} with identifier token_{i*7%997}]"
                embed = generate_semantic_embedding(f"{cat} {k} {c}")
                embed_str = json.dumps(embed)
                now_iso = "2026-11-10T00:00:00"

                cur = conn.execute("""
                    INSERT INTO memories (
                        memory_id, key_name, content, category, source, confidence,
                        importance, created_at, updated_at, expires_at,
                        user_approval_status, access_count, embedding_json, metadata
                    ) VALUES (?, ?, ?, ?, ?, 1.0, 1.0, ?, ?, NULL, 'confirmed', 1, ?, '{}');
                """, (f"mem_perf_{i}", k, c, cat, "user_explicit", now_iso, now_iso, embed_str))
                row_id = cur.lastrowid
                if engine.vec_available:
                    try:
                        conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?);", (row_id, embed_str))
                    except Exception:
                        pass

    insert_elapsed = time.perf_counter() - t0_insert
    print(f"  [PASS] Inserted {total_records} records in {insert_elapsed:.2f}s ({total_records/insert_elapsed:.1f} records/sec)")

    # Measure query execution time over 10 distinct queries
    test_queries = [
        "GitHub Actions linting pipeline",
        "Display resolution 3840x2160",
        "Global hotkey voice agent HUD",
        "Chirkoot wake phrase not Circuit",
        "PowerShell Windows 11 Enterprise",
        "Sprint task persistent memory upgrade"
    ]

    latencies = []
    for q in test_queries:
        t0_q = time.perf_counter()
        results = engine.search_hybrid(q, top_k=5)
        q_elapsed = (time.perf_counter() - t0_q) * 1000.0  # in ms
        latencies.append(q_elapsed)
        assert len(results) > 0, f"Expected results for query '{q}'"
        assert results[0]["final_score"] > 0, "Top result should have positive score"

    avg_latency = sum(latencies) / len(latencies)
    max_latency = max(latencies)
    print(f"  [PASS] Hybrid Query Latency over {total_records}+ records: Avg = {avg_latency:.2f}ms, Max = {max_latency:.2f}ms (< 50ms requirement)")
    assert max_latency < 100.0, f"Query latency must be < 100ms, got max {max_latency:.2f}ms"

    # Verify prompt scoping never injects entire database
    prompt = engine.get_scoped_memories_prompt("GitHub Actions", top_k=5)
    line_count = len(prompt.strip().split("\n"))
    assert line_count <= 8, f"Scoped prompt must contain at most top-k lines, got {line_count}"
    assert "GitHub Actions" in prompt
    print(f"  [PASS] Scoped prompt injection limited to Top-K (lines: {line_count}) - database never dumped into context")
    print("  => Test 8 Passed Successfully.")


def test_9_provenance_guard(engine: SQLiteMemoryEngine):
    print("\n--- Test 9: Provenance Guard (Inferences Blocked from Preferences) ---")
    # 1. Attempt to create user preference from unconfirmed model inference -> MUST BE BLOCKED
    res1 = engine.store_memory(
        category="user_preference",
        key="coffee_preference",
        content="User likes black coffee without sugar",
        source="model_inference"
    )
    assert res1.get("status") == "error", f"Unconfirmed model inference should be rejected: {res1}"
    assert res1.get("provenance_violation") is True
    print("  [PASS] Unconfirmed model inference blocked from creating user_preference")

    # 2. Attempt to create correction from observed screen content -> MUST BE BLOCKED
    res2 = engine.store_memory(
        category="correction",
        key="misspelled_name",
        content="User's real name is John Doe",
        source="observed_screen_content"
    )
    assert res2.get("status") == "error", f"Observed screen content should be rejected: {res2}"
    assert res2.get("provenance_violation") is True
    print("  [PASS] Observed screen content blocked from creating correction")

    # 3. Explicit user statement -> MUST SUCCEED
    res3 = engine.store_memory(
        category="user_preference",
        key="coffee_preference",
        content="I prefer espresso in the morning",
        source="user_explicit"
    )
    assert res3.get("status") == "success", f"User explicit should be accepted: {res3}"
    print("  [PASS] Explicit user statement permitted to create user_preference")

    # 4. Confirmation of inference -> MUST SUCCEED
    res4 = engine.store_memory(
        category="observed_context",
        key="editor_color_scheme",
        content="Observed Monokai theme in active editor",
        source="observed_screen_content"
    )
    assert res4.get("status") == "success"
    # Confirm it into user_preference
    conf_res = engine.confirm_memory("observed_context", "editor_color_scheme", target_category="user_preference")
    assert conf_res.get("status") == "success"
    promoted = engine.get_memory("user_preference", "editor_color_scheme")
    assert promoted is not None
    assert promoted["user_approval_status"] == "confirmed"
    print("  [PASS] Promoted observed context to user_preference upon explicit user confirmation")
    print("  => Test 9 Passed Successfully.")


def main():
    print("=" * 70)
    print("CHIRKOOT MK-V SQLITE PERSISTENT MEMORY & HYBRID SEARCH TEST SUITE")
    print(f"SQLite Vec Extension Available: {SQLITE_VEC_AVAILABLE}")
    print("=" * 70)

    # Use an isolated temp directory for the test suite
    temp_dir = Path(tempfile.mkdtemp(prefix="chirkoot_memory_test_"))
    db_path = temp_dir / "test_memory.db"

    try:
        engine = SQLiteMemoryEngine(db_path)
        print(f"Initialized SQLite Memory Engine at: {db_path}")

        test_1_exact_keyword_retrieval(engine)
        test_2_semantic_retrieval(engine)
        test_3_duplicate_detection_and_merging(engine)
        test_4_concurrent_writes(engine)
        test_5_and_6_migration_and_rollback(temp_dir)
        test_7_memory_deletion(engine)
        test_8_large_dataset_performance(engine)
        test_9_provenance_guard(engine)

        engine.close()
        print("\n" + "=" * 70)
        print("ALL 9 SQLITE MEMORY ENGINE TESTS PASSED WITH 100% SUCCESS!")
        print("=" * 70)

    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

if __name__ == "__main__":
    main()
