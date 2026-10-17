# FileTag — Local AI File Tagger

A Windows desktop app that lets you attach hashtags and notes to your files, find them later with natural-language search, and reorganize them with AI suggestions.
All AI runs **locally** with OpenVINO — on the Intel NPU when possible, falling back to the CPU. No file content ever leaves your machine.

> The user interface is currently in Korean.

## Features (v0.1.0)
| Feature | Description |
|---|---|
| Add files | Drag & drop onto the window or use *Add files*. Enter tags and a note, get AI tag suggestions, optionally copy into a storage folder |
| Folder scan | Register a whole folder. Rule-based tags are added automatically (kind, extension, year, code language, `project:<name>`) |
| Text extraction | txt/md/source code, pdf, docx, **hwp, hwpx** (Hangul word processor) |
| Natural-language search | Embedding-based semantic search + keyword search over content and tags. `#tag` acts as a required filter |
| AI organize | AI proposes how to move the current search results into folders following your instruction (and optional folder names) → review/edit → apply |
| Undo | Moves from the last organize run can be restored to their original locations |
| Move tracking | Files moved by other programs are re-linked on the next scan via a fingerprint (size + hash of first/last 1 MB), keeping their tags |

## Installation
Requires Windows and Python 3.12.
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe setup_models.py      # downloads the embedding model + LLM (~1.5 GB)
.\.venv\Scripts\python.exe run.py
```
The app also runs without models; in that case only keyword and tag search are available.

## Configuration
| Environment variable | Default | Purpose |
|---|---|---|
| `FILETAG_HOME` | `%LOCALAPPDATA%\FileTag` | Location of the database and models |
| `FILETAG_DEVICES` | `NPU,CPU` | Device order to try |
| `FILETAG_LLM_DIR` | `<FILETAG_HOME>\models\llm` | Use a different LLM folder (e.g. an NPU-converted model) |

## Models and the NPU
| Purpose | Model | Device |
|---|---|---|
| Embeddings (semantic search) | `OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov` | NPU ✅ |
| LLM (tag / organize suggestions) | `OpenVINO/Qwen3-1.7B-int4-ov` | NPU ❌ → CPU |

The Intel NPU on Meteor Lake (Core Ultra series 1) only compiles **symmetric int4** models. The published `Qwen3-1.7B-int4-ov` uses asymmetric quantization, so NPU compilation fails and the app automatically falls back to the CPU (about 5 s per tag suggestion).

You can convert the model to symmetric int4 yourself so that it runs on the NPU, but in our tests a 1.7B model loses too much quality:

| LLM build | Device | Result |
|---|---|---|
| Published (asymmetric int4) | CPU | Accurate tags and folder grouping, ~5 s |
| Converted (symmetric, per-channel int4) | NPU | Faster tags (~2.7 s), but output breaks on longer prompts |
| Converted (symmetric, group size 128) | NPU | Poor folder names, ~27 s |

To convert for the NPU (use a separate environment with a **short path** — long paths make the OpenVINO PyTorch frontend DLL fail to load):
```powershell
py -3.12 -m venv C:\ftcv
C:\ftcv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
C:\ftcv\Scripts\pip install "optimum-intel[openvino]" nncf
C:\ftcv\Scripts\optimum-cli export openvino --model Qwen/Qwen3-1.7B --task text-generation-with-past --weight-format int4 --sym --ratio 1.0 --group-size -1 "$env:LOCALAPPDATA\FileTag\models\llm-npu"
$env:FILETAG_LLM_DIR = "$env:LOCALAPPDATA\FileTag\models\llm-npu"
```
The first NPU run takes about a minute to compile; the result is cached in `models\cache`, so later starts are much faster.

## Tests
```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Roadmap
- Image content analysis with a vision model
- Larger LLM (e.g. Qwen3-4B) on the NPU
- Watching folders and registering new files automatically

## License
[MIT](LICENSE)
