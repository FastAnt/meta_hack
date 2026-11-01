"""Local vector store for content items and user reactions.

SQLite (stdlib) at `<data>/content_index.db`. Deliberately no
external vector DB: the catalogue is ~10^3 items, brute-force
cosine over same-model vectors is milliseconds.

Tables:
- items: one row per content pack (id, type, title, topic,
  source dir, text hash, embedding model, vector blob).
- reactions: like/dislike per item per project.
- serves: what was shown, when, and dwell seconds (a fact
  skipped in under ~3.5 s counts as a weak negative signal).
- gen_log: generation-request timestamps (cooldown).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .embeddings import pack_vector_bytes, unpack_vector

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY, type TEXT, title TEXT, topic TEXT,
  source_dir TEXT, text_hash TEXT, model TEXT, vector BLOB,
  updated_at REAL);
CREATE TABLE IF NOT EXISTS reactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT, project TEXT, kind TEXT, at REAL);
CREATE TABLE IF NOT EXISTS serves(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT, project TEXT, at REAL, dwell_s REAL);
CREATE TABLE IF NOT EXISTS gen_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT, at REAL, reason TEXT);
"""


class VectorStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path),
                                     check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- items ------------------------------------------------
    def upsert_item(self, pack, source_dir, model, vector, thash):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO items VALUES"
                " (?,?,?,?,?,?,?,?,?)",
                (pack["id"], pack.get("type", "game"),
                 pack.get("title", ""), pack.get("topic", ""),
                 str(source_dir), thash, model,
                 pack_vector_bytes(vector), time.time()))
            self._conn.commit()

    def item_meta(self, item_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT text_hash, model FROM items WHERE id=?",
                (item_id,)).fetchone()
        return row

    def all_vectors(self, model):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, vector FROM items WHERE model=?",
                (model,)).fetchall()
        return [(r[0], unpack_vector(r[1])) for r in rows]

    def purge_other_models(self, model):
        with self._lock:
            self._conn.execute(
                "DELETE FROM items WHERE model != ?", (model,))
            self._conn.commit()

    def get_vector(self, item_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT vector FROM items WHERE id=?",
                (item_id,)).fetchone()
        return unpack_vector(row[0]) if row else None

    # -- reactions ---------------------------------------------
    def add_reaction(self, item_id, project, kind):
        with self._lock:
            self._conn.execute(
                "INSERT INTO reactions(item_id, project, kind, at)"
                " VALUES (?,?,?,?)",
                (item_id, project, kind, time.time()))
            self._conn.commit()

    def reaction_ids(self, project, kind):
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT item_id FROM reactions"
                " WHERE project=? AND kind=?",
                (project, kind)).fetchall()
        return [r[0] for r in rows]

    def reaction_counts(self, project):
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) FROM reactions"
                " WHERE project=? GROUP BY kind",
                (project,)).fetchall()
        return {r[0]: r[1] for r in rows}

    # -- serves --------------------------------------------------
    def record_serve(self, item_id, project, dwell_s=None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO serves(item_id, project, at, dwell_s)"
                " VALUES (?,?,?,?)",
                (item_id, project, time.time(), dwell_s))
            self._conn.commit()

    def serve_count(self, project=None):
        with self._lock:
            if project:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM serves WHERE project=?",
                    (project,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM serves").fetchone()
        return row[0] if row else 0

    def reset_serves(self, project=None):
        """Forget what was watched (all projects by default).

        Reactions are preferences, not viewing history, so they
        stay. Returns the number of deleted serve rows."""
        with self._lock:
            if project:
                cur = self._conn.execute(
                    "DELETE FROM serves WHERE project=?", (project,))
            else:
                cur = self._conn.execute("DELETE FROM serves")
            self._conn.commit()
        return cur.rowcount

    def fast_skip_count(self, project, days=7):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM serves WHERE project=?"
                " AND dwell_s IS NOT NULL AND dwell_s < 3.5"
                " AND at > ?",
                (project, time.time() - days * 86400)).fetchone()
        return row[0] if row else 0

    # -- generation log -------------------------------------------
    def log_generation(self, project, reason):
        with self._lock:
            self._conn.execute(
                "INSERT INTO gen_log(project, at, reason)"
                " VALUES (?,?,?)", (project, time.time(), reason))
            self._conn.commit()

    def last_generation(self, project):
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(at) FROM gen_log WHERE project=?",
                (project,)).fetchone()
        return row[0] if row and row[0] else None
