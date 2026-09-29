"""Keep the deterministic test suite network-free and keyless (SPEC 12).

The demo/pipeline/API gate tests must reproduce the reference trajectory with
the LLM OFF -- the LLM is optional (SPEC 7) and every LLM unit test in
test_llm.py opts back in by patching `settings` itself with a fake key.
Without this pin, a GROQ_API_KEY present in `env` would make those gate tests
do live network calls instead of running the template path.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app import llm as llm_module
from app import pipeline as pipeline_module


@pytest.fixture(autouse=True)
def force_keyless_llm(monkeypatch) -> None:
    keyless = Settings()
    monkeypatch.setattr(pipeline_module, "settings", keyless)
    monkeypatch.setattr(llm_module, "settings", keyless)