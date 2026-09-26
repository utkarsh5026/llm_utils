# Recipes

Complete programs for common jobs. Each one assumes `azure_openai_utils.py` is next to your script and your
[environment variables](getting-started.md#3-tell-it-where-your-deployment-is) are set.

## Triage support tickets

Sort incoming tickets into categories and urgency levels, and print what the batch cost.

```python
from typing import Literal

from pydantic import BaseModel, Field

import azure_openai_utils as az


class Triage(BaseModel):
    reasoning: str = Field(description="One or two sentences explaining the choice")  # (1)!
    category: Literal["billing", "bug", "outage", "feature_request", "account", "other"]
    urgency: Literal["low", "normal", "high"]
    summary: str = Field(description="At most 12 words")


SYSTEM = """You triage support tickets for a SaaS product.
'high' urgency means many users are blocked or money is being lost right now."""

tickets = [
    "I was charged twice for September, please refund one of them.",
    "The export button does nothing on Safari.",
    "EVERYTHING is down, our whole team is locked out!!",
    "Would love a dark mode.",
]

with az.track("triage") as t:
    results = az.structured_many(tickets, Triage, system=SYSTEM, concurrency=8, tag="triage")

for res in results:
    r = res.parsed
    print(f"[{r.urgency:>6}] {r.category:<16} {r.summary}")

print()
print(t.summary())
```

1. Putting `reasoning` first makes the model explain its decision *before* it picks a category, which tends to
   improve the choice. You can ignore the field afterwards.

```text
[normal] billing          Customer charged twice for September, requests refund
[normal] bug              Export button unresponsive in Safari
[  high] outage           Whole team locked out, service appears down
[   low] feature_request  Request for a dark mode
```

## Turn a folder of invoices into a CSV

Extract the same fields from every document, skip the ones that fail, and write a spreadsheet.

```python
import csv
from pathlib import Path

from pydantic import BaseModel, Field

import azure_openai_utils as az


class Invoice(BaseModel):
    invoice_number: str | None
    vendor: str
    issue_date: str | None = Field(description="YYYY-MM-DD")
    total: float
    currency: str = Field(description="ISO 4217 code, e.g. USD")


files = sorted(Path("invoices").glob("*.txt"))
az.enable_logging("logs/invoices.jsonl")  # keep a record of every question and answer

with az.track("invoice-import") as t:
    results = az.structured_many(
        [f.read_text(encoding="utf-8") for f in files],
        Invoice,
        system="Extract the invoice fields from the user's text. Use null for anything it does not state.",
        concurrency=8,
        return_exceptions=True,  # one bad file shouldn't stop the batch
        tag="invoice-import",
    )

with open("invoices.csv", "w", newline="", encoding="utf-8") as out:
    writer = csv.DictWriter(out, fieldnames=["file", *Invoice.model_fields])
    writer.writeheader()
    for f, res in zip(files, results):
        if isinstance(res, Exception):
            print(f"skipped {f.name}: {type(res).__name__}: {res}")
            continue
        writer.writerow({"file": f.name, **res.parsed.model_dump()})

print(t.summary())
```

## Read data from an image

Send an image in the message list to a vision-capable deployment (`gpt-4o`, `gpt-4.1`, `gpt-5`, ...).

```python
import base64
from pathlib import Path

from pydantic import BaseModel

import azure_openai_utils as az


class Item(BaseModel):
    name: str
    price: float


class Receipt(BaseModel):
    store: str | None
    date: str | None
    items: list[Item]
    total: float


image = base64.b64encode(Path("receipt.jpg").read_bytes()).decode()

messages = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "Read this receipt."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
        ],
    }
]

receipt = az.structured(
    messages,
    Receipt,
    system="Extract the receipt. Use null for anything that isn't legible.",
).parsed

for item in receipt.items:
    print(f"{item.name:<30} {item.price:>8.2f}")
print(f"{'TOTAL':<30} {receipt.total:>8.2f}")
```

!!! note

    With `enable_logging()`, the base64 image is written into the log along with the rest of the message.
    Use `enable_logging(..., content=False)` if you don't want that.

## Compare two deployments

Run the same inputs through two models, measure how often they agree, and compare cost and speed.

```python
from typing import Literal

import azure_openai_utils as az

Label = Literal["positive", "negative", "neutral"]
reviews = ["...", "...", "..."]  # your evaluation set

with az.track("compare") as t:
    small = az.structured_many(reviews, Label, deployment="gpt-4o-mini", tag="gpt-4o-mini")
    large = az.structured_many(reviews, Label, deployment="gpt-4.1", tag="gpt-4.1")

agree = sum(a.parsed == b.parsed for a, b in zip(small, large))
print(f"agreement: {agree}/{len(reviews)}")

for tag, stats in t.by_tag().items():
    print(f"{tag:<12} ${stats.cost:.4f}  avg {stats.latency_s / stats.calls:.2f}s per call")
```

## Stop at a spending limit

Check the running cost of a `track()` block and stop before you go over budget.

```python
import azure_openai_utils as az

BUDGET_USD = 5.00

with az.track("backfill") as t:
    for doc in documents:
        if t.cost >= BUDGET_USD:
            print(f"Stopping: ${t.cost:.2f} spent after {t.calls} calls")
            break
        save(az.extract(doc, Invoice).parsed)
```

## Save every call to SQLite

A [hook](guides/logging.md#hooks) receives every call. This one writes each call to a table you can query
with SQL.

```python
import json
import sqlite3
import threading

import azure_openai_utils as az

db = sqlite3.connect("llm_calls.db", check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS calls (
        id TEXT PRIMARY KEY, ts TEXT, fn TEXT, deployment TEXT, model TEXT, tag TEXT, status TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, latency_s REAL, record TEXT
    )"""
)
lock = threading.Lock()  # hooks can run on several threads at once


@az.add_hook
def save_call(rec: az.CallRecord) -> None:
    row = (
        rec.id,
        rec.ts,
        rec.fn,
        rec.deployment,
        rec.model,
        rec.tag,
        rec.status,
        rec.usage.input_tokens,
        rec.usage.output_tokens,
        rec.cost_usd,
        rec.latency_s,
        json.dumps(rec.to_dict(), default=str),
    )
    with lock, db:  # `with db` commits
        db.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
```

```sql
SELECT tag, COUNT(*) AS calls, SUM(cost_usd) AS cost, AVG(latency_s) AS avg_latency
FROM calls GROUP BY tag ORDER BY cost DESC;
```

## Test your code without calling Azure

Every function calls `get_client()` to get its client. In tests, replace it with a fake that returns canned
answers. Your application code doesn't need to change, and nothing touches the network.

```python
# test_triage.py
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion

import azure_openai_utils as az
from myapp import triage_ticket  # your code, which calls az.structured(...)


def fake_completion(answer: dict) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "test",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o-mini-2024-07-18",
            "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(answer)}}
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        }
    )


class FakeClient:
    def __init__(self, *answers: dict):
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)  # inspect what was sent
        return fake_completion(self.answers.pop(0))


@pytest.fixture
def fake_llm(monkeypatch):
    def install(*answers: dict) -> FakeClient:
        client = FakeClient(*answers)
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "test")
        monkeypatch.setattr(az, "get_client", lambda **_: client)
        return client

    return install


def test_outage_is_high_urgency(fake_llm):
    fake_llm({"reasoning": "...", "category": "outage", "urgency": "high", "summary": "All users locked out"})

    result = triage_ticket("EVERYTHING is down!")

    assert result.urgency == "high"
```

!!! tip

    For non-model schemas such as `Literal[...]`, `list[...]` or `int`, the answer travels as `{"value": ...}`,
    so the fake should return `{"value": "positive"}`.
