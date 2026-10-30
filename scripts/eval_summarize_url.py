"""Live eval for summarize_url on the built-in 20 public pages. Kept for its name; the work is done by `eval_urls.py`.

    MINION_ROSTER=path/to/roster.json uv run python scripts/eval_summarize_url.py [--max-words 80]

Bar: 18 of 20 pass. Latency is reported only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_urls import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
