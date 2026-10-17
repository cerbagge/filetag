"""PySide6 데스크톱 화면.

무거운 작업(스캔, AI)은 백그라운드 스레드에서 돌린다. SQLite 연결은 스레드마다 따로 열고
AI 모델 객체만 공유한다 (모델 내부에서 lock으로 직렬화).
"""
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout, QWidget,
)

from . import __version__, config
from .ai import AI
from .db import DB
from .library import Library, MovePlan


# ---------- 백그라운드 작업 ----------
class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)


class Task(QRunnable):
    """fn(lib, progress)를 별도 스레드에서 실행한다. lib는 그 스레드 전용 DB 연결을 가진다."""

    def __init__(self, ai: AI, fn):
        super().__init__()
        self.ai, self.fn, self.signals = ai, fn, _Signals()

    def run(self):
        db = DB(config.DB_PATH)
        try:
            result = self.fn(Library(db, self.ai), lambda i, n, s: self.signals.progress.emit(i, n, s))
            self.signals.done.emit(result)
        except Exception:
            self.signals.failed.emit(traceback.format_exc())
        finally:
            db.close()


def split_tags(text: str) -> list[str]:
    return [t.strip().lstrip("#") for t in text.replace("#", ",").split(",") if t.strip().lstrip("#")]


# ---------- 새 파일 등록 ----------
class AddFilesDialog(QDialog):
    def __init__(self, parent: "MainWindow", paths: list[Path]):
        super().__init__(parent)
        self.win, self.paths = parent, paths
        self.setWindowTitle("파일 등록")
        self.resize(520, 360)
        lay = QVBoxLayout(self)
        names = "\n".join(p.name for p in paths[:8]) + (f"\n… 외 {len(paths) - 8}개" if len(paths) > 8 else "")
        lay.addWidget(QLabel(f"등록할 파일 {len(paths)}개:\n{names}"))
        form = QFormLayout()
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("쉼표로 구분: 장학금, 신청서, 2026")
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("나중에 찾을 때 도움이 될 메모")
        self.note.setFixedHeight(70)
        form.addRow("태그", self.tags)
        form.addRow("메모", self.note)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.suggest_btn = QPushButton("AI 태그 제안")
        self.suggest_btn.setEnabled(bool(parent.ai.llm) and len(paths) == 1)
        self.suggest_btn.clicked.connect(self.suggest)
        row.addWidget(self.suggest_btn)
        self.auto_ai = QCheckBox("파일마다 AI 태그 자동 추가")
        self.auto_ai.setEnabled(bool(parent.ai.llm))
        self.auto_ai.setChecked(bool(parent.ai.llm) and len(paths) > 1)
        row.addWidget(self.auto_ai)
        lay.addLayout(row)

        row = QHBoxLayout()
        self.copy = QCheckBox("보관 폴더로 복사:")
        self.store = QLineEdit(parent.settings.value("store_dir", ""))
        browse = QPushButton("…")
        browse.clicked.connect(self.pick_store)
        row.addWidget(self.copy)
        row.addWidget(self.store, 1)
        row.addWidget(browse)
        lay.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def pick_store(self):
        d = QFileDialog.getExistingDirectory(self, "보관 폴더 선택", self.store.text())
        if d:
            self.store.setText(d)
            self.copy.setChecked(True)

    def suggest(self):
        self.suggest_btn.setEnabled(False)
        self.suggest_btn.setText("제안 중…")
        path = self.paths[0]

        def done(tags):
            merged = split_tags(self.tags.text()) + [t for t in tags if t not in split_tags(self.tags.text())]
            self.tags.setText(", ".join(merged))
            self.suggest_btn.setText("AI 태그 제안")
            self.suggest_btn.setEnabled(True)

        self.win.run(lambda lib, _p: lib.suggest_tags_for_path(path), done)

    def accept(self):
        if self.copy.isChecked():
            if not self.store.text().strip():
                QMessageBox.warning(self, "보관 폴더", "보관 폴더를 지정해 주세요.")
                return
            self.win.settings.setValue("store_dir", self.store.text().strip())
        super().accept()

    def values(self):
        return (split_tags(self.tags.text()), self.note.toPlainText().strip(), self.auto_ai.isChecked(),
                self.store.text().strip() if self.copy.isChecked() else None)


# ---------- AI 정리 ----------
class OrganizeDialog(QDialog):
    def __init__(self, parent: "MainWindow", file_ids: list[int]):
        super().__init__(parent)
        self.win, self.file_ids, self.plans = parent, file_ids, []
        self.setWindowTitle("AI 정리 제안")
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"지금 목록에 보이는 파일 {len(file_ids)}개를 정리합니다. 실제 이동은 '적용'을 눌렀을 때만 일어납니다."))
        form = QFormLayout()
        self.instruction = QLineEdit()
        self.instruction.setPlaceholderText("예: 학교 문서를 과목별로 나눠줘 (둘 다 비우면 파일 종류별)")
        self.folder_names = QLineEdit()
        self.folder_names.setPlaceholderText("선택. 쉼표로 구분: 학교, 생활  (적으면 AI가 이 폴더들 중에서만 고릅니다)")
        row = QHBoxLayout()
        self.base = QLineEdit(parent.settings.value("organize_dir", ""))
        browse = QPushButton("…")
        browse.clicked.connect(self.pick_base)
        row.addWidget(self.base, 1)
        row.addWidget(browse)
        form.addRow("정리 방향", self.instruction)
        form.addRow("나눌 폴더", self.folder_names)
        form.addRow("정리할 위치", row)
        lay.addLayout(form)
        self.propose_btn = QPushButton("제안 받기")
        self.propose_btn.clicked.connect(self.propose)
        lay.addWidget(self.propose_btn)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["파일", "새 위치 (체크 해제하면 제외)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        lay.addWidget(self.table, 1)
        self.buttons = QDialogButtonBox()
        self.apply_btn = self.buttons.addButton("적용 (파일 이동)", QDialogButtonBox.AcceptRole)
        self.apply_btn.setEnabled(False)
        self.buttons.addButton("닫기", QDialogButtonBox.RejectRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

    def pick_base(self):
        d = QFileDialog.getExistingDirectory(self, "정리할 위치", self.base.text())
        if d:
            self.base.setText(d)

    def propose(self):
        base = self.base.text().strip()
        if not base:
            QMessageBox.warning(self, "정리할 위치", "정리할 위치를 지정해 주세요.")
            return
        self.win.settings.setValue("organize_dir", base)
        self.propose_btn.setEnabled(False)
        self.propose_btn.setText("AI가 제안을 만드는 중…")
        ids, instr, names = self.file_ids, self.instruction.text(), split_tags(self.folder_names.text())
        self.win.run(lambda lib, _p: lib.propose_moves(ids, instr, base, names), self.show_plans)

    def show_plans(self, plans: list[MovePlan]):
        self.plans = plans
        self.table.setRowCount(len(plans))
        for r, p in enumerate(plans):
            src = QTableWidgetItem(p.src)
            src.setFlags(src.flags() | Qt.ItemIsUserCheckable)
            src.setCheckState(Qt.Checked)
            self.table.setItem(r, 0, src)
            self.table.setItem(r, 1, QTableWidgetItem(p.dst))
        self.propose_btn.setText("다시 제안 받기")
        self.propose_btn.setEnabled(True)
        self.apply_btn.setEnabled(bool(plans))

    def chosen(self) -> list[MovePlan]:
        out = []
        for r, p in enumerate(self.plans):
            if self.table.item(r, 0).checkState() == Qt.Checked:
                out.append(MovePlan(p.file_id, p.src, self.table.item(r, 1).text().strip()))
        return out


# ---------- 메인 창 ----------
class MainWindow(QMainWindow):
    COLS = ["이름", "태그", "종류", "수정일", "폴더"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"FileTag {__version__} — 로컬 AI 파일 태그 관리자")
        self.resize(1300, 800)
        self.setAcceptDrops(True)
        self.settings = QSettings("FileTag", "FileTag")
        self.pool = QThreadPool.globalInstance()
        self.ai = AI()
        self.db = DB(config.DB_PATH)
        self.lib = Library(self.db, self.ai)
        self.shown_ids: list[int] = []
        self.current_id: int | None = None
        self._build()
        self.refresh_tags()
        self.do_search()
        self.load_ai()

    def _build(self):
        tb = QToolBar()
        self.addToolBar(tb)
        tb.addAction("폴더 스캔", self.scan_folder)
        tb.addAction("파일 등록", self.pick_files)
        tb.addSeparator()
        self.organize_act = tb.addAction("AI 정리", self.organize)
        tb.addAction("정리 되돌리기", self.undo_moves)
        tb.addSeparator()
        self.embed_act = tb.addAction("의미 색인 갱신", self.embed_missing)

        central = QWidget()
        root = QVBoxLayout(central)
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("찾고 싶은 것을 자연어로 적으세요. 예: 작년에 낸 장학금 서류 / 전세 계약 관련 #중요")
        self.query.returnPressed.connect(self.do_search)
        go = QPushButton("검색")
        go.clicked.connect(self.do_search)
        row.addWidget(self.query, 1)
        row.addWidget(go)
        root.addLayout(row)

        split = QSplitter()
        self.tag_list = QListWidget()
        self.tag_list.itemClicked.connect(lambda it: self.search_for(f"#{it.data(Qt.UserRole)}"))
        split.addWidget(self.tag_list)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.itemSelectionChanged.connect(self.on_select)
        self.table.cellDoubleClicked.connect(lambda *_: self.open_file())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_menu)
        split.addWidget(self.table)

        detail = QWidget()
        d = QVBoxLayout(detail)
        self.d_name = QLabel("파일을 선택하세요")
        self.d_name.setWordWrap(True)
        self.d_name.setStyleSheet("font-weight: bold; font-size: 14px")
        self.d_path = QLabel()
        self.d_path.setWordWrap(True)
        self.d_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.d_tags = QLineEdit()
        self.d_tags.setPlaceholderText("쉼표로 구분")
        self.d_note = QPlainTextEdit()
        self.d_note.setPlaceholderText("메모")
        self.d_preview = QPlainTextEdit()
        self.d_preview.setReadOnly(True)
        self.d_preview.setPlaceholderText("본문 미리보기")
        self.suggest_btn = QPushButton("AI 태그 제안")
        self.suggest_btn.clicked.connect(self.suggest_tags)
        save = QPushButton("저장")
        save.clicked.connect(self.save_detail)
        btns = QHBoxLayout()
        btns.addWidget(self.suggest_btn)
        btns.addWidget(save)
        for w in (self.d_name, self.d_path, QLabel("태그"), self.d_tags, QLabel("메모"), self.d_note):
            d.addWidget(w)
        d.addLayout(btns)
        d.addWidget(QLabel("본문"))
        d.addWidget(self.d_preview, 1)
        split.addWidget(detail)
        split.setSizes([200, 750, 350])
        root.addWidget(split, 1)
        self.setCentralWidget(central)
        self.status = QLabel()
        self.statusBar().addPermanentWidget(self.status)
        self._set_ai_buttons()

    # ---------- 공통 ----------
    def run(self, fn, on_done=None, label: str | None = None):
        task = Task(self.ai, fn)
        if on_done:
            task.signals.done.connect(on_done)
        task.signals.failed.connect(self.on_failed)
        if label:
            task.signals.progress.connect(lambda i, n, s: self.statusBar().showMessage(f"{label} {i + 1}/{n} {s}"))
            task.signals.done.connect(lambda _r: self.statusBar().showMessage(f"{label} 완료", 5000))
        self.pool.start(task)

    def on_failed(self, tb: str):
        self.statusBar().clearMessage()
        QMessageBox.critical(self, "오류", tb[-2000:])

    def _set_ai_buttons(self):
        self.status.setText(f"AI  {self.ai.status()}")
        self.suggest_btn.setEnabled(bool(self.ai.llm))
        self.embed_act.setEnabled(bool(self.ai.embedder))

    def load_ai(self):
        if not (config.EMBED_MODEL_DIR.exists() or config.LLM_MODEL_DIR.exists()):
            self.status.setText("AI 모델 없음 — setup_models.py 실행 후 다시 시작하세요 (키워드·태그 검색은 사용 가능)")
            return
        self.status.setText("AI 모델 불러오는 중… (첫 실행은 NPU 컴파일로 1~2분 걸립니다)")

        def done(_):
            self._set_ai_buttons()
            if self.ai.errors:
                QMessageBox.warning(self, "AI", "\n\n".join(self.ai.errors)[-1500:])
            if self.ai.embedder:
                self.embed_missing()

        self.run(lambda lib, _p: lib.ai.load(), done)

    # ---------- 목록 ----------
    def refresh_tags(self):
        self.tag_list.clear()
        for name, count in self.db.all_tags():
            it = QListWidgetItem(f"#{name}  ({count})")
            it.setData(Qt.UserRole, name)
            self.tag_list.addItem(it)

    def search_for(self, text: str):
        self.query.setText(text)
        self.do_search()

    def do_search(self):
        q = self.query.text().strip()
        if q and self.ai.embedder:
            self.statusBar().showMessage("검색 중…")
            self.run(lambda lib, _p: lib.search(q), self.show_ids)
        else:
            self.show_ids(self.lib.search(q))

    def show_ids(self, ids: list[int]):
        self.shown_ids = ids
        files = self.db.list_files(ids=ids)
        self.table.setRowCount(len(files))
        for r, f in enumerate(files):
            vals = [f.name, ", ".join(f"#{t}" for t in f.tags), f.kind,
                    datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d"), str(Path(f.path).parent)]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, f.id)
                if not Path(f.path).exists():
                    it.setForeground(Qt.gray)
                self.table.setItem(r, c, it)
        self.statusBar().showMessage(f"{len(files)}개 파일", 5000)

    def selected_ids(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self.table.item(r, 0).data(Qt.UserRole) for r in rows]

    def on_select(self):
        ids = self.selected_ids()
        self.current_id = ids[0] if len(ids) == 1 else None
        f = self.db.get_file(self.current_id) if self.current_id else None
        self.d_name.setText(f.name if f else (f"{len(ids)}개 선택" if ids else "파일을 선택하세요"))
        self.d_path.setText(f.path if f else "")
        self.d_tags.setText(", ".join(f.tags) if f else "")
        self.d_note.setPlainText(f.note if f else "")
        self.d_preview.setPlainText(self.db.get_content(f.id)[:3000] if f else "")

    def save_detail(self):
        if not self.current_id:
            return
        fid = self.current_id
        self.db.set_tags(fid, split_tags(self.d_tags.text()))
        self.db.set_note(fid, self.d_note.toPlainText().strip())
        self.refresh_tags()
        self._update_row(fid)
        if self.ai.embedder:
            self.run(lambda lib, _p: lib.embed(fid))
        self.statusBar().showMessage("저장했습니다", 3000)

    def _update_row(self, fid: int):
        f = self.db.get_file(fid)
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.UserRole) == fid:
                self.table.item(r, 1).setText(", ".join(f"#{t}" for t in f.tags))

    def suggest_tags(self):
        if not self.current_id:
            return
        fid = self.current_id
        self.suggest_btn.setEnabled(False)
        self.suggest_btn.setText("제안 중…")

        def done(tags):
            cur = split_tags(self.d_tags.text())
            if self.current_id == fid:
                self.d_tags.setText(", ".join(cur + [t for t in tags if t not in cur]))
            self.suggest_btn.setText("AI 태그 제안")
            self.suggest_btn.setEnabled(True)
            self.statusBar().showMessage("제안된 태그를 확인하고 '저장'을 누르세요", 5000)

        self.run(lambda lib, _p: lib.suggest_tags(fid), done)

    def table_menu(self, pos):
        ids = self.selected_ids()
        if not ids:
            return
        m = QMenu(self)
        m.addAction("열기", self.open_file)
        m.addAction("폴더에서 보기", self.reveal_file)
        m.addSeparator()
        m.addAction(f"선택한 {len(ids)}개 AI 정리", lambda: self.organize(ids))
        m.addAction("목록에서 제거 (파일은 그대로)", self.remove_selected)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def open_file(self):
        if self.current_id:
            os.startfile(self.db.get_file(self.current_id).path)

    def reveal_file(self):
        if self.current_id:
            subprocess.Popen(["explorer", "/select,", self.db.get_file(self.current_id).path])

    def remove_selected(self):
        ids = self.selected_ids()
        if QMessageBox.question(self, "목록에서 제거", f"{len(ids)}개를 목록에서 제거할까요? 실제 파일은 지우지 않습니다.") \
                != QMessageBox.Yes:
            return
        for fid in ids:
            self.db.remove_file(fid)
        self.refresh_tags()
        self.do_search()

    # ---------- 등록/스캔 ----------
    def scan_folder(self):
        d = QFileDialog.getExistingDirectory(self, "스캔할 폴더")
        if not d:
            return
        use_ai = False
        if self.ai.llm:
            use_ai = QMessageBox.question(
                self, "AI 태그", "파일마다 AI 태그도 붙일까요?\n(NPU에서 파일당 수 초가 걸려 느립니다. "
                "'아니오'면 규칙 태그만 붙이고, 나중에 파일별로 'AI 태그 제안'을 쓸 수 있습니다.)") == QMessageBox.Yes
        self.run(lambda lib, p: lib.scan_folder(d, use_ai, p), self.after_change, "스캔")

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "등록할 파일")
        if files:
            self.add_files([Path(f) for f in files])

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        files = [p for p in paths if p.is_file()]
        folders = [p for p in paths if p.is_dir()]
        if files:
            self.add_files(files)
        for d in folders:
            self.run(lambda lib, p, d=d: lib.scan_folder(d, False, p), self.after_change, "스캔")

    def add_files(self, paths: list[Path]):
        dlg = AddFilesDialog(self, paths)
        if dlg.exec() != QDialog.Accepted:
            return
        tags, note, auto_ai, copy_to = dlg.values()

        def work(lib, progress):
            for i, p in enumerate(paths):
                progress(i, len(paths), p.name)
                lib.add_file(p, tags, note, auto_ai, copy_to)

        self.run(work, self.after_change, "등록")

    def after_change(self, _=None):
        self.refresh_tags()
        self.do_search()

    def embed_missing(self):
        self.run(lambda lib, p: lib.embed_missing(p), None, "의미 색인")

    # ---------- 정리 ----------
    def organize(self, ids: list[int] | None = None):
        ids = ids or self.shown_ids
        if not ids:
            QMessageBox.information(self, "AI 정리", "먼저 검색해서 정리할 파일을 목록에 띄우세요.")
            return
        dlg = OrganizeDialog(self, ids)
        if dlg.exec() != QDialog.Accepted:
            return
        plans = dlg.chosen()
        if QMessageBox.question(self, "파일 이동", f"{len(plans)}개 파일을 이동합니다. 진행할까요?\n"
                                "('정리 되돌리기'로 원래 위치로 돌릴 수 있습니다.)") != QMessageBox.Yes:
            return
        self.run(lambda lib, _p: lib.apply_moves(plans),
                 lambda n: (QMessageBox.information(self, "완료", f"{n}개 파일을 이동했습니다."), self.after_change()))

    def undo_moves(self):
        if QMessageBox.question(self, "정리 되돌리기", "마지막 정리에서 이동한 파일들을 원래 위치로 되돌릴까요?") \
                != QMessageBox.Yes:
            return
        self.run(lambda lib, _p: lib.undo_last_moves(),
                 lambda n: (QMessageBox.information(self, "완료", f"{n}개 파일을 되돌렸습니다."), self.after_change()))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("FileTag")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
