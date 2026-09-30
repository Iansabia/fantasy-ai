"""One place to call an LLM. Local: `claude -p` (Claude CLI login). CI: Anthropic API
(ANTHROPIC_API_KEY) via the official `anthropic` SDK. Calls per run are capped.

The LLM only ever writes explanation text or labels news. It never makes decisions.
"""
from __future__ import annotations

import os
import shutil
import subprocess

DEFAULT_MODEL = os.environ.get("FAI_LLM_MODEL", "claude-haiku-4-5-20251001")
MAX_CALLS = int(os.environ.get("FAI_LLM_MAX_CALLS", "20"))


class LLM:
    def __init__(self, max_calls: int = MAX_CALLS, model: str = DEFAULT_MODEL):
        self.max_calls, self.model, self.calls = max_calls, model, 0

    def available(self) -> bool:
        return bool(shutil.which("claude") or os.environ.get("ANTHROPIC_API_KEY"))

    def __call__(self, prompt: str, timeout: int = 90) -> str | None:
        if self.calls >= self.max_calls:
            return None
        self.calls += 1
        if shutil.which("claude"):
            try:
                r = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=timeout)
            except (subprocess.TimeoutExpired, OSError):
                return None
            return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic
                client = anthropic.Anthropic(timeout=float(timeout), max_retries=1)
                msg = client.messages.create(model=self.model, max_tokens=2048,
                                             messages=[{"role": "user", "content": prompt}])
            except Exception:
                return None
            if msg.stop_reason not in ("end_turn", "stop_sequence"):
                return None
            return "".join(b.text for b in msg.content if b.type == "text").strip() or None
        return None
