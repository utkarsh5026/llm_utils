# llm_utils

Drop-in, single-file LLM helpers. Each provider gets its own self-contained module that you copy into a
project; there are no imports between modules.

**Documentation:** <https://utkarsh5026.github.io/llm_utils/>. It covers getting started, guides, recipes and the
API reference.

| Module | Provider | Needs |
|---|---|---|
| [`azure_openai_utils.py`](azure_openai_utils.py) | Azure OpenAI | `openai>=1.106`, `pydantic>=2.8`, optional `azure-identity` |

## azure_openai_utils

Pydantic structured output, token/cost tracking and call logging for Azure OpenAI.

### Setup

```bash
pip install "openai>=1.106" "pydantic>=2.8"   # + azure-identity for keyless Entra ID auth
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
export AZURE_OPENAI_API_KEY=...              # omit to use Entra ID
export AZURE_OPENAI_DEPLOYMENT=my-gpt-4o     # default deployment
```

It uses Azure's v1 API by default, with no `api-version` to manage. If you set `OPENAI_API_VERSION` or pass
`api_version=`, it switches to the classic `AzureOpenAI` client instead.

### Structured output

```python
from typing import Literal
from pydantic import BaseModel
import azure_openai_utils as az


class Invoice(BaseModel):
    vendor: str
    total: float
    due_date: str | None  # strict mode: optional fields are `X | None`, not defaults


res = az.structured("What's on this invoice? ...", Invoice)
res.parsed  # Invoice(vendor='Acme', total=1249.5, due_date=None)
res.usage, res.cost  # Usage(input_tokens=812, output_tokens=41, ...), 0.00244

az.extract(text, Invoice, instructions="Dates as YYYY-MM-DD.")  # extraction prompt built in
az.structured(text, list[Invoice])  # lists, Literals, Enums, ints...
az.structured(review, Literal["positive", "negative", "neutral"]).parsed
az.structured(text, Invoice, mode="json")  # deployments without structured outputs

results = az.structured_many(texts, Invoice, concurrency=8)  # threads, order kept
results = await az.astructured_many(texts, Invoice, concurrency=8)  # asyncio
```

* **Validation retries:** if the output fails Pydantic validation, including custom `@field_validator`s,
  the errors are sent back to the model and it gets another attempt (`validation_retries=1` by default).
* **Failures** raise `RefusalError`, `ContentFilterError`, `TruncatedError` or `ValidationFailedError`.
  Each carries `.record`, so usage and cost are still visible. SDK network/API errors propagate unchanged.
* **Extra arguments** such as `temperature`, `max_completion_tokens`, `reasoning_effort` and `seed` are passed to
  `chat.completions.create`.

### Token tracking

```python
print(az.tracker.summary())  # everything in this process, by model
print(az.tracker.summary(by="tag"))  # grouped by the tag= you passed

with az.track("nightly-eval") as t:  # only calls inside this block (threads + asyncio tasks included)
    az.structured_many(texts, Invoice, tag="eval")
t.calls, t.usage, t.cost, t.records
```

```text
tracker: global — 50 calls, 1 errors
model              calls   input  cached  output  reasoning   cost($)
gpt-4o-2024-11-20     50  40,600   8,192   2,050          0  0.110210
TOTAL                 50  40,600   8,192   2,050          0  0.110210
```

Cost is looked up from the model Azure reports (e.g. `gpt-4o-2024-11-20` → `gpt-4o`) in `PRICING`, and
cached input tokens are billed at the cached rate. Use `az.set_price("my-deployment", input=..., output=...)`
for Data Zone/Regional pricing or models that aren't listed. Unknown models get `cost=None` and a one-time warning.

### Logging questions and responses

```python
az.enable_logging("logs/llm_calls.jsonl")  # content=False drops prompts/outputs
az.structured(question, Invoice, tag="invoices", metadata={"user_id": "u_42"})
rows = az.read_log("logs/llm_calls.jsonl")


@az.add_hook  # or send each record anywhere yourself
def to_db(record: az.CallRecord): ...
```

Each call, failed calls included, is written as one JSON line: id, timestamp, deployment, model, tag, schema,
the messages sent (your question), parsed output (or raw output on failure), status, errors, attempts, usage,
cost, latency and metadata.

## Development

```bash
make install        # uv sync + git pre-commit hook
make check          # everything CI runs: lint (ruff, pyright, hygiene), tests, minimum versions, drop-in import
make help           # all targets (fmt, test, cov, live, ...)
make docs           # preview the docs site at http://127.0.0.1:8000 (sources in docs/, config in mkdocs.yml)
```

CI (`.github/workflows/ci.yml`) runs lint, the test suite on Python 3.10–3.14, and the minimum-version and drop-in
checks on every push to `main` and every PR. To run the live Azure test there, add `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_DEPLOYMENT` as repository secrets, then trigger the workflow manually
with **live** ticked.
