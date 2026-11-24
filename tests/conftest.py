import os

# Unit tests must never hit paid APIs; live checks live in scripts/smoke_llm.py
os.environ["DISABLE_DEEP_SEARCH"] = "1"
