"""파일 등록·스캔·검색·정리를 묶은 핵심 로직. UI와 테스트가 함께 사용한다."""
import hashlib
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from . import config
from .ai import AI
from .db import DB, clean_tag
from .extract import CODE_LANG, extract_text, kind_of

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode", "$RECYCLE.BIN",
             "System Volume Information", ".gradle", "build", "dist"}
PROJECT_MARKERS = (".git", "package.json", "pyproject.toml", "requirements.txt", "build.gradle", "pom.xml",
                   "Cargo.toml", "go.mod", "CMakeLists.txt")


def fingerprint(path: Path) -> str:
    """크기 + 앞/뒤 1MB 해시. 큰 파일도 빠르게 계산하며, 이동된 파일을 다시 찾는 데 쓴다."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read())
    return h.hexdigest()


# 키워드 검색 전에 떼어 낼 흔한 조사/어미 (긴 것부터 검사)
JOSA = sorted(["에서", "으로", "에게", "하고", "이랑", "받는", "관련", "로", "을", "를", "이", "가", "은", "는", "의",
               "에", "와", "과", "랑", "도", "만"], key=len, reverse=True)


def strip_josa(word: str) -> str:
    for j in JOSA:
        if word.endswith(j) and len(word) - len(j) >= 2:
            return word[:-len(j)]
    return word


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
        i += 1


@dataclass
class MovePlan:
    file_id: int
    src: str
    dst: str


class Library:
    def __init__(self, db: DB, ai: AI | None = None):
        self.db = db
        self.ai = ai or AI()
        self._vec_cache: tuple[str, list[int], np.ndarray | None] | None = None
        self._project_cache: dict[Path, str | None] = {}

    # ---------- 등록 ----------
    def add_file(self, path: Path | str, tags: list[str] = (), note: str = "", use_ai_tags: bool = False,
                 copy_to: Path | str | None = None) -> int:
        path = Path(path).resolve()
        if copy_to:
            target = unique_path(Path(copy_to) / path.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            path = target
        st = path.stat()
        fp = fingerprint(path)
        existing = self.db.get_by_path(str(path))
        moved = None if existing else self.db.find_missing_by_fingerprint(fp)
        if moved:  # 다른 프로그램으로 옮겨진 파일이면 태그를 유지한 채 경로만 바꾼다
            self.db.update_path(moved["id"], str(path))
        file_id = self.db.upsert_file(str(path), path.name, path.suffix.lower().lstrip("."),
                                      kind_of(path.suffix), st.st_size, st.st_mtime, fp)
        unchanged = existing and existing["fingerprint"] == fp
        if not unchanged and not moved:
            content = extract_text(path, config.MAX_CONTENT_CHARS)
            self.db.set_content(file_id, content)
            self.db.add_tags(file_id, self.rule_tags(path, st.st_mtime), "rule")
            if use_ai_tags and self.ai.llm:
                self.db.add_tags(file_id, self.suggest_tags(file_id), "ai")
        if tags:
            self.db.add_tags(file_id, list(tags), "user")
        if note:
            self.db.set_note(file_id, note)
        if self.ai.embedder and (not unchanged or tags or note):
            self.embed(file_id)
        return file_id

    def scan_folder(self, folder: Path | str, use_ai_tags: bool = False, progress=None) -> int:
        paths = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            paths += [Path(root) / f for f in files if not f.startswith("~$")]
        for i, p in enumerate(paths):
            if progress and progress(i, len(paths), p.name) is False:
                break
            try:
                self.add_file(p, use_ai_tags=use_ai_tags)
            except OSError:
                continue  # 잠긴 파일이나 권한 없는 파일은 건너뛴다
        return len(paths)

    def rule_tags(self, path: Path, mtime: float) -> list[str]:
        tags = [kind_of(path.suffix), datetime.fromtimestamp(mtime).strftime("%Y")]
        if path.suffix:
            tags.append(path.suffix.lower().lstrip("."))
        if path.suffix.lower() in CODE_LANG:
            tags.append(CODE_LANG[path.suffix.lower()])
        project = self._project_of(path.parent)
        if project:
            tags.append(f"프로젝트:{project}")
        return tags

    def _project_of(self, folder: Path) -> str | None:
        chain = []
        cur = folder
        for _ in range(6):
            if cur in self._project_cache:
                found = self._project_cache[cur]
                break
            chain.append(cur)
            if any((cur / m).exists() for m in PROJECT_MARKERS):
                found = cur.name
                break
            if cur.parent == cur:
                found = None
                break
            cur = cur.parent
        else:
            found = None
        for c in chain:
            self._project_cache[c] = found
        return found

    # ---------- AI ----------
    def embed_text(self, file_id: int) -> str:
        f = self.db.get_file(file_id)
        content = self.db.get_content(file_id)[:config.EMBED_CONTENT_CHARS]
        return f"{f.name}\n폴더: {Path(f.path).parent.name}\n태그: {', '.join(f.tags)}\n메모: {f.note}\n{content}"

    def embed(self, file_id: int):
        vec = self.ai.embedder.embed_documents([self.embed_text(file_id)])[0]
        self.db.set_embedding(file_id, self.ai.embedder.model_id, vec)
        self._vec_cache = None

    def embed_missing(self, progress=None) -> int:
        if not self.ai.embedder:
            return 0
        ids = self.db.files_without_embedding(self.ai.embedder.model_id)
        for i, fid in enumerate(ids):
            if progress and progress(i, len(ids), "") is False:
                break
            self.embed(fid)
        return len(ids)

    def suggest_tags(self, file_id: int) -> list[str]:
        f = self.db.get_file(file_id)
        return self._suggest(f.name, Path(f.path).parent.name, self.db.get_content(file_id))

    def suggest_tags_for_path(self, path: Path | str) -> list[str]:
        """아직 등록하지 않은 파일(끌어다 놓은 새 파일)용."""
        path = Path(path)
        return self._suggest(path.name, path.parent.name, extract_text(path, config.LLM_CONTENT_CHARS))

    def _suggest(self, name: str, folder: str, content: str) -> list[str]:
        known = [t for t, _ in self.db.all_tags()]
        tags = self.ai.llm.suggest_tags(name, folder, content, known)
        return [t for t in (clean_tag(x) for x in tags) if t and len(t) <= 20]

    # ---------- 검색 ----------
    def search(self, query: str, limit: int = 200) -> list[int]:
        """'#태그'는 반드시 포함할 조건으로, 나머지 문장은 의미 검색 + 키워드 검색으로 처리한다."""
        words = query.split()
        required = [clean_tag(w) for w in words if w.startswith("#") and clean_tag(w)]
        text = " ".join(w for w in words if not w.startswith("#")).strip()
        scores: dict[int, float] = {}

        if text and self.ai.embedder:
            ids, mat = self._vectors()
            if mat is not None:
                sims = mat @ self.ai.embedder.embed_query(text)
                # 무관한 파일도 0.3~0.4가 나오므로 최고 점수 근처만 남긴다
                cutoff = max(0.35, float(sims.max()) - 0.1)
                for idx in np.argsort(-sims)[:100]:
                    if sims[idx] >= cutoff:
                        scores[ids[idx]] = scores.get(ids[idx], 0) + float(sims[idx])
        tag_names = {t.lower() for t, _ in self.db.all_tags()}
        for w in (strip_josa(w) for w in text.split()):
            for fid in self.db.text_search(w):
                scores[fid] = scores.get(fid, 0) + 0.3
            if w.lower() in tag_names:
                for f in self.db.list_files(tag=w):
                    scores[f.id] = scores.get(f.id, 0) + 0.5

        if not text:
            if not required:
                return [f.id for f in self.db.list_files(limit=limit)]
            scores = {f.id: 0 for f in self.db.list_files(tag=required[0])}
        if required:
            scores = {fid: s for fid, s in scores.items()
                      if {t.lower() for t in required} <= {t.lower() for t in self.db.get_tags(fid)}}
        return sorted(scores, key=lambda k: -scores[k])[:limit]

    def _vectors(self):
        model = self.ai.embedder.model_id
        if not self._vec_cache or self._vec_cache[0] != model:
            ids, mat = self.db.load_embeddings(model)
            self._vec_cache = (model, ids, mat)
        return self._vec_cache[1], self._vec_cache[2]

    # ---------- 정리 ----------
    def propose_moves(self, file_ids: list[int], instruction: str, base_dir: Path | str,
                      folder_names: list[str] = ()) -> list[MovePlan]:
        files = self.db.list_files(ids=file_ids)
        if self.ai.llm and (instruction.strip() or folder_names):
            folders = self.ai.llm.propose_folders(instruction, [(f.id, f.name, f.tags) for f in files], folder_names)
        else:  # LLM이 없으면 파일 종류별로 나눈다
            folders = {f.id: f.kind for f in files}
        plans, taken = [], set()
        for f in files:
            dst = Path(base_dir) / folders.get(f.id, "기타") / f.name
            stem, i = dst, 1
            while dst.exists() or str(dst).lower() in taken:
                dst = stem.with_name(f"{stem.stem} ({i}){stem.suffix}")
                i += 1
            taken.add(str(dst).lower())
            if Path(f.path) != dst:
                plans.append(MovePlan(f.id, f.path, str(dst)))
        return plans

    def apply_moves(self, plans: list[MovePlan]) -> int:
        batch = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        done = 0
        for p in plans:
            if not Path(p.src).exists():
                continue
            Path(p.dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(p.src, p.dst)
            self.db.update_path(p.file_id, p.dst)
            self.db.log_move(batch, p.file_id, p.src, p.dst)
            done += 1
        return done

    def undo_last_moves(self) -> int:
        n = 0
        for m in self.db.last_batch():
            if Path(m["dst"]).exists() and not Path(m["src"]).exists():
                Path(m["src"]).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(m["dst"], m["src"])
                self.db.update_path(m["file_id"], m["src"])
                n += 1
            self.db.mark_undone(m["id"])
        return n
