"""Load ultracode.py as an importable module for tests.

The script reads its configuration from the environment at import time, so each
test loads its own copy with a throwaway STATE_DIR and no live keys or webhooks
— nothing here can touch the real snapshot or post to Discord.
"""
import importlib.util
import os
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, ".github", "scripts", "ultracode.py")


def load(**env):
    """Import ultracode with `env` applied. Returns (module, state_dir)."""
    cfg = {
        "STATE_DIR": tempfile.mkdtemp(),
        "GROQ_API_KEY": "", "GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": "",
        "DISCORD_WEBHOOK": "", "DISCORD_WEBHOOK_IMAGES": "",
        "DISCORD_WEBHOOK_VIDEOS": "", "MAJOR_PING": "",
        "HEARTBEAT_HOURS": "99999",   # never fire a heartbeat mid-test
    }
    cfg.update(env)
    cfg.setdefault("LOG_FILE", os.path.join(cfg["STATE_DIR"], "log.md"))
    os.environ.update(cfg)

    spec = importlib.util.spec_from_file_location("ultracode", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, cfg["STATE_DIR"]


def report(checks):
    """Print PASS/FAIL for [(label, ok), ...] and return a process exit code."""
    for label, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    passed = sum(1 for _, ok in checks if ok)
    print(f"\n{passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1
