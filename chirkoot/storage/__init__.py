"""Chirkoot Storage Package.

Provides SQLite FTS5 + sqlite-vec hybrid persistent memory engine,
semantic embedding projection, near-duplicate merging, migration, and rollback.
"""

from sqlite_memory import (
    SQLiteMemoryEngine,
    SQLITE_MEMORY,
    generate_semantic_embedding,
    cosine_similarity,
    canonicalize_category,
    STANDARD_CATEGORIES,
    SQLITE_VEC_AVAILABLE,
    MEMORY_DB_FILE,
    MEMORY_JSON_FILE,
    MEMORY_BACKUP_DIR
)

__all__ = [
    "SQLiteMemoryEngine",
    "SQLITE_MEMORY",
    "generate_semantic_embedding",
    "cosine_similarity",
    "canonicalize_category",
    "STANDARD_CATEGORIES",
    "SQLITE_VEC_AVAILABLE",
    "MEMORY_DB_FILE",
    "MEMORY_JSON_FILE",
    "MEMORY_BACKUP_DIR"
]
