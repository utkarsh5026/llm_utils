"""One real call against your Azure deployment. Skipped unless the env vars are set:

AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_API_KEY=... AZURE_OPENAI_DEPLOYMENT=... uv run pytest -m live -s
"""

import os

import pytest
from pydantic import BaseModel

import azure_openai_utils as az

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_DEPLOYMENT")),
        reason="set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT to run live tests",
    ),
]


class Invoice(BaseModel):
    vendor: str
    total: float
    due_date: str | None


def test_live_extract(tmp_path):
    path = az.enable_logging(tmp_path / "calls.jsonl")
    try:
        res = az.extract(
            "Invoice #1042 from Acme Corp. Amount due: $1,249.50, payable by 2026-10-31.",
            Invoice,
            instructions="Dates as YYYY-MM-DD.",
            tag="live-test",
        )
    finally:
        az.disable_logging()

    assert "acme" in res.parsed.vendor.lower()
    assert res.parsed.total == pytest.approx(1249.5)
    assert res.usage.input_tokens > 0 and res.usage.output_tokens > 0

    [row] = az.read_log(path)
    assert row["status"] == "ok" and row["model"] == res.model
    print(f"\nmodel={res.model} cost={res.cost}\n{az.tracker.summary()}")
