"""중앙 SQLite 저장소: 파일, 태그, 본문 전문검색(FTS5), 임베딩, 이동 기록."""
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    fingerprint TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_fp ON files(fingerprint);
CREATE TABLE IF NOT EXISTS tags(
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS file_tags(
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'user',  -- user | ai | rule
    PRIMARY KEY(file_id, tag_id)
);
CREATE TABLE IF NOT EXISTS embeddings(
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS moves(
    id INTEGER PRIMARY KEY,
    batch TEXT NOT NULL,
    file_id INTEGER NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    at REAL NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(name, path, note, tags, content, tokenize='trigram');
"""


@dataclass
class FileRow:
    id: int
    path: str
    name: str
    ext: str
    kind: str
    size: int
    mtime: float
    note: str
    tags: list[str]


def clean_tag(tag: str) -> str:
    return tag.strip().lstrip("#").strip()


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    # ---------- 파일 ----------
    def get_by_path(self, path: str):
        return self.conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()

    def find_missing_by_fingerprint(self, fingerprint: str):
        """같은 지문을 가졌지만 원래 위치에 더 이상 없는 파일 (다른 프로그램으로 옮겨진 파일 재연결용)."""
        for row in self.conn.execute("SELECT * FROM files WHERE fingerprint=?", (fingerprint,)):
            if not Path(row["path"]).exists():
                return row
        return None

    def upsert_file(self, path: str, name: str, ext: str, kind: str, size: int, mtime: float, fingerprint: str) -> int:
        row = self.get_by_path(path)
        with self.conn:
            if row:
                self.conn.execute(
                    "UPDATE files SET name=?, ext=?, kind=?, size=?, mtime=?, fingerprint=? WHERE id=?",
                    (name, ext, kind, size, mtime, fingerprint, row["id"]),
                )
                return row["id"]
            cur = self.conn.execute(
                "INSERT INTO files(path,name,ext,kind,size,mtime,fingerprint,added_at) VALUES(?,?,?,?,?,?,?,?)",
                (path, name, ext, kind, size, mtime, fingerprint, time.time()),
            )
            return cur.lastrowid

    def update_path(self, file_id: int, new_path: str):
        p = Path(new_path)
        with self.conn:
            self.conn.execute("UPDATE files SET path=?, name=? WHERE id=?", (str(p), p.name, file_id))
        self._refresh_fts(file_id)

    def set_note(self, file_id: int, note: str):
        with self.conn:
            self.conn.execute("UPDATE files SET note=? WHERE id=?", (note, file_id))
        self._refresh_fts(file_id)

    def remove_file(self, file_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            self.conn.execute("DELETE FROM files_fts WHERE rowid=?", (file_id,))

    def get_file(self, file_id: int) -> FileRow | None:
        row = self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._to_filerow(row) if row else None

    def list_files(self, ids: list[int] | None = None, tag: str | None = None, limit: int = 2000) -> list[FileRow]:
        if ids is not None:
            if not ids:
                return []
            rows = {r["id"]: r for r in self.conn.execute(
                f"SELECT * FROM files WHERE id IN ({','.join('?' * len(ids))})", ids)}
            return [self._to_filerow(rows[i]) for i in ids if i in rows]
        if tag:
            rows = self.conn.execute(
                "SELECT f.* FROM files f JOIN file_tags ft ON ft.file_id=f.id JOIN tags t ON t.id=ft.tag_id "
                "WHERE t.name=? ORDER BY f.mtime DESC LIMIT ?", (clean_tag(tag), limit))
        else:
            rows = self.conn.execute("SELECT * FROM files ORDER BY mtime DESC LIMIT ?", (limit,))
        return [self._to_filerow(r) for r in rows]

    def _to_filerow(self, row) -> FileRow:
        return FileRow(row["id"], row["path"], row["name"], row["ext"], row["kind"], row["size"],
                       row["mtime"], row["note"], self.get_tags(row["id"]))

    # ---------- 태그 ----------
    def get_tags(self, file_id: int) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT t.name FROM tags t JOIN file_tags ft ON ft.tag_id=t.id WHERE ft.file_id=? ORDER BY t.name",
            (file_id,))]

    def add_tags(self, file_id: int, tags: list[str], source: str = "user"):
        with self.conn:
            for tag in tags:
                tag = clean_tag(tag)
                if not tag:
                    continue
                self.conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (tag,))
                tag_id = self.conn.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()[0]
                self.conn.execute("INSERT OR IGNORE INTO file_tags(file_id,tag_id,source) VALUES(?,?,?)",
                                  (file_id, tag_id, source))
        self._refresh_fts(file_id)

    def set_tags(self, file_id: int, tags: list[str]):
        """사용자가 편집한 태그 목록으로 교체한다. 남아 있는 태그는 출처(source)를 유지한다."""
        wanted = {clean_tag(t).lower(): clean_tag(t) for t in tags if clean_tag(t)}
        with self.conn:
            for name in self.get_tags(file_id):
                if name.lower() not in wanted:
                    self.conn.execute(
                        "DELETE FROM file_tags WHERE file_id=? AND tag_id=(SELECT id FROM tags WHERE name=?)",
                        (file_id, name))
        self.add_tags(file_id, list(wanted.values()), "user")
        self._prune_tags()

    def all_tags(self) -> list[tuple[str, int]]:
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT t.name, COUNT(ft.file_id) c FROM tags t JOIN file_tags ft ON ft.tag_id=t.id "
            "GROUP BY t.id ORDER BY c DESC, t.name")]

    def _prune_tags(self):
        with self.conn:
            self.conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)")

    # ---------- 전문검색 ----------
    def set_content(self, file_id: int, content: str):
        self._refresh_fts(file_id, content)

    def _refresh_fts(self, file_id: int, content: str | None = None):
        row = self.conn.execute("SELECT name, path, note FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            return
        if content is None:
            old = self.conn.execute("SELECT content FROM files_fts WHERE rowid=?", (file_id,)).fetchone()
            content = old[0] if old else ""
        tags = " ".join(self.get_tags(file_id))
        with self.conn:
            self.conn.execute("DELETE FROM files_fts WHERE rowid=?", (file_id,))
            self.conn.execute("INSERT INTO files_fts(rowid,name,path,note,tags,content) VALUES(?,?,?,?,?,?)",
                              (file_id, row["name"], row["path"], row["note"], tags, content))

    def get_content(self, file_id: int) -> str:
        row = self.conn.execute("SELECT content FROM files_fts WHERE rowid=?", (file_id,)).fetchone()
        return row[0] if row else ""

    def text_search(self, term: str, limit: int = 500) -> list[int]:
        """trigram FTS는 3글자 이상만 색인으로 찾을 수 있어, 짧은 검색어는 LIKE로 찾는다."""
        if len(term) >= 3:
            q = '"' + term.replace('"', '""') + '"'
            rows = self.conn.execute("SELECT rowid FROM files_fts WHERE files_fts MATCH ? LIMIT ?", (q, limit))
        else:
            like = f"%{term}%"
            rows = self.conn.execute(
                "SELECT rowid FROM files_fts WHERE name LIKE ? OR note LIKE ? OR tags LIKE ? OR path LIKE ? "
                "OR content LIKE ? LIMIT ?", (like, like, like, like, like, limit))
        return [r[0] for r in rows]

    # ---------- 임베딩 ----------
    def set_embedding(self, file_id: int, model: str, vec: np.ndarray):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO embeddings(file_id,model,vec) VALUES(?,?,?)",
                              (file_id, model, vec.astype(np.float32).tobytes()))

    def load_embeddings(self, model: str) -> tuple[list[int], np.ndarray | None]:
        rows = self.conn.execute("SELECT file_id, vec FROM embeddings WHERE model=?", (model,)).fetchall()
        if not rows:
            return [], None
        return [r[0] for r in rows], np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])

    def files_without_embedding(self, model: str) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM files WHERE id NOT IN (SELECT file_id FROM embeddings WHERE model=?)", (model,))]

    # ---------- 이동 기록 ----------
    def log_move(self, batch: str, file_id: int, src: str, dst: str):
        with self.conn:
            self.conn.execute("INSERT INTO moves(batch,file_id,src,dst,at) VALUES(?,?,?,?,?)",
                              (batch, file_id, src, dst, time.time()))

    def last_batch(self) -> list[sqlite3.Row]:
        row = self.conn.execute("SELECT batch FROM moves WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return []
        return self.conn.execute("SELECT * FROM moves WHERE batch=? ORDER BY id DESC", (row[0],)).fetchall()

    def mark_undone(self, move_id: int):
        with self.conn:
            self.conn.execute("UPDATE moves SET undone=1 WHERE id=?", (move_id,))
