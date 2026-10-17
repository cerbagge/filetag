"""파일 종류 판별과 본문 텍스트 추출 (txt/코드/pdf/docx/hwp/hwpx)."""
import re
import struct
import zipfile
import zlib
from pathlib import Path

CODE_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "c++", ".hpp": "c++", ".cs": "c#",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".swift": "swift", ".m": "matlab",
    ".ps1": "powershell", ".sh": "shell", ".bat": "batch", ".sql": "sql", ".html": "html", ".css": "css",
    ".mjs": "javascript", ".ipynb": "jupyter", ".r": "r", ".lua": "lua", ".dart": "dart",
}
TEXT_EXTS = {".txt", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml", ".ini", ".toml",
             ".cfg", ".tex", ".srt", ".rst"}
KINDS = {
    "문서": {".pdf", ".docx", ".doc", ".hwp", ".hwpx", ".pptx", ".ppt", ".xlsx", ".xls", ".odt"} | TEXT_EXTS,
    "이미지": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tif", ".tiff", ".svg"},
    "영상": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm"},
    "음악": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"},
    "압축": {".zip", ".7z", ".rar", ".tar", ".gz"},
    "코드": set(CODE_LANG),
}


def kind_of(ext: str) -> str:
    ext = ext.lower()
    for kind, exts in KINDS.items():
        if ext in exts:
            return kind
    return "기타"


def extract_text(path: Path, limit: int = 20000) -> str:
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXTS or ext in CODE_LANG:
            text = _read_plain(path, limit)
        elif ext == ".pdf":
            text = _read_pdf(path, limit)
        elif ext == ".docx":
            text = _read_docx(path)
        elif ext == ".hwpx":
            text = _read_hwpx(path)
        elif ext == ".hwp":
            text = _read_hwp(path)
        else:
            return ""
    except Exception:
        # 손상/암호화 파일은 본문 없이 메타데이터만으로 처리한다
        return ""
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


def _read_plain(path: Path, limit: int) -> str:
    raw = path.read_bytes()[: limit * 4]
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="ignore")


def _read_pdf(path: Path, limit: int) -> str:
    from pypdf import PdfReader
    parts, total = [], 0
    for page in PdfReader(str(path)).pages[:30]:
        t = page.extract_text() or ""
        parts.append(t)
        total += len(t)
        if total >= limit:
            break
    return "\n".join(parts)


def _read_docx(path: Path) -> str:
    import docx
    return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)


def _read_hwpx(path: Path) -> str:
    out = []
    with zipfile.ZipFile(path) as z:
        sections = sorted(n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n))
        for name in sections:
            xml = z.read(name).decode("utf-8", errors="ignore")
            for para in re.findall(r"<hp:p\b.*?</hp:p>", xml, flags=re.S):
                line = "".join(re.findall(r"<hp:t[^>]*>(.*?)</hp:t>", para, flags=re.S))
                out.append(re.sub(r"<[^>]+>", "", line))
    return "\n".join(out)


HWPTAG_PARA_TEXT = 67
# 8 wchar(16바이트)를 차지하는 확장/인라인 컨트롤 문자
_HWP_WIDE_CTRL = set(range(1, 10)) | set(range(11, 13)) | set(range(14, 24))


def _read_hwp(path: Path) -> str:
    import olefile
    with olefile.OleFileIO(str(path)) as ole:
        header = ole.openstream("FileHeader").read()
        flags = struct.unpack("<I", header[36:40])[0]
        compressed, protected = flags & 1, flags & 4
        if protected:  # 배포용 문서는 암호화되어 있어 읽지 않는다
            return ""
        sections = sorted(
            (e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText" and e[1].startswith("Section")),
            key=lambda e: int(e[1][7:]),
        )
        out = []
        for entry in sections:
            data = ole.openstream(entry).read()
            if compressed:
                data = zlib.decompress(data, -15)
            out.append(_hwp_records_text(data))
    return "\n".join(out)


def _hwp_records_text(data: bytes) -> str:
    out, pos = [], 0
    while pos + 4 <= len(data):
        hdr = struct.unpack("<I", data[pos:pos + 4])[0]
        pos += 4
        tag, size = hdr & 0x3FF, (hdr >> 20) & 0xFFF
        if size == 0xFFF:
            size = struct.unpack("<I", data[pos:pos + 4])[0]
            pos += 4
        if tag == HWPTAG_PARA_TEXT:
            out.append(_hwp_para_text(data[pos:pos + size]))
        pos += size
    return "\n".join(out)


def _hwp_para_text(buf: bytes) -> str:
    chars, i, n = [], 0, len(buf) // 2
    while i < n:
        c = struct.unpack("<H", buf[i * 2:i * 2 + 2])[0]
        if c in _HWP_WIDE_CTRL:
            i += 8
            continue
        if c >= 32:
            chars.append(chr(c))
        elif c in (10, 13):
            chars.append("\n")
        i += 1
    return "".join(chars)
