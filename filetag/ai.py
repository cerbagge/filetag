"""OpenVINO GenAI 기반 로컬 AI: 임베딩(의미 검색)과 LLM(태그·정리 제안).

NPU를 먼저 시도하고 실패하면 config.DEVICE_ORDER의 다음 장치로 넘어간다.
모델 폴더가 없으면 해당 기능은 None으로 남고 앱은 키워드/태그 검색만 사용한다.
"""
import json
import re
import threading
from pathlib import Path

import numpy as np

from . import config

# NPU 컴파일 결과를 저장해 두 번째 실행부터 모델 로딩을 빠르게 한다
CACHE_DIR = str(config.MODELS_DIR / "cache")
QUERY_INSTRUCTION ="Instruct: Given a search request, retrieve the user's files that match it\nQuery: "


def _model_id(model_dir: Path) -> str:
    f = model_dir / "MODEL_ID"
    return f.read_text(encoding="utf-8").strip() if f.exists() else model_dir.name


class Embedder:
    def __init__(self, model_dir: Path, devices: list[str]):
        import openvino_genai as g
        self.model_id = _model_id(model_dir)
        self.lock = threading.Lock()
        last_err = None
        for device in devices:
            cfg = g.TextEmbeddingPipeline.Config()
            cfg.pooling_type = g.TextEmbeddingPipeline.PoolingType.LAST_TOKEN
            cfg.normalize = True
            cfg.padding_side = "left"
            cfg.query_instruction = QUERY_INSTRUCTION
            if device == "NPU":  # NPU는 고정 입력 크기가 필요하다
                cfg.max_length = 512
                cfg.pad_to_max_length = True
                cfg.batch_size = 1
            try:
                self.pipe = g.TextEmbeddingPipeline(str(model_dir), device, cfg, CACHE_DIR=CACHE_DIR)
                self.device = device
                return
            except Exception as e:
                last_err = e
        raise RuntimeError(f"임베딩 모델을 불러오지 못했습니다: {last_err}")

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        with self.lock:
            out = [np.asarray(self.pipe.embed_documents([t]), dtype=np.float32)[0] for t in texts]
        return np.stack(out)

    def embed_query(self, text: str) -> np.ndarray:
        with self.lock:
            return np.asarray(self.pipe.embed_query(text), dtype=np.float32)


class LLM:
    def __init__(self, model_dir: Path, devices: list[str]):
        import openvino_genai as g
        self.model_id = _model_id(model_dir)
        self.lock = threading.Lock()
        last_err = None
        for device in devices:
            props = {"MAX_PROMPT_LEN": 1536, "MIN_RESPONSE_LEN": 256} if device == "NPU" else {}
            props["CACHE_DIR"] = CACHE_DIR
            try:
                self.pipe = g.LLMPipeline(str(model_dir), device, **props)
                self.device = device
                return
            except Exception as e:
                last_err = e
        raise RuntimeError(f"LLM을 불러오지 못했습니다: {last_err}")

    def ask(self, prompt: str, max_new_tokens: int = 200) -> str:
        # Qwen3의 사고(think) 모드를 끄고 답만 받는다
        with self.lock:
            out = str(self.pipe.generate(prompt + " /no_think", max_new_tokens=max_new_tokens))
        return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()

    def suggest_tags(self, name: str, folder: str, content: str, known_tags: list[str]) -> list[str]:
        known = ", ".join(known_tags[:60]) or "(없음)"
        prompt = (
            "다음 파일을 나중에 쉽게 찾을 수 있도록 한국어 태그를 3~7개 붙여 주세요.\n"
            "주제, 문서 종류, 관련 기관/과목/프로젝트, 용도 위주로 짧은 명사로 만드세요.\n"
            f"이미 사용 중인 태그가 맞으면 그대로 재사용하세요: {known}\n"
            f"파일명: {name}\n폴더: {folder}\n본문 일부:\n{content[:config.LLM_CONTENT_CHARS] or '(본문 없음)'}\n\n"
            '답은 JSON 배열 하나로만 하세요. 예: ["장학금", "신청서", "2026"]'
        )
        return parse_list(self.ask(prompt, 120))[:7]

    def propose_folders(self, instruction: str, items: list[tuple[int, str, list[str]]],
                        folders: list[str] = ()) -> dict[int, str]:
        """items: (파일 id, 파일명, 태그). folders를 주면 그 안에서만 고른다. 반환: 파일 id -> 폴더 이름."""
        # 소형 모델은 문장에서 폴더 이름을 잘 뽑지 못하므로, 이름은 사용자가 직접 준다
        cats = [safe_folder_name(c) for c in folders if c.strip()]
        rule =(f"각 파일은 반드시 다음 폴더 중 하나에 넣으세요: {json.dumps(cats, ensure_ascii=False)}. "
                "어디에도 맞지 않으면 \"기타\".\n" if cats else
                "요청의 기준으로 파일들을 묶을 짧은 한국어 폴더 이름(2~6글자)을 정하세요. "
                "폴더 이름은 파일명이 아닙니다. 여러 파일이 같은 폴더를 공유해야 합니다.\n"
                "예: 요청 '과목별로' -> 공업수학_과제.pdf: 공업수학, 공업수학_시험.pdf: 공업수학\n")
        result: dict[int, str] = {}
        names = {i: name for i, name, _ in items}
        for start in range(0, len(items), 10):  # 10개씩 나눠 묻는다
            chunk = items[start:start + 10]
            lines = "\n".join(f"{i}: {name} [{', '.join(tags)}]" for i, name, tags in chunk)
            prompt = (
                f"사용자의 정리 요청: {instruction}\n{rule}"
                "대괄호 안은 파일의 태그이며 판단 참고용입니다.\n"
                f"{lines}\n\n"
                '답은 JSON 객체 하나로만 하세요. 키는 파일 번호, 값은 폴더 이름. 예: {"12": "폴더A", "15": "폴더B"}'
            )
            for k, v in parse_dict(self.ask(prompt, 300)).items():
                if str(k).isdigit() and isinstance(v, str) and v.strip():
                    name = safe_folder_name(v)
                    if (cats and name not in cats) or name == names.get(int(k)) or "." in name:
                        name = "기타"  # 목록 밖이거나 파일명을 그대로 베낀 답
                    result[int(k)] = name
        return result


def parse_list(text: str) -> list[str]:
    m = re.search(r"\[.*?\]", text, flags=re.S)
    if m:
        try:
            return [str(x).strip().lstrip("#") for x in json.loads(m.group(0)) if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return [t.strip().strip('"\'').lstrip("#") for t in re.split(r"[,\n#]", text) if 0 < len(t.strip()) <= 20]


def parse_dict(text: str) -> dict:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def safe_folder_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name.strip()).strip(". ")[:40] or "기타"


class AI:
    """앱 전체가 공유하는 AI 상태. 모델은 처음 쓸 때 불러온다."""

    def __init__(self):
        self.embedder: Embedder | None = None
        self.llm: LLM | None = None
        self.errors: list[str] = []

    def load(self):
        if config.EMBED_MODEL_DIR.exists() and not self.embedder:
            try:
                self.embedder = Embedder(config.EMBED_MODEL_DIR, config.DEVICE_ORDER)
            except Exception as e:
                self.errors.append(str(e))
        if config.LLM_MODEL_DIR.exists() and not self.llm:
            try:
                self.llm = LLM(config.LLM_MODEL_DIR, config.DEVICE_ORDER)
            except Exception as e:
                self.errors.append(str(e))

    def status(self) -> str:
        parts = [f"임베딩: {self.embedder.device}" if self.embedder else "임베딩: 없음",
                 f"LLM: {self.llm.device}" if self.llm else "LLM: 없음"]
        return " | ".join(parts)
