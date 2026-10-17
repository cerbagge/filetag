"""OpenVINO로 미리 변환된 모델을 Hugging Face에서 받아 데이터 폴더에 저장한다.

사용법: python setup_models.py [--embed REPO] [--llm REPO]
"""
import argparse

from huggingface_hub import snapshot_download

from filetag import config

DEFAULT_EMBED = "OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov"
DEFAULT_LLM = "OpenVINO/Qwen3-1.7B-int4-ov"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed", default=DEFAULT_EMBED)
    ap.add_argument("--llm", default=DEFAULT_LLM)
    ap.add_argument("--skip-llm", action="store_true")
    args = ap.parse_args()

    targets = [(args.embed, config.EMBED_MODEL_DIR)]
    if not args.skip_llm:
        targets.append((args.llm, config.LLM_MODEL_DIR))
    for repo, dest in targets:
        print(f"다운로드: {repo} -> {dest}")
        snapshot_download(repo_id=repo, local_dir=str(dest))
        (dest / "MODEL_ID").write_text(repo, encoding="utf-8")
    print("완료")


if __name__ == "__main__":
    main()
