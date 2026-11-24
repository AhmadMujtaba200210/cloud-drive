import os

# Unit tests must never hit paid APIs; live checks live in scripts/smoke_llm.py
os.environ["DISABLE_DEEP_SEARCH"] = "1"

import pytest


@pytest.fixture(autouse=True)
def _reset_llm_cooldowns():
    from app import llm
    llm._cooldown_until.clear()
    yield
    llm._cooldown_until.clear()
