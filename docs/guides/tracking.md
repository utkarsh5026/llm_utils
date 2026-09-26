# Tokens and cost

Every call is counted. You can check the usage of a single call, of a block of code, or of the whole process,
and group it by model or by your own tags.

## Per call

Every result carries its own usage and cost:

```python
res = az.structured(prompt, Invoice)

res.usage.input_tokens  # 812   prompt tokens, including the schema
res.usage.cached_tokens  # 0    part of input_tokens served from Azure's prompt cache
res.usage.output_tokens  # 41   completion tokens, including reasoning tokens
res.usage.reasoning_tokens  # 0  part of output_tokens spent on hidden reasoning (o-series, gpt-5)
res.usage.total_tokens  # 853  input + output
res.cost  # 0.00244  US dollars, or None if the model has no price
```

If the call needed [validation retries](structured-output.md#validation-retries), these numbers are the
**sum of all attempts**.

## The whole process: `az.tracker`

`az.tracker` is always on. It sees every call made through the module in the current process, from any thread
or task, including failed calls.

```python
print(az.tracker.summary())
```

```text
tracker: global — 2 calls, 0 errors
model                   calls  input  cached  output  reasoning   cost($)
gpt-4o-2024-11-20           1    812       0      41          0  0.002440
gpt-4o-mini-2024-07-18      1    500     256      30          0  0.000074
TOTAL                       2  1,312     256      71          0  0.002514
```

For use in code:

```python
az.tracker.calls  # 2
az.tracker.errors  # 0
az.tracker.usage  # Usage(input_tokens=1312, output_tokens=71, cached_tokens=256, reasoning_tokens=0)
az.tracker.cost  # 0.0025138
az.tracker.last  # the CallRecord of the most recent call

az.tracker.totals()  # Stats(calls=2, errors=0, usage=..., cost=..., unpriced_calls=0, latency_s=...)
az.tracker.by_model()  # {"gpt-4o-2024-11-20": Stats(...), "gpt-4o-mini-2024-07-18": Stats(...)}
az.tracker.by_tag()  # {"invoices": Stats(...), "triage": Stats(...)}

az.tracker.reset()  # start counting from zero
```

### Group by tag

Pass `tag=` on your calls to label them by feature, customer, pipeline step, or experiment. Then group the
report by tag:

```python
az.extract(invoice_text, Invoice, tag="invoices")
az.structured(ticket, Triage, tag="triage")

print(az.tracker.summary(by="tag"))
```

```text
tracker: global — 2 calls, 0 errors
tag       calls  input  cached  output  reasoning   cost($)
invoices      1    812       0      41          0  0.002440
triage        1    500     256      30          0  0.000074
TOTAL         2  1,312     256      71          0  0.002514
```

Calls without a tag are listed as `(none)`.

## Track a block of code

`az.track()` counts only the calls made inside a `with` block:

```python
with az.track("import-job") as t:
    for doc in documents:
        az.extract(doc, Invoice)

print(t.summary())
print(t.calls, t.usage.total_tokens, t.cost)

for rec in t.records:  # every CallRecord from the block
    print(rec.status, rec.latency_s, rec.cost_usd)
```

- It covers [`structured_many`](batch-and-async.md) worker threads and asyncio tasks started inside the block.
- Scopes can be nested. A call is counted by every enclosing scope *and* by the global tracker:

```python
with az.track("pipeline") as whole:
    with az.track("step-1") as step1:
        az.structured(a, A)
    with az.track("step-2") as step2:
        az.structured(b, B)

# whole.calls == 2, step1.calls == 1, step2.calls == 1
```

## Report on a job with several steps

A job often calls the model from several functions, each with its own prompt and often its own deployment. To see
what one run of the job cost, and which step and which model the money went to:

- pass a `tag=` for each step, and
- run the job inside `az.track()`.

The functions don't need a tracker passed to them. Every call made inside the `with` block is counted, however deep
in the call stack it happens.

```python
from typing import Literal

from pydantic import BaseModel, field_validator

import azure_openai_utils as az


class Invoice(BaseModel):
    vendor: str
    total: float

    @field_validator("total")
    @classmethod
    def not_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("total must be >= 0")
        return v


class Summary(BaseModel):
    summary: str


def classify(doc: str) -> str:
    kind = Literal["invoice", "contract", "other"]
    return az.structured(doc, kind, system="Classify the document.", deployment="gpt-5.4-mini", tag="classify").parsed


def extract(doc: str) -> Invoice:
    return az.extract(doc, Invoice, deployment="gpt-5.4-mini", tag="extract").parsed


def summarize(docs: list[str]) -> str:
    return az.structured("\n\n".join(docs), Summary, deployment="gpt-5.4", tag="summarize").parsed.summary


def process(docs: list[str]) -> tuple[list[Invoice], str]:
    invoices = [extract(doc) for doc in docs if classify(doc) == "invoice"]
    return invoices, summarize(docs)


with az.track("process-batch") as t:
    invoices, summary = process(docs)

print(t.summary())  # grouped by model
print(t.summary(by="tag"))  # grouped by step
```

With two invoices and one contract in `docs`:

```text
tracker: process-batch — 6 calls, 0 errors
model                    calls   input  cached  output  reasoning   cost($)
gpt-5.4-2026-03-05           1   6,200       0     820        512  0.027800
gpt-5.4-mini-2026-03-17      5  13,310   3,072     272          0  0.009133
TOTAL                        6  19,510   3,072   1,092        512  0.036933
tracker: process-batch — 6 calls, 0 errors
tag        calls   input  cached  output  reasoning   cost($)
classify       3   3,450   3,072      36          0  0.000676
extract        2   9,860       0     236          0  0.008457
summarize      1   6,200       0     820        512  0.027800
TOTAL          6  19,510   3,072   1,092        512  0.036933
```

The two tables are different views of the same six calls:

- **By model:** `gpt-5.4-mini` made 5 of the 6 calls, but the single `gpt-5.4` call cost three times as much as
  all of them together.
- **By step:** `extract` cost 12 times as much as `classify` on the same model. Its prompts are longer, and each
  extraction needed one [validation retry](structured-output.md#validation-retries), whose tokens are included.
  `classify` was cheap because 1,024 of each prompt's 1,150 input tokens came from the prompt cache.
- A retry counts toward that call's `attempts`, not as an extra call, so `calls` is the number of calls your code
  made.

!!! tip "One `track()` block per job"

    `az.tracker` counts everything since the process started. In a process that runs many jobs, such as a web
    server or a queue worker, wrap each job in its own `track()` block so every report starts from zero. If the
    job's steps run on threads you start yourself, see [Using your own threads](batch-and-async.md#tracking-a-batch).

### Save the report as JSON

`summary()` is for reading. To keep the numbers in a file, object storage or a database, build a dict from the
same tracker:

```python
import json
import uuid
from dataclasses import asdict


def stats_dict(s: az.Stats) -> dict:
    return {
        "calls": s.calls,
        "errors": s.errors,
        "cost_usd": round(s.cost, 8),
        "unpriced_calls": s.unpriced_calls,
        **asdict(s.usage),
        "latency_s": round(s.latency_s, 3),
    }


def job_report(t: az.UsageTracker) -> dict:
    return {
        "job": t.name,
        "total": stats_dict(t.totals()),
        "by_model": {model: stats_dict(s) for model, s in t.by_model().items()},
        "by_tag": {tag or "(none)": stats_dict(s) for tag, s in t.by_tag().items()},
        "calls": [rec.to_dict(content=False) for rec in t.records],  # prompts and outputs left out
    }


job_id = uuid.uuid4().hex
with az.track("process-batch") as t:
    try:
        invoices, summary = process(docs)
    finally:  # a job that fails part-way still gets a report
        with open(f"report-{job_id}.json", "w", encoding="utf-8") as f:
            json.dump(job_report(t), f, indent=2)
```

The report for the run above, shortened so each group fits on one line:

```json5
{
  "job": "process-batch",
  "total": {"calls": 6, "errors": 0, "cost_usd": 0.0369329, "unpriced_calls": 0, "input_tokens": 19510, "output_tokens": 1092, "cached_tokens": 3072, "reasoning_tokens": 512, "latency_s": 5.254},
  "by_model": {
    "gpt-5.4-mini-2026-03-17": {"calls": 5, "errors": 0, "cost_usd": 0.0091329, "unpriced_calls": 0, "input_tokens": 13310, "output_tokens": 272, "cached_tokens": 3072, "reasoning_tokens": 0, "latency_s": 3.153},
    "gpt-5.4-2026-03-05": {"calls": 1, "errors": 0, "cost_usd": 0.0278, "unpriced_calls": 0, "input_tokens": 6200, "output_tokens": 820, "cached_tokens": 0, "reasoning_tokens": 512, "latency_s": 2.101}
  },
  "by_tag": {
    "classify": {"calls": 3, "errors": 0, "cost_usd": 0.0006759, "unpriced_calls": 0, "input_tokens": 3450, "output_tokens": 36, "cached_tokens": 3072, "reasoning_tokens": 0, "latency_s": 1.351},
    "extract": {"calls": 2, "errors": 0, "cost_usd": 0.008457, "unpriced_calls": 0, "input_tokens": 9860, "output_tokens": 236, "cached_tokens": 0, "reasoning_tokens": 0, "latency_s": 1.802},
    "summarize": {"calls": 1, "errors": 0, "cost_usd": 0.0278, "unpriced_calls": 0, "input_tokens": 6200, "output_tokens": 820, "cached_tokens": 0, "reasoning_tokens": 512, "latency_s": 2.101}
  },
  "calls": [
    // one entry per call (6 here); only the first `extract` call is shown
    {
      "id": "c4e736cc00ce4982a0f17d89f4c400e5",
      "ts": "2026-09-26T14:25:25.167Z",
      "fn": "extract",
      "deployment": "gpt-5.4-mini",
      "model": "gpt-5.4-mini-2026-03-17",
      "schema": "Invoice",
      "mode": "strict",
      "tag": "extract",
      "status": "ok",
      "errors": ["total: Value error, total must be >= 0"],
      "attempts": 2,
      "usage": {"input_tokens": 4930, "output_tokens": 118, "cached_tokens": 0, "reasoning_tokens": 0},
      "cost_usd": 0.0042285,
      "latency_s": 0.901,
      "metadata": {}
    }
  ]
}
```

- `by_model` and `by_tag` hold the same numbers as the two tables. `calls` holds every
  [`CallRecord`](../reference.md#callrecord) from the block, so it shows which model each step used and which calls
  were retried (`attempts`, `errors`).
- Failed and [cancelled](errors.md#cancelled-calls) calls are in the report too, with their `status` and the
  tokens they used, and they count toward `errors`. The `finally` block writes the report even when the job is
  interrupted.
- `cost_usd` leaves out calls that have no price, and `unpriced_calls` says how many there were. See
  [Set your own prices](#set-your-own-prices).
- Drop `content=False` to keep each call's prompt and output in the report.

## Pricing

Cost is calculated from the model that Azure *reports* for each response, such as `gpt-4o-2024-11-20`.
The module strips the date and looks the model up in `az.PRICING`. The built-in table uses OpenAI's list prices
(USD per 1M tokens), which Azure **Global Standard** deployments follow:

| Model            | Input | Cached input | Output |
| ---------------- | ----: | -----------: | -----: |
| `gpt-5.4`      |  2.50 |         0.25 |  15.00 |
| `gpt-5.4-mini` |  0.75 |        0.075 |   4.50 |
| `gpt-5.4-nano` |  0.20 |         0.02 |   1.25 |
| `gpt-5.2`      |  1.75 |        0.175 |  14.00 |
| `gpt-5.1`      |  1.25 |        0.125 |  10.00 |
| `gpt-5`        |  1.25 |        0.125 |  10.00 |
| `gpt-5-mini`   |  0.25 |        0.025 |   2.00 |
| `gpt-5-nano`   |  0.05 |        0.005 |   0.40 |
| `gpt-4.1`      |  2.00 |         0.50 |   8.00 |
| `gpt-4.1-mini` |  0.40 |         0.10 |   1.60 |
| `gpt-4.1-nano` |  0.10 |        0.025 |   0.40 |
| `gpt-4o`       |  2.50 |         1.25 |  10.00 |
| `gpt-4o-mini`  |  0.15 |        0.075 |   0.60 |
| `o3`           |  2.00 |         0.50 |   8.00 |
| `o3-mini`      |  1.10 |         0.55 |   4.40 |
| `o4-mini`      |  1.10 |        0.275 |   4.40 |

Cached input tokens are billed at the cached rate. Reasoning tokens are already included in output tokens, so
they aren't charged twice.

`gpt-5.4` has a 1.05M-token context window, and a call with **more than 272K input tokens** is billed at the
long-context rate for the whole call: 5.00 input, 0.50 cached input and 22.50 output. The module applies this
automatically. `gpt-5.4-mini` and `gpt-5.4-nano` accept at most 272K input tokens, so they have a single rate.
`gpt-5.4-pro` isn't in the table because it only works with the Responses API, and this module calls Chat Completions.

### Set your own prices

Data Zone and Regional deployments cost more than Global Standard, and new models may be missing from the
table. Use `set_price` for either case:

```python
# Price a specific deployment. This takes precedence over the model's price.
az.set_price("my-eu-gpt-4o", input=2.75, output=11.00, cached_input=1.375)

# Add a model the table doesn't know
az.set_price("gpt-6-preview", input=3.00, output=15.00)
```

`set_price` sets a single rate. To keep a long-context rate on a deployment, assign a `Price` yourself:

```python
az.PRICING["my-eu-gpt-5.4"] = az.Price(2.75, 16.50, 0.275, long_context=az.Price(5.50, 24.75, 0.55))
```

The lookup order is:

1. an entry for the **deployment name** (so you can price each deployment separately),
2. the exact model name Azure reported,
3. that model name without its trailing `-YYYY-MM-DD`.

!!! warning "Unknown models"

    If none of those match, the call's `cost` is `None` and a warning is logged once for that model. The
    tracker still counts its tokens, and its summary marks the cost column with `*`:

    ``text     * 3 call(s) have no price and are left out of cost; see set_price()     ``

## Your own tracker

`UsageTracker` is a public class. For a running total that you can read and reset independently of
`az.tracker`, create one and feed it from a [hook](logging.md#hooks):

```python
billing = az.UsageTracker("billing", keep_records=True)
az.add_hook(billing.record)

# ... later
print(billing.summary(by="tag"))
```
