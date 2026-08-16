"""Download a faster-whisper model for offline Local Whisper STT.

One-time download (needs internet). After this, Settings → Voice → Local Whisper
becomes available, and the model is loaded lazily only while you speak.

Usage:
    python -m phantom_ai.voice.install_whisper [tiny|base|small|medium|large]
    python -m phantom_ai.voice.install_whisper base   # recommended on 8 GB laptops
"""

from __future__ import annotations

import argparse
import sys

_SIZES_MB = {
    "tiny": "~39 MB",
    "base": "~74 MB",
    "small": "~460 MB",
    "medium": "~1.5 GB",
    "large": "~3 GB",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("size", nargs="?", default="base",
                    choices=list(_SIZES_MB),
                    help=f"model size (default: base) — sizes: {_SIZES_MB}")
    args = ap.parse_args()

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface-hub is required. Install it with:\n"
              "  pip install --user huggingface_hub faster-whisper")
        return 1

    repo = f"Systran/faster-whisper-{args.size}"
    print(f"Downloading {repo} ({_SIZES_MB[args.size]}) — one time, needs internet…")
    try:
        path = snapshot_download(repo_id=repo)
    except Exception as exc:  # noqa: BLE001
        print(f"Download failed: {exc}")
        print("Check your internet connection and try again.")
        return 1

    print(f"OK — model cached at {path}")
    print("Local Whisper is now available in Settings → Voice → Provider status.")
    print("Remember: `pip install --user faster-whisper` must also be installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
