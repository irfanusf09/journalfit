"""Tests never read or change the real ~/.journalfit (settings, API keys, browser profile)."""

import os
import tempfile

os.environ["JOURNALFIT_HOME"] = tempfile.mkdtemp(prefix="journalfit-test-")
for _k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY"):
    os.environ.pop(_k, None)
