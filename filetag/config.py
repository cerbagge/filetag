"""경로와 모델 설정. FILETAG_HOME 환경변수로 데이터 위치를 바꿀 수 있다."""
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("FILETAG_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FileTag")
DB_PATH = APP_DIR / "filetag.db"
MODELS_DIR = APP_DIR / "models"
EMBED_MODEL_DIR = MODELS_DIR / "embed"
# 직접 변환한 NPU용 모델 등을 쓰려면 FILETAG_LLM_DIR로 지정한다
LLM_MODEL_DIR = Path(os.environ.get("FILETAG_LLM_DIR") or MODELS_DIR / "llm")

# 본문은 앞부분만 저장/분석한다 (DB 크기와 모델 입력 길이 제한)
MAX_CONTENT_CHARS = 20000
EMBED_CONTENT_CHARS = 1000
LLM_CONTENT_CHARS = 1500

# NPU 우선, 실패하면 CPU
DEVICE_ORDER = [d.strip() for d in os.environ.get("FILETAG_DEVICES", "NPU,CPU").split(",") if d.strip()]
