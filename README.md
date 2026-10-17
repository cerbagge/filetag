# FileTag — 로컬 AI 파일 태그 관리자

파일에 해시태그·메모를 달아 두고, 자연어로 찾고, AI 제안대로 정리하는 Windows 데스크톱 앱입니다.
AI는 전부 로컬(Intel NPU 우선, 실패 시 CPU)에서 OpenVINO로 실행됩니다.

## 기능 (v0.1.0)
| 기능 | 설명 |
|---|---|
| 파일 등록 | 창에 끌어다 놓기 / `파일 등록`. 태그·메모 입력, AI 태그 제안, 보관 폴더로 복사 |
| 폴더 스캔 | 폴더 전체 등록. 규칙 태그 자동 부여 (종류, 확장자, 연도, 코드 언어, `프로젝트:이름`) |
| 본문 추출 | txt/md/코드, pdf, docx, **hwp, hwpx** |
| 자연어 검색 | 임베딩 의미 검색 + 본문/태그 키워드 검색. `#태그`는 필수 조건 |
| AI 정리 | 검색 결과를 정리 방향(+나눌 폴더)대로 이동하는 안을 AI가 제안 → 검토·수정 → 적용 |
| 되돌리기 | 마지막 정리의 이동을 원래 위치로 복원 |
| 이동 추적 | 다른 프로그램으로 옮긴 파일도 다시 스캔하면 지문(크기+앞뒤 1MB 해시)으로 찾아 태그 유지 |

## 설치
```powershell
cd 04_filetag
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_models.py      # 임베딩 + LLM 다운로드 (약 1.5GB)
.\.venv\Scripts\python.exe run.py
```
모델이 없어도 앱은 실행되며, 키워드·태그 검색만 사용됩니다.

## 데이터 위치
- DB·모델: `%LOCALAPPDATA%\FileTag\` (`FILETAG_HOME` 환경변수로 변경)
- 사용 장치 순서: `FILETAG_DEVICES` (기본 `NPU,CPU`)

## 모델과 NPU
| 용도 | 모델 | 장치 |
|---|---|---|
| 임베딩(의미 검색) | `OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov` | NPU ✅ |
| LLM(태그·정리 제안) | `OpenVINO/Qwen3-1.7B-int4-ov` | NPU ❌ → CPU |

Intel NPU(Meteor Lake)는 **대칭 int4** 모델만 컴파일됩니다. 배포된 `Qwen3-1.7B-int4-ov`는 비대칭 양자화라
NPU 컴파일에 실패하고 CPU로 자동 전환됩니다 (태그 제안 약 5초).

직접 대칭 양자화로 변환하면 NPU에서 돌지만, 1.7B 모델은 품질이 실사용 수준 아래로 떨어집니다 (2026-10-17 측정):

| LLM 구성 | 장치 | 결과 |
|---|---|---|
| 배포본 (비대칭 int4) | CPU | 태그·과목별 정리 모두 정확, 약 5초 |
| 변환 (대칭 채널 int4) | NPU | 태그 2.7초로 빠르나 긴 프롬프트에서 출력 깨짐 |
| 변환 (대칭 group128) | NPU | 폴더 이름 품질 낮음 ("공학/고사/회로"), 27초 |

NPU용 변환 방법 (경로가 길면 DLL 로드에 실패하므로 짧은 경로의 별도 환경 사용):
```powershell
py -3.12 -m venv $env:LOCALAPPDATA\FileTag\cv
& $env:LOCALAPPDATA\FileTag\cv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
& $env:LOCALAPPDATA\FileTag\cv\Scripts\pip install "optimum-intel[openvino]" nncf
& $env:LOCALAPPDATA\FileTag\cv\Scripts\optimum-cli export openvino --model Qwen/Qwen3-1.7B --task text-generation-with-past --weight-format int4 --sym --ratio 1.0 --group-size -1 "$env:LOCALAPPDATA\FileTag\models\llm-npu"
$env:FILETAG_LLM_DIR = "$env:LOCALAPPDATA\FileTag\models\llm-npu"   # 이 모델로 실행
```
첫 실행은 NPU 컴파일로 1분 정도 걸리며, 결과가 `models\cache`에 저장되어 다음부터 빨라집니다.

## 테스트
```powershell
.\.venv\Scripts\python.exe -m pytest -q
```
