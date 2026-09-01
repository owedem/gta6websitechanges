"""The AI provider chain must cascade on FAILURE, not just on a missing key.

Regression for the outage that ran 27-31 Aug 2026: Groq's pinned model was
decommissioned, every write-up 404'd, and because the chain stopped at the first
provider that merely had a key, the working Gemini key was never tried. Every
alert posted "AI write-up unavailable" for five days.
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import load, report  # noqa: E402

uc, STATE = load(GROQ_API_KEY="gk", GEMINI_API_KEY="gm",
                 GROQ_MODEL="llama-3.3-70b-versatile")

CALLS = []


class Resp(io.BytesIO):
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_urlopen(groq_chat, gemini_ok):
    """Stub Groq + Gemini. `groq_chat(model)` returns text, or None to 404."""
    def fake(req, timeout=None):
        url = req.full_url
        body = json.loads(req.data) if getattr(req, "data", None) else {}
        if "groq.com/openai/v1/models" in url:
            CALLS.append("groq:list")
            return Resp(json.dumps({"data": [
                {"id": "whisper-large-v3"},                       # not a chat model
                {"id": "meta-llama/llama-4-maverick-17b-128e-instruct"},
                {"id": "llama-3.1-8b-instant"},
            ]}).encode())
        if "groq.com" in url:
            CALLS.append(f"groq:chat:{body.get('model')}")
            out = groq_chat(body.get("model"))
            if out is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(
                    json.dumps({"error": {"message": "The model `%s` does not exist"
                                          % body.get("model")}}).encode()))
            return Resp(json.dumps({"choices": [{"message": {"content": out}}]}).encode())
        if "generativelanguage" in url:
            CALLS.append("gemini:chat")
            if not gemini_ok:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {},
                                             io.BytesIO(b'{"error":{"message":"quota"}}'))
            return Resp(json.dumps({"candidates": [
                {"content": {"parts": [{"text": "GEMINI WRITE-UP"}]}}]}).encode())
        raise AssertionError("unexpected url " + url)
    return fake


def run(groq_chat, gemini_ok=True):
    CALLS.clear()
    urllib.request.urlopen = make_urlopen(groq_chat, gemini_ok)
    return uc.analyse("some changes")


checks = []

# A pinned model that has been retired must not end the run: rediscover a live
# model from Groq's own list, retry, and remember the winner.
got = run(lambda m: None if "3.3-70b" in m else "GROQ WRITE-UP")
checks.append(("dead pinned model self-heals", got == "GROQ WRITE-UP"))
cached = open(os.path.join(STATE, "_meta", "groq-model.txt"), encoding="utf-8").read()
checks.append(("winning model remembered in state", "maverick" in cached))

# The happy path must not pay for rediscovery.
got = run(lambda m: "GROQ WRITE-UP")
checks.append(("cached model used directly", got == "GROQ WRITE-UP"))
checks.append(("no rediscovery on the happy path", len(CALLS) == 1))

# The actual bug: Groq unusable must fall through to Gemini.
got = run(lambda m: None)
checks.append(("groq dead -> cascades to gemini", got == "GEMINI WRITE-UP"))

# With everything down, the Discord fallback line must name each provider.
got = run(lambda m: None, gemini_ok=False)
checks.append(("all providers dead returns None", got is None))
checks.append(("error names every provider tried",
               "Groq" in uc.LAST_AI_ERROR and "Gemini" in uc.LAST_AI_ERROR))

sys.exit(report(checks))
