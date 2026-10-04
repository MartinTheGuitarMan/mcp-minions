"""Live v0.2 check against real backends.

  live_v02.py demo   two fast LM Studio models vote; Ollama reasoning model judges disagreements
  live_v02.py down   Ollama must be STOPPED: a route containing it has to raise MINION_DOWN, not fall back
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from minions import tools  # noqa: E402
from minions.config import Roster  # noqa: E402
from minions.errors import MinionError  # noqa: E402

LABELS = ["positive", "negative", "neutral"]
CASES = [
    "Absolutely fantastic, would buy again!",
    "The food was fine, I guess. Service could have been faster though.",
    "Not bad.",
    "I expected worse.",
    "Delivery arrived on the date promised. The box was slightly dented but the contents were intact.",
    "Great, another update that breaks everything.",
]
MODELS = {
    "g1": {"backend": "lmstudio", "model": "google/gemma-3-1b"},
    "e4b": {"backend": "lmstudio", "model": "google/gemma-4-e4b"},
    "q9": {"backend": "ollama", "model": "qwen3.5:9b", "kind": "reasoning", "max_tokens": 3000, "timeout": 600},
}


def demo():
    r = Roster.from_dict({"models": MODELS, "routes": {"classify": {"fast": ["g1", "e4b"], "quorum": 2, "judge": "q9"}}}, meta=True)
    judged = 0
    for text in CASES:
        t = time.time()
        try:
            out = tools.classify(r, text, LABELS)
            m = out["_minion"]
            judged += bool(m["judge"])
            print(f"{time.time() - t:6.1f}s  {out['label']:9} votes={m['votes']} judge={m.get('judge_verdict', '-') if m['judge'] else 'not used'}  | {text[:55]}")
        except MinionError as e:
            print(f"{time.time() - t:6.1f}s  ERROR {str(e)[:120]}  | {text[:55]}")
    print(f"judge used on {judged}/{len(CASES)} cases")


def down():
    r = Roster.from_dict({"models": {**MODELS, "q9": {**MODELS["q9"], "kind": "fast"}},
                          "routes": {"classify": {"fast": ["g1", "q9"], "quorum": 1}}})
    t = time.time()
    try:
        print("UNEXPECTED success:", tools.classify(r, "Not bad.", LABELS))
    except MinionError as e:
        print(f"{time.time() - t:.1f}s  {e}")


{"demo": demo, "down": down}[sys.argv[1]]()
