import struct
import zipfile
from pathlib import Path

import pytest

from filetag.ai import AI, parse_dict, parse_list
from filetag.db import DB
from filetag.extract import _hwp_records_text, extract_text
from filetag.library import Library


@pytest.fixture
def lib(tmp_path):
    return Library(DB(tmp_path / "t.db"), AI())


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_add_file_rule_and_user_tags(lib, tmp_path):
    f = write(tmp_path / "docs" / "장학금 신청서.txt", "2026학년도 국가장학금 신청 안내")
    fid = lib.add_file(f, tags=["#장학금", "서류"], note="학교 제출용")
    tags = lib.db.get_tags(fid)
    assert {"장학금", "서류", "문서", "txt"} <= set(tags)
    assert lib.db.get_file(fid).note == "학교 제출용"


def test_search_by_text_tag_and_required_tag(lib, tmp_path):
    a = lib.add_file(write(tmp_path / "a.txt", "국가장학금 신청 안내"), tags=["장학금"])
    b = lib.add_file(write(tmp_path / "b.txt", "전기요금 영수증"), tags=["영수증"])
    assert lib.search("국가장학금") == [a]          # 3글자 이상: FTS
    assert lib.search("영수") == [b]                # 2글자: LIKE
    assert set(lib.search("#장학금")) == {a}
    assert lib.search("안내 #영수증") == []


def test_search_strips_josa(lib, tmp_path):
    a = lib.add_file(write(tmp_path / "학교" / "신청서.txt", "x"))
    lib.add_file(write(tmp_path / "집" / "요금.txt", "y"))
    assert lib.search("학교에서 받은") == [a]


def test_project_tag_for_code(lib, tmp_path):
    write(tmp_path / "myapp" / "pyproject.toml", "")
    fid = lib.add_file(write(tmp_path / "myapp" / "src" / "main.py", "print(1)"))
    assert {"python", "코드", "프로젝트:myapp"} <= set(lib.db.get_tags(fid))


def test_moved_file_keeps_tags(lib, tmp_path):
    f = write(tmp_path / "x" / "report.txt", "내용")
    fid = lib.add_file(f, tags=["보고서"])
    moved = tmp_path / "y" / "report.txt"
    moved.parent.mkdir()
    f.rename(moved)
    assert lib.add_file(moved) == fid
    assert "보고서" in lib.db.get_tags(fid)


def test_set_tags_replaces(lib, tmp_path):
    fid = lib.add_file(write(tmp_path / "a.txt", "x"), tags=["옛태그"])
    lib.db.set_tags(fid, ["새태그", "문서"])
    tags = lib.db.get_tags(fid)
    assert "새태그" in tags and "옛태그" not in tags
    assert "옛태그" not in [t for t, _ in lib.db.all_tags()]


def test_organize_and_undo(lib, tmp_path):
    a = lib.add_file(write(tmp_path / "in" / "a.txt", "x"))
    b = lib.add_file(write(tmp_path / "in" / "b.py", "y"))
    out = tmp_path / "out"
    plans = lib.propose_moves([a, b], "", out)  # LLM 없음: 종류별
    assert {Path(p.dst).parent.name for p in plans} == {"문서", "코드"}
    assert lib.apply_moves(plans) == 2
    assert (out / "문서" / "a.txt").exists()
    assert lib.db.get_file(a).path == str(out / "문서" / "a.txt")
    assert lib.undo_last_moves() == 2
    assert (tmp_path / "in" / "a.txt").exists()
    assert lib.db.get_file(a).path == str(tmp_path / "in" / "a.txt")


def test_copy_to_storage(lib, tmp_path):
    src = write(tmp_path / "dl" / "memo.txt", "x")
    fid = lib.add_file(src, tags=["메모"], copy_to=tmp_path / "보관함")
    assert lib.db.get_file(fid).path == str((tmp_path / "보관함" / "memo.txt").resolve())
    assert src.exists()


def test_hwpx_extract(tmp_path):
    p = tmp_path / "a.hwpx"
    xml = '<hs:sec><hp:p><hp:run><hp:t>안녕하세요</hp:t></hp:run></hp:p><hp:p><hp:run><hp:t>한글 문서</hp:t></hp:run></hp:p></hs:sec>'
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Contents/section0.xml", xml)
    assert extract_text(p) == "안녕하세요\n한글 문서"


def test_hwp_record_parser():
    # 확장 컨트롤(0x0B, 8 wchar) 뒤에 본문이 오는 PARA_TEXT 레코드
    body = struct.pack("<8H", 11, 0, 0, 0, 0, 0, 0, 11) + "한글 본문".encode("utf-16-le") + struct.pack("<H", 13)
    rec = struct.pack("<I", 67 | (len(body) << 20)) + body
    other = struct.pack("<I", 66 | (4 << 20)) + b"\0" * 4
    assert _hwp_records_text(other + rec).strip() == "한글 본문"


def test_parse_llm_output():
    assert parse_list('태그는 ["장학금", "#신청서"] 입니다') == ["장학금", "신청서"]
    assert parse_list("장학금, 신청서") == ["장학금", "신청서"]
    assert parse_dict('결과: {"3": "영수증"}') == {"3": "영수증"}
