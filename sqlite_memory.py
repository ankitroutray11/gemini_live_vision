"""Chirkoot MK-V SQLite Persistent Memory Engine with FTS5 and Vector Search (sqlite-vec).

Provides industrial-grade persistent memory with:
- Relational schema storing IDs, content, categories, provenance, confidence, importance,
  timestamps, expiry, and user approval status.
- SQLite FTS5 full-text keyword search with BM25 ranking.
- Vector search via sqlite-vec extension (vec0 virtual table) with fast fallback.
- Local deterministic 64-dimensional semantic embedding projection.
- Hybrid ranking: Combines FTS5 BM25 and Vector distance with recency, importance, and approval weighting.
- Automatic near-duplicate detection and merging.
- Safe WAL mode (Write-Ahead Logging) transactions supporting concurrent readers and writers.
- Migration engine: Automatic backup of memory.json, validation of counts/contents, test retrieval,
  and full rollback capability.
- Scoped top-k retrieval for prompt injection without dumping entire database into context window.
"""

import os
import sys
import re
import json
import time
import math
import shutil
import hashlib
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any, Union

logger = logging.getLogger("ChirkootSQLiteMemory")

# Try importing sqlite_vec for vector extension
SQLITE_VEC_AVAILABLE = False
try:
    import sqlite_vec
    SQLITE_VEC_AVAILABLE = True
except Exception as e:
    logger.warning(f"[Memory] sqlite-vec module not available: {e}. Falling back to in-memory cosine similarity.")

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except Exception:
    NUMPY_AVAILABLE = False

from security_policy import (
    CHIRKOOT_APPDATA_DIR,
    MEMORY_DIR,
    GLOBAL_TAINT_TRACKER,
    record_audit_event,
    redact_secrets
)

MEMORY_DB_FILE = MEMORY_DIR / "memory.db"
MEMORY_JSON_FILE = MEMORY_DIR / "memory.json"
MEMORY_BACKUP_DIR = MEMORY_DIR / "backups"
MEMORY_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

VECTOR_DIM = 64

# Category definitions
STANDARD_CATEGORIES = {
    "user_preference": "User-confirmed preferences",
    "user_confirmed_preferences": "User-confirmed preferences",
    "correction": "Corrections",
    "corrections": "Corrections",
    "personal_context": "Personal context",
    "workflow_pattern": "Workflow patterns",
    "workflow_patterns": "Workflow patterns",
    "shortcut": "Application shortcuts",
    "application_shortcuts": "Application shortcuts",
    "project_information": "Project information",
    "temporary_task_context": "Temporary task context",
    "task_context": "Temporary task context",
    "observed_information": "Observed information",
    "observed_context": "Observed information",
    "model_inference": "Model inferences",
    "model_inferences": "Model inferences",
    "general": "General facts and knowledge"
}

CATEGORY_CANONICAL_MAP = {
    "user_confirmed_preferences": "user_preference",
    "corrections": "correction",
    "workflow_patterns": "workflow_pattern",
    "application_shortcuts": "shortcut",
    "temporary_task_context": "task_context",
    "observed_information": "observed_context",
    "model_inferences": "model_inference"
}

def canonicalize_category(cat: str) -> str:
    c = cat.lower().strip()
    return CATEGORY_CANONICAL_MAP.get(c, c)


# ==============================================================================
# 1. LOCAL DETERMINISTIC SEMANTIC EMBEDDING ENGINE
# ==============================================================================

def generate_semantic_embedding(text: str, dim: int = VECTOR_DIM) -> List[float]:
    """Generates a dense normalized semantic feature vector for text.
    
    Uses subword n-grams and token hashing projected into a unit-normalized vector.
    Runs locally in < 0.1ms without external model weights or network requests.
    """
    clean = redact_secrets(text).lower()
    tokens = re.findall(r"\w+", clean)
    if not tokens:
        return [0.0] * dim

    vec = [0.0] * dim
    
    # 1. Word tokens
    for token in tokens:
        h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if (h >> 16) & 1 else -1.0
        vec[idx] += sign * 2.0

        # 2. Character 3-grams for subword morphology and typo tolerance
        if len(token) >= 3:
            for i in range(len(token) - 2):
                ng = token[i:i+3]
                h_ng = int(hashlib.md5(ng.encode("utf-8")).hexdigest(), 16)
                idx_ng = h_ng % dim
                sign_ng = 1.0 if (h_ng >> 16) & 1 else -1.0
                vec[idx_ng] += sign_ng * 0.6

    # L2 normalize
    norm_sq = sum(x * x for x in vec)
    if norm_sq > 0:
        norm = math.sqrt(norm_sq)
        vec = [round(x / norm, 6) for x in vec]
    return vec

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Computes cosine similarity between two float vectors."""
    if len(v1) != len(v2) or not v1 or not v2:
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 > 0 and norm2 > 0:
        return max(0.0, min(1.0, dot / (norm1 * norm2)))
    return 0.0


# ==============================================================================
# 2. SQLITE MEMORY ENGINE (FTS5 + SQLITE-VEC)
# ==============================================================================

class SQLiteMemoryEngine:
    """Thread-safe SQLite persistent memory system with FTS5 and sqlite-vec support."""
    
    _instance: Optional['SQLiteMemoryEngine'] = None
    _lock = threading.RLock()

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or MEMORY_DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self.vec_available = False
        self._init_database()

    @classmethod
    def get_instance(cls, db_path: Optional[Path] = None) -> 'SQLiteMemoryEngine':
        with cls._lock:
            if cls._instance is None or (db_path and cls._instance.db_path != db_path):
                cls._instance = SQLiteMemoryEngine(db_path)
            return cls._instance

    def _get_connection(self) -> sqlite3.Connection:
        """Returns a thread-local SQLite connection configured with WAL and extensions."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=10.0,
                check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            # WAL mode for high concurrency
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("PRAGMA foreign_keys = ON;")

            # Load sqlite-vec extension if available
            if SQLITE_VEC_AVAILABLE:
                try:
                    conn.enable_load_extension(True)
                    sqlite_vec.load(conn)
                    conn.enable_load_extension(False)
                    self.vec_available = True
                except Exception as e:
                    logger.warning(f"[Memory] Failed to load sqlite-vec extension in connection: {e}")
                    self.vec_available = False
            else:
                self.vec_available = False

            self._local.conn = conn
        return conn

    def close(self):
        """Closes the thread-local connection."""
        conn = getattr(self._local, "conn", None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def _init_database(self):
        """Initializes tables, FTS5 index, triggers, and vector virtual table."""
        with self._write_lock:
            conn = self._get_connection()
            with conn:
                # 1. Main memories relational table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_id TEXT UNIQUE NOT NULL,
                        key_name TEXT NOT NULL,
                        content TEXT NOT NULL,
                        category TEXT NOT NULL,
                        source TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 1.0,
                        importance REAL NOT NULL DEFAULT 1.0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        expires_at TEXT DEFAULT NULL,
                        user_approval_status TEXT NOT NULL DEFAULT 'unconfirmed',
                        access_count INTEGER NOT NULL DEFAULT 0,
                        embedding_json TEXT NOT NULL,
                        metadata TEXT DEFAULT '{}'
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_cat_key ON memories(category, key_name);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(user_approval_status);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at);")

                # 2. SQLite FTS5 Full-Text Search Virtual Table
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        content,
                        category,
                        key_name,
                        content='memories',
                        content_rowid='id'
                    );
                """)

                # Triggers to keep FTS5 synchronized automatically with memories table
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_fts(rowid, content, category, key_name)
                        VALUES (new.id, new.content, new.category, new.key_name);
                    END;
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content, category, key_name)
                        VALUES('delete', old.id, old.content, old.category, old.key_name);
                    END;
                """)
                conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content, category, key_name)
                        VALUES('delete', old.id, old.content, old.category, old.key_name);
                        INSERT INTO memories_fts(rowid, content, category, key_name)
                        VALUES (new.id, new.content, new.category, new.key_name);
                    END;
                """)

                # 3. sqlite-vec Vector Table
                if self.vec_available:
                    try:
                        conn.execute(f"""
                            CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
                                rowid INTEGER PRIMARY KEY,
                                embedding float[{VECTOR_DIM}]
                            );
                        """)
                    except Exception as ve:
                        logger.warning(f"[Memory] Could not create vec0 virtual table: {ve}. Using in-memory vector fallback.")
                        self.vec_available = False

    # --------------------------------------------------------------------------
    # CRUD Operations
    # --------------------------------------------------------------------------

    def store_memory(
        self,
        category: str,
        key: str,
        content: str,
        source: str = "user_explicit",
        confidence: Optional[float] = None,
        importance: float = 1.0,
        user_approval_status: Optional[str] = None,
        ttl_seconds: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Stores or updates a memory with provenance checks, duplicate detection, and embedding indexing."""
        cat = canonicalize_category(category)
        clean_key = key.strip()
        clean_content = redact_secrets(content.strip())
        now = datetime.now()
        now_iso = now.isoformat()

        # Provenance enforcement: only direct user input or explicit confirmation can write to preferences/corrections
        is_tainted, taint_source, _ = GLOBAL_TAINT_TRACKER.is_tainted()
        eff_source = source.lower().strip()
        if is_tainted and eff_source == "user_explicit" and (not user_approval_status or user_approval_status != "confirmed"):
            eff_source = "observed_screen_content"

        is_confirmed = (user_approval_status == "confirmed") or (eff_source in ("user_explicit", "user_confirmed"))
        status = "confirmed" if is_confirmed else "unconfirmed"

        if cat in ("user_preference", "correction") and not is_confirmed:
            msg = (
                f"[PROVENANCE ERROR] Inferred or observed information ({eff_source}) cannot "
                f"create persistent {cat} '{clean_key}' without explicit user confirmation."
            )
            logger.warning(f"[Memory] Blocked unconfirmed preference: {msg}")
            record_audit_event("MEMORY_PROVENANCE_BLOCKED", {
                "category": cat,
                "key": clean_key,
                "source": eff_source
            })
            return {"status": "error", "error": msg, "provenance_violation": True}

        # Calculate confidence & importance
        if is_confirmed:
            eff_confidence = 1.0 if confidence is None else min(1.0, max(0.0, float(confidence)))
            eff_importance = max(1.5, float(importance))
        else:
            eff_confidence = min(1.0, max(0.0, float(confidence if confidence is not None else 0.6)))
            eff_importance = float(importance)

        # Expiry
        expires_at = None
        if ttl_seconds is not None and ttl_seconds > 0:
            expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        elif eff_source == "temporary_task_context" or cat == "task_context":
            expires_at = (now + timedelta(hours=2)).isoformat()

        # Generate embedding
        embedding = generate_semantic_embedding(f"{cat} {clean_key} {clean_content}")
        embedding_str = json.dumps(embedding)

        # Duplicate check: within same category, detect near-duplicate via vector cosine similarity
        existing_dup = self.find_duplicate(cat, clean_key, embedding, threshold=0.98)

        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._write_lock:
            conn = self._get_connection()
            with conn:
                if existing_dup:
                    # Update existing record
                    target_id = existing_dup["id"]
                    conn.execute("""
                        UPDATE memories SET
                            content = ?,
                            confidence = max(confidence, ?),
                            importance = max(importance, ?),
                            updated_at = ?,
                            expires_at = ?,
                            user_approval_status = ?,
                            access_count = access_count + 1,
                            embedding_json = ?,
                            metadata = ?
                        WHERE id = ?;
                    """, (
                        clean_content, eff_confidence, eff_importance, now_iso,
                        expires_at, status, embedding_str, meta_json, target_id
                    ))
                    # Update vector table
                    if self.vec_available:
                        try:
                            conn.execute("DELETE FROM memories_vec WHERE rowid = ?;", (target_id,))
                            conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?);", (target_id, embedding_str))
                        except Exception as ve:
                            logger.warning(f"Error updating vector record: {ve}")

                    record_audit_event("MEMORY_UPDATE_MERGE", {"id": target_id, "key": clean_key, "category": cat})
                    return {
                        "status": "success",
                        "action": "updated",
                        "memory_id": existing_dup["memory_id"],
                        "id": target_id,
                        "category": cat,
                        "key": clean_key,
                        "confirmed": is_confirmed
                    }
                else:
                    # Insert new record
                    mem_id = f"mem_{secrets_token(cat, clean_key)}"
                    cur = conn.execute("""
                        INSERT INTO memories (
                            memory_id, key_name, content, category, source, confidence,
                            importance, created_at, updated_at, expires_at,
                            user_approval_status, access_count, embedding_json, metadata
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?);
                    """, (
                        mem_id, clean_key, clean_content, cat, eff_source, eff_confidence,
                        eff_importance, now_iso, now_iso, expires_at,
                        status, embedding_str, meta_json
                    ))
                    new_id = cur.lastrowid
                    if self.vec_available:
                        try:
                            conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?);", (new_id, embedding_str))
                        except Exception as ve:
                            logger.warning(f"Error inserting vector record: {ve}")

                    record_audit_event("MEMORY_INSERT", {"id": new_id, "key": clean_key, "category": cat})
                    return {
                        "status": "success",
                        "action": "created",
                        "memory_id": mem_id,
                        "id": new_id,
                        "category": cat,
                        "key": clean_key,
                        "confirmed": is_confirmed
                    }

    def find_duplicate(
        self,
        category: str,
        key: str,
        embedding: List[float],
        threshold: float = 0.98
    ) -> Optional[Dict[str, Any]]:
        """Finds an existing near-duplicate memory using exact key match or high vector similarity."""
        conn = self._get_connection()
        # 1. Exact key match in category
        row = conn.execute(
            "SELECT id, memory_id, content, embedding_json FROM memories WHERE category = ? AND key_name = ? LIMIT 1;",
            (category, key)
        ).fetchone()
        if row:
            return dict(row)

        # 2. Vector distance match in category
        candidates = conn.execute(
            "SELECT id, memory_id, content, embedding_json FROM memories WHERE category = ? LIMIT 200;",
            (category,)
        ).fetchall()
        for cand in candidates:
            try:
                cand_vec = json.loads(cand["embedding_json"])
                sim = cosine_similarity(embedding, cand_vec)
                if sim >= threshold:
                    return dict(cand)
            except Exception:
                continue
        return None

    def get_memory(self, category: str, key: str) -> Optional[Dict[str, Any]]:
        """Retrieves a memory by category and key."""
        cat = canonicalize_category(category)
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM memories WHERE category = ? AND key_name = ? LIMIT 1;",
            (cat, key.strip())
        ).fetchone()
        return dict(row) if row else None

    def get_memory_by_id(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a memory by its unique string memory_id or numeric ID."""
        conn = self._get_connection()
        if str(memory_id).isdigit():
            row = conn.execute("SELECT * FROM memories WHERE id = ? LIMIT 1;", (int(memory_id),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM memories WHERE memory_id = ? LIMIT 1;", (memory_id,)).fetchone()
        return dict(row) if row else None

    def update_memory(
        self,
        category: str,
        key: str,
        new_content: str,
        importance: Optional[float] = None,
        user_confirmed: bool = True
    ) -> Dict[str, Any]:
        """Edits an existing memory insight and marks it confirmed."""
        cat = canonicalize_category(category)
        clean_key = key.strip()
        clean_content = redact_secrets(new_content.strip())
        now_iso = datetime.now().isoformat()
        new_embed = generate_semantic_embedding(f"{cat} {clean_key} {clean_content}")
        embed_str = json.dumps(new_embed)

        with self._write_lock:
            conn = self._get_connection()
            with conn:
                row = conn.execute(
                    "SELECT id FROM memories WHERE category = ? AND key_name = ? LIMIT 1;",
                    (cat, clean_key)
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT id FROM memories WHERE memory_id = ? OR id = ? LIMIT 1;",
                        (clean_key, int(clean_key) if clean_key.isdigit() else -1)
                    ).fetchone()
                if not row:
                    return {"status": "error", "error": f"Memory [{cat}] '{clean_key}' not found."}

                mem_row_id = row["id"]
                imp_clause = "importance = ?" if importance is not None else "importance = importance"
                params = [clean_content, now_iso, embed_str]
                if importance is not None:
                    params.append(float(importance))
                if user_confirmed:
                    params.append("confirmed")
                    params.append("user_confirmed")
                    approval_sql = ", user_approval_status = ?, source = ?"
                else:
                    approval_sql = ""

                params.extend([mem_row_id])

                sql = f"""
                    UPDATE memories SET
                        content = ?,
                        updated_at = ?,
                        embedding_json = ?
                        {', importance = ?' if importance is not None else ''}
                        {approval_sql}
                    WHERE id = ?;
                """
                conn.execute(sql, params)

                if self.vec_available:
                    try:
                        conn.execute("DELETE FROM memories_vec WHERE rowid = ?;", (mem_row_id,))
                        conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?);", (mem_row_id, embed_str))
                    except Exception as ve:
                        logger.warning(f"Error updating vector record: {ve}")

                record_audit_event("MEMORY_EDIT", {"id": mem_row_id, "key": clean_key, "category": cat})
                return {"status": "success", "id": mem_row_id, "category": cat, "key": clean_key}

    def confirm_memory(self, category: str, key: str, target_category: Optional[str] = None) -> Dict[str, Any]:
        """Promotes an unconfirmed memory to user_approval_status='confirmed'."""
        cat = canonicalize_category(category)
        clean_key = key.strip()
        dest_cat = canonicalize_category(target_category) if target_category else cat

        with self._write_lock:
            conn = self._get_connection()
            with conn:
                row = conn.execute(
                    "SELECT id, content FROM memories WHERE category = ? AND key_name = ? LIMIT 1;",
                    (cat, clean_key)
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT id, content FROM memories WHERE memory_id = ? OR id = ? LIMIT 1;",
                        (clean_key, int(clean_key) if clean_key.isdigit() else -1)
                    ).fetchone()
                if not row:
                    return {"status": "error", "error": f"Memory [{cat}] '{clean_key}' not found."}

                mem_row_id = row["id"]
                now_iso = datetime.now().isoformat()
                new_embed = generate_semantic_embedding(f"{dest_cat} {clean_key} {row['content']}")
                embed_str = json.dumps(new_embed)

                conn.execute("""
                    UPDATE memories SET
                        category = ?,
                        source = 'user_confirmed',
                        confidence = 1.0,
                        importance = max(importance, 1.5),
                        user_approval_status = 'confirmed',
                        expires_at = NULL,
                        updated_at = ?,
                        embedding_json = ?
                    WHERE id = ?;
                """, (dest_cat, now_iso, embed_str, mem_row_id))

                if self.vec_available:
                    try:
                        conn.execute("DELETE FROM memories_vec WHERE rowid = ?;", (mem_row_id,))
                        conn.execute("INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?);", (mem_row_id, embed_str))
                    except Exception as ve:
                        logger.warning(f"Error updating vector record: {ve}")

                record_audit_event("MEMORY_CONFIRM", {"id": mem_row_id, "key": clean_key, "target_category": dest_cat})
                return {"status": "success", "id": mem_row_id, "category": dest_cat, "key": clean_key}

    def delete_memory(self, category: str, key: str) -> Dict[str, Any]:
        """Deletes a memory by category and key."""
        cat = canonicalize_category(category)
        clean_key = key.strip()
        with self._write_lock:
            conn = self._get_connection()
            with conn:
                row = conn.execute(
                    "SELECT id, content FROM memories WHERE category = ? AND key_name = ? LIMIT 1;",
                    (cat, clean_key)
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT id, content FROM memories WHERE memory_id = ? OR id = ? LIMIT 1;",
                        (clean_key, int(clean_key) if clean_key.isdigit() else -1)
                    ).fetchone()
                if not row:
                    return {"status": "error", "error": f"Memory [{cat}] '{clean_key}' not found."}

                mem_id = row["id"]
                conn.execute("DELETE FROM memories WHERE id = ?;", (mem_id,))
                if self.vec_available:
                    try:
                        conn.execute("DELETE FROM memories_vec WHERE rowid = ?;", (mem_id,))
                    except Exception:
                        pass

                record_audit_event("MEMORY_DELETE", {"id": mem_id, "category": cat, "key": clean_key})
                return {"status": "success", "id": mem_id, "category": cat, "key": clean_key, "content": row["content"]}

    def delete_memory_by_id(self, memory_id: str) -> Dict[str, Any]:
        """Deletes a memory by string memory_id or numeric ID."""
        with self._write_lock:
            conn = self._get_connection()
            with conn:
                if str(memory_id).isdigit():
                    row = conn.execute("SELECT id, category, key_name FROM memories WHERE id = ? LIMIT 1;", (int(memory_id),)).fetchone()
                else:
                    row = conn.execute("SELECT id, category, key_name FROM memories WHERE memory_id = ? LIMIT 1;", (memory_id,)).fetchone()
                if not row:
                    return {"status": "error", "error": f"Memory ID '{memory_id}' not found."}

                num_id = row["id"]
                conn.execute("DELETE FROM memories WHERE id = ?;", (num_id,))
                if self.vec_available:
                    try:
                        conn.execute("DELETE FROM memories_vec WHERE rowid = ?;", (num_id,))
                    except Exception:
                        pass

                record_audit_event("MEMORY_DELETE", {"id": num_id, "memory_id": memory_id})
                return {"status": "success", "id": num_id, "category": row["category"], "key": row["key_name"]}

    def prune_expired(self) -> int:
        """Purges memories whose expires_at timestamp has passed."""
        now_iso = datetime.now().isoformat()
        with self._write_lock:
            conn = self._get_connection()
            with conn:
                rows = conn.execute(
                    "SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?;",
                    (now_iso,)
                ).fetchall()
                if not rows:
                    return 0

                ids = [r["id"] for r in rows]
                for mem_id in ids:
                    conn.execute("DELETE FROM memories WHERE id = ?;", (mem_id,))
                    if self.vec_available:
                        try:
                            conn.execute("DELETE FROM memories_vec WHERE rowid = ?;", (mem_id,))
                        except Exception:
                            pass
                return len(ids)

    def list_memories(
        self,
        category: Optional[str] = None,
        query: Optional[str] = None,
        include_expired: bool = False,
        limit: int = 200,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Lists memories matching category and optional keyword query with pagination."""
        if not include_expired:
            self.prune_expired()

        conn = self._get_connection()
        conditions = []
        params = []

        if not include_expired:
            conditions.append("(expires_at IS NULL OR expires_at >= ?)")
            params.append(datetime.now().isoformat())

        if category:
            cat = canonicalize_category(category)
            conditions.append("category = ?")
            params.append(cat)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT * FROM memories{where_clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?;"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d.pop("embedding_json", None)
            d["insight"] = d["content"]
            d["key"] = d["key_name"]
            d["is_confirmed"] = (d["user_approval_status"] == "confirmed")
            d["provenance"] = d["source"]
            results.append(d)
        return results

    # --------------------------------------------------------------------------
    # Hybrid Search: FTS5 + Vector Similarity + Ranking
    # --------------------------------------------------------------------------

    def search_hybrid(
        self,
        query: str,
        category: Optional[str] = None,
        top_k: int = 5,
        fts_weight: float = 0.5,
        vec_weight: float = 0.5
    ) -> List[Dict[str, Any]]:
        """Performs hybrid retrieval combining FTS5 keyword matching and vector similarity.
        
        Applies:
        - BM25 score normalization.
        - Vector cosine distance normalization.
        - Recency decay half-life weighting.
        - Importance and user approval multipliers.
        """
        self.prune_expired()
        clean_query = query.strip()
        if not clean_query:
            return []

        conn = self._get_connection()
        cat_filter = canonicalize_category(category) if category else None
        now_dt = datetime.now()
        now_iso = now_dt.isoformat()

        # 1. FTS5 Keyword Search
        fts_scores: Dict[int, float] = {}
        tokens = re.findall(r"\w+", clean_query.lower())
        if tokens:
            # Build FTS5 prefix OR query: token1* OR token2*
            fts_match_expr = " OR ".join(f'"{t}"*' for t in tokens)
            try:
                sql_fts = """
                    SELECT rowid, bm25(memories_fts, 5.0, 1.0, 5.0) AS bm25_rank
                    FROM memories_fts
                    WHERE memories_fts MATCH ?
                    LIMIT 100;
                """
                fts_rows = conn.execute(sql_fts, (fts_match_expr,)).fetchall()
                for r in fts_rows:
                    row_id = r["rowid"]
                    # In FTS5, bm25 returns negative or zero. Lower (more negative) is better match.
                    raw_rank = r["bm25_rank"]
                    normalized_score = max(0.0, -raw_rank) / (1.0 + max(0.0, -raw_rank))
                    fts_scores[row_id] = normalized_score
            except Exception as e:
                logger.debug(f"[Memory] FTS5 query failed: {e}")

        # 2. Vector Similarity Search
        vec_scores: Dict[int, float] = {}
        q_vec = generate_semantic_embedding(clean_query)
        q_vec_str = json.dumps(q_vec)

        if self.vec_available:
            try:
                sql_vec = """
                    SELECT rowid, distance
                    FROM memories_vec
                    WHERE embedding MATCH ?
                    ORDER BY distance
                    LIMIT 100;
                """
                vec_rows = conn.execute(sql_vec, (q_vec_str,)).fetchall()
                for r in vec_rows:
                    row_id = r["rowid"]
                    dist = float(r["distance"])
                    # L2 distance on unit vectors in [0, 2]. Similarity in [0, 1]
                    sim = max(0.0, 1.0 - (dist / 2.0))
                    vec_scores[row_id] = sim
            except Exception as ve:
                logger.warning(f"[Memory] sqlite-vec search error: {ve}")
        
        # Fallback vector search if sqlite-vec unavailable or table empty
        if not vec_scores:
            rows_all = conn.execute(
                "SELECT id, embedding_json FROM memories WHERE (expires_at IS NULL OR expires_at >= ?) LIMIT 500;",
                (now_iso,)
            ).fetchall()
            for r in rows_all:
                row_id = r["id"]
                try:
                    cand_vec = json.loads(r["embedding_json"])
                    sim = cosine_similarity(q_vec, cand_vec)
                    if sim > 0.1:
                        vec_scores[row_id] = sim
                except Exception:
                    continue

        # 3. Combine Candidates
        all_candidate_ids = set(fts_scores.keys()) | set(vec_scores.keys())
        if not all_candidate_ids:
            return []

        # Retrieve full candidate records
        placeholders = ",".join("?" for _ in all_candidate_ids)
        sql_fetch = f"""
            SELECT * FROM memories
            WHERE id IN ({placeholders})
            AND (expires_at IS NULL OR expires_at >= ?);
        """
        fetch_params = list(all_candidate_ids) + [now_iso]
        cand_rows = conn.execute(sql_fetch, fetch_params).fetchall()

        ranked_results = []
        for row in cand_rows:
            row_id = row["id"]
            if cat_filter and row["category"] != cat_filter:
                continue

            score_fts = fts_scores.get(row_id, 0.0)
            score_vec = vec_scores.get(row_id, 0.0)

            # Combined base score
            if score_fts > 0 and score_vec > 0:
                base_score = (fts_weight * score_fts) + (vec_weight * score_vec)
            elif score_fts > 0:
                base_score = score_fts * 0.8
            else:
                base_score = score_vec * 0.8

            # Importance weighting
            importance = float(row["importance"])
            w_imp = 1.0 + 0.25 * max(0.0, importance - 1.0)

            # Approval status boost
            is_confirmed = (row["user_approval_status"] == "confirmed")
            w_approval = 1.4 if is_confirmed else 0.8

            # Recency weighting (30-day half-life; muted for confirmed user preferences)
            if is_confirmed and row["category"] in ("user_preference", "correction"):
                w_recency = 1.0
            else:
                try:
                    upd_dt = datetime.fromisoformat(row["updated_at"])
                    age_days = (now_dt - upd_dt).total_seconds() / 86400.0
                    w_recency = math.exp(-0.023 * max(0.0, age_days))
                except Exception:
                    w_recency = 1.0

            final_score = base_score * w_imp * w_approval * w_recency

            res_dict = dict(row)
            res_dict.pop("embedding_json", None)
            res_dict["insight"] = res_dict["content"]
            res_dict["key"] = res_dict["key_name"]
            res_dict["fts_score"] = round(score_fts, 4)
            res_dict["vec_score"] = round(score_vec, 4)
            res_dict["final_score"] = round(final_score, 4)
            res_dict["is_confirmed"] = is_confirmed
            res_dict["provenance"] = row["source"]
            ranked_results.append(res_dict)

        ranked_results.sort(key=lambda x: x["final_score"], reverse=True)
        return ranked_results[:top_k]

    def get_scoped_memories_prompt(self, query: str = "", top_k: int = 5) -> str:
        """Retrieves top-k relevant memories on-demand formatted for system instruction injection.
        
        Guarantees that the entire memory database is never dumped into the prompt.
        """
        if not query or not query.strip():
            # If no query, return top confirmed user preferences
            conn = self._get_connection()
            rows = conn.execute("""
                SELECT category, key_name, content, user_approval_status
                FROM memories
                WHERE user_approval_status = 'confirmed'
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?;
            """, (top_k,)).fetchall()
            if not rows:
                return ""
            items = [f"- [{r['category'].upper()}] {r['key_name']}: {r['content']} (verified)" for r in rows]
            return (
                "\n\nRELEVANT USER PREFERENCES & MEMORY (Scoped Top-K):\n"
                + "\n".join(items)
                + "\nAlways apply confirmed preferences in your operations."
            )

        results = self.search_hybrid(query, top_k=top_k)
        if not results:
            return ""

        items = []
        for r in results:
            status_tag = "verified" if r["is_confirmed"] else f"unconfirmed_{r['source']}"
            items.append(f"- [{r['category'].upper()}] {r['key_name']}: {r['content']} (status: {status_tag})")

        return (
            "\n\nRELEVANT USER PREFERENCES & MEMORY (Scoped Top-K):\n"
            + "\n".join(items)
            + "\nAlways apply confirmed preferences in your operations."
        )

    # --------------------------------------------------------------------------
    # Migration & Rollback Engine
    # --------------------------------------------------------------------------

    def backup_json_memory(self) -> Optional[Path]:
        """Creates a timestamped and standard .bak backup of memory.json before migration."""
        if not MEMORY_JSON_FILE.exists():
            return None
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        ts_backup = MEMORY_BACKUP_DIR / f"memory_backup_{timestamp}.json"
        std_backup = MEMORY_JSON_FILE.parent / f"{MEMORY_JSON_FILE.name}.bak"
        try:
            shutil.copy2(MEMORY_JSON_FILE, ts_backup)
            shutil.copy2(MEMORY_JSON_FILE, std_backup)
            logger.info(f"[Migration] Backed up memory.json to {ts_backup} and {std_backup}")
            return std_backup
        except Exception as e:
            logger.error(f"[Migration] Failed to back up memory.json: {e}")
            return None

    def migrate_from_json(self, delete_original_after_verification: bool = False) -> Dict[str, Any]:
        """Migrates all valid records from memory.json into SQLite with validation and rollback."""
        if not MEMORY_JSON_FILE.exists():
            return {"status": "skipped", "message": "memory.json does not exist. Nothing to migrate."}

        backup_path = self.backup_json_memory()
        if not backup_path:
            return {"status": "error", "error": "Could not create backup of memory.json prior to migration."}

        try:
            with open(MEMORY_JSON_FILE, "r", encoding="utf-8") as f:
                raw_json = json.load(f)
        except Exception as e:
            return {"status": "error", "error": f"Failed to parse memory.json: {e}"}

        migrated_count = 0
        skipped_count = 0
        sample_keys = []

        # Iterate over all categories and entries
        for cat_name, entries in raw_json.items():
            if not isinstance(entries, dict):
                continue
            for k, val in entries.items():
                if not isinstance(val, dict):
                    continue
                content = val.get("insight") or val.get("content") or ""
                if not content or not str(content).strip():
                    skipped_count += 1
                    continue

                source = val.get("source") or val.get("provenance") or "user_explicit"
                conf = val.get("confidence", 1.0 if val.get("is_confirmed") else 0.8)
                is_conf = val.get("is_confirmed", source in ("user_explicit", "user_confirmed"))
                status = "confirmed" if is_conf else "unconfirmed"
                imp = 2.0 if cat_name in ("user_preference", "correction") else 1.0

                meta = {
                    "source_context": val.get("source_context", ""),
                    "original_access_count": val.get("access_count", 1)
                }

                res = self.store_memory(
                    category=cat_name,
                    key=k,
                    content=str(content).strip(),
                    source=source,
                    confidence=conf,
                    importance=imp,
                    user_approval_status=status,
                    metadata=meta
                )
                if res.get("status") == "success":
                    migrated_count += 1
                    if len(sample_keys) < 5:
                        sample_keys.append((cat_name, k))
                else:
                    skipped_count += 1

        # Validation: Verify counts and verify retrieval
        conn = self._get_connection()
        total_in_db = conn.execute("SELECT count(*) as count FROM memories;").fetchone()["count"]
        
        # Test retrieval on one migrated key
        test_retrieval_ok = True
        if sample_keys:
            test_cat, test_key = sample_keys[0]
            fetched = self.get_memory(test_cat, test_key)
            if not fetched:
                test_retrieval_ok = False

        if total_in_db < migrated_count or not test_retrieval_ok:
            return {
                "status": "error",
                "error": "Migration verification failed: Record counts or retrieval check mismatch.",
                "total_in_db": total_in_db,
                "expected": migrated_count
            }

        # Safe archiving of original
        if delete_original_after_verification:
            try:
                MEMORY_JSON_FILE.unlink()
                logger.info("[Migration] Verified migration complete. Original memory.json removed.")
            except Exception as e:
                logger.warning(f"[Migration] Could not remove original memory.json: {e}")
        else:
            archived = MEMORY_JSON_FILE.parent / f"{MEMORY_JSON_FILE.name}.migrated"
            try:
                shutil.move(str(MEMORY_JSON_FILE), str(archived))
                logger.info(f"[Migration] Verified migration complete. Original moved to {archived}")
            except Exception as e:
                logger.warning(f"[Migration] Could not archive original memory.json: {e}")

        record_audit_event("MEMORY_MIGRATION_SUCCESS", {
            "migrated_count": migrated_count,
            "total_in_db": total_in_db,
            "backup_path": str(backup_path)
        })

        return {
            "status": "success",
            "migrated_count": migrated_count,
            "skipped_count": skipped_count,
            "total_in_db": total_in_db,
            "test_retrieval_ok": test_retrieval_ok,
            "backup_file": str(backup_path)
        }

    def rollback_to_json(self) -> Dict[str, Any]:
        """Rolls back the memory system from the .bak backup to restore memory.json."""
        std_backup = MEMORY_JSON_FILE.parent / f"{MEMORY_JSON_FILE.name}.bak"
        if not std_backup.exists():
            # Check latest timestamped backup
            backups = sorted(MEMORY_BACKUP_DIR.glob("memory_backup_*.json"))
            if backups:
                std_backup = backups[-1]
            else:
                return {"status": "error", "error": "No backup file found to rollback from."}

        try:
            shutil.copy2(std_backup, MEMORY_JSON_FILE)
            logger.info(f"[Migration] Rolled back memory.json from {std_backup}")
            record_audit_event("MEMORY_MIGRATION_ROLLBACK", {"restored_from": str(std_backup)})
            return {"status": "success", "restored_file": str(MEMORY_JSON_FILE), "source": str(std_backup)}
        except Exception as e:
            return {"status": "error", "error": f"Failed to restore backup: {e}"}


def secrets_token(cat: str, key: str) -> str:
    """Generates a stable unique hex slug for category and key."""
    h = hashlib.sha256(f"{cat}:{key}:{time.time()}".encode("utf-8")).hexdigest()
    return h[:12]


# Initialize global engine instance
SQLITE_MEMORY = SQLiteMemoryEngine.get_instance()
