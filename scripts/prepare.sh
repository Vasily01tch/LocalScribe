#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v swiftc >/dev/null || { echo 'Установите Xcode Command Line Tools'; exit 1; }
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
mkdir -p src-tauri/resources/bin
swiftc native/Recorder.swift -o src-tauri/resources/recorder
cp "$(.venv/bin/python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" src-tauri/resources/bin/ffmpeg
.venv/bin/pyinstaller --noconfirm --clean --onedir --name server --collect-all mlx_whisper --collect-all mlx --collect-all tiktoken --distpath src-tauri/resources backend/server.py
# MLX searches for mlx.metallib beside the loaded libmlx.dylib in a bundle.
cp src-tauri/resources/server/_internal/mlx/lib/mlx.metallib src-tauri/resources/server/_internal/mlx.metallib
.venv/bin/python -c 'from huggingface_hub import snapshot_download; snapshot_download("mlx-community/whisper-large-v2-mlx", local_dir="src-tauri/resources/model")'
echo 'Ресурсы готовы. Выполните npm install && npm run tauri build.'
