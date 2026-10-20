"""`mcp-minions init`: write a commented starter config file. Never overwrites without --force, never contacts a
runtime or a model, and writes no keys."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_PATH = "~/.config/minions/minions.toml"

STARTER = '''# mcp-minions config. Point MINION_CONFIG at this file. Real environment variables always win over it.
# Never put API keys here: a cloud model names the environment variable that holds its key (key_env).

[machine]                         # flags for this computer (all optional)
lifecycle     = false             # true: minion_status also shows loaded/installed models and whether a load is safe
min_free_pct  = 45                # free memory needed before a managed model is loaded
quiet_windows = []                # e.g. ["23:00-06:00"]: never load inside these local-time windows
allow_battery = false             # false: do not load models on battery
# log = "~/.minions/usage.jsonl"  # usage log (counts and timings, never the texts); enables minion_report

[models.main]
backend = "{backend}"             # "lmstudio" or "ollama"
model   = "{model}"               # the model name exactly as your runtime lists it
kind    = "fast"
# manage = true                   # let minions load/unload it (minion_load / minion_unload)
# mem_gb = 5                      # about how much memory it needs when loaded
# ttl_s  = 600                    # the runtime unloads it after this many idle seconds

# A second, bigger model that only steps in when the fast ones disagree:
# [models.judge]
# backend = "{backend}"
# model   = "<a-reasoning-model>"
# kind    = "reasoning"
#
# [routes.classify]
# fast  = ["main"]
# judge = "judge"

# A hosted model, for public text only (never used for sensitive calls, which is the default):
# [models.cloud]
# backend = "openai-compat"
# base_url = "https://api.example.com/v1"
# model = "<model id>"
# cloud = true
# key_env = "EXAMPLE_API_KEY"
'''


def write_starter(path: str, backend: str = "lmstudio", model: str = "<your-model>", force: bool = False) -> Path:
    p = Path(path).expanduser()
    if p.exists() and not force:
        raise FileExistsError(f"{p} already exists; use --force to overwrite it")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(STARTER.format(backend=backend, model=model))
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mcp-minions init", description="Write a commented starter config file.")
    ap.add_argument("--path", default=DEFAULT_PATH, help=f"where to write it (default {DEFAULT_PATH})")
    ap.add_argument("--backend", choices=["lmstudio", "ollama"], default="lmstudio")
    ap.add_argument("--model", default="<your-model>", help="model name as your runtime lists it")
    ap.add_argument("--force", action="store_true", help="overwrite an existing file")
    a = ap.parse_args(argv)
    try:
        p = write_starter(a.path, a.backend, a.model, a.force)
    except FileExistsError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Wrote {p}\nNext: edit the model name, then add MINION_CONFIG to your MCP client entry:\n"
          f'  "env": {{ "MINION_CONFIG": "{p}" }}')
    return 0
