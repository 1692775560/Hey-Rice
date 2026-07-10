#!/usr/bin/env python3
"""Download and prepare the local sherpa-onnx wake-word model."""
from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wakeword import REQUIRED_FILES


MODEL_NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
DEFAULT_MODEL_DIR = ROOT / "models" / MODEL_NAME
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    f"{MODEL_NAME}.tar.bz2"
)
KEYWORD_LINE = "x iǎo g uā x iǎo g uā :2.0 #0.25 @小瓜小瓜"


def write_keywords(model_dir: Path) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "keywords.txt").write_text(KEYWORD_LINE + "\n", encoding="utf-8")


def model_is_ready(model_dir: Path) -> bool:
    return all((model_dir / filename).is_file() for filename in REQUIRED_FILES)


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if root not in target.parents and target != root:
            raise RuntimeError(f"unsafe path in model archive: {member.name}")
    archive.extractall(destination)


def install_model(model_dir: Path = DEFAULT_MODEL_DIR) -> Path:
    if model_is_ready(model_dir):
        return model_dir
    if model_dir.exists() and any(model_dir.iterdir()):
        raise RuntimeError(
            f"model directory is incomplete and not empty: {model_dir}. "
            "Move it aside and run this command again."
        )

    model_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hey-rice-kws-") as temp_root:
        archive_path = Path(temp_root) / f"{MODEL_NAME}.tar.bz2"
        print(f"Downloading {MODEL_URL}")
        urllib.request.urlretrieve(MODEL_URL, archive_path)
        with tarfile.open(archive_path, "r:bz2") as archive:
            _safe_extract(archive, Path(temp_root))
        extracted = Path(temp_root) / MODEL_NAME
        if not extracted.is_dir():
            raise RuntimeError("downloaded archive does not contain the expected model")
        if model_dir.exists():
            model_dir.rmdir()
        shutil.move(str(extracted), str(model_dir))

    write_keywords(model_dir)
    if not model_is_ready(model_dir):
        raise RuntimeError("model installation finished but required files are missing")
    return model_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--check", action="store_true", help="only validate local files")
    args = parser.parse_args()

    if args.check:
        if not model_is_ready(args.model_dir):
            raise SystemExit(f"KWS model is not ready: {args.model_dir}")
        print(f"KWS model is ready: {args.model_dir}")
        return

    installed = install_model(args.model_dir)
    print(f"KWS model is ready: {installed}")


if __name__ == "__main__":
    main()
