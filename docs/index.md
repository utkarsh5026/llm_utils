---
hide:
  - navigation
---
# llm_utils

<p class="hero-tagline">
Get typed Python objects back from Azure OpenAI instead of free text. Token counts, cost and a log of
every call come built in. It's one file you copy into your project.
</p>

[Get started in 5 minutes](getting-started.md){ .md-button .md-button--primary }
[Browse recipes](recipes.md){ .md-button }

---

## What it looks like

```python
from pydantic import BaseModel

import azure_openai_utils as az


class Invoice(BaseModel):
    vendor: str
    total: float
    due_date: str | None


res = az.extract("Invoice #1042 from Acme Corp. Amount due: $1,249.50, payable by 2026-10-31.", Invoice)

res.parsed  # (1)!
res.usage  # (2)!
res.cost  # (3)!
```

1. `Invoice(vendor='Acme Corp', total=1249.5, due_date='2026-10-31')`: a real, validated Pydantic object.
2. `Usage(input_tokens=812, output_tokens=41, cached_tokens=0, reasoning_tokens=0)`
3. `0.00244`, in US dollars, worked out from the model Azure reports.

You describe the shape of the answer as a Pydantic model and get an instance of that model back. You never
parse JSON, write regexes over the model's text, or guess at token counts.

## What you get

<div class="grid cards" markdown>

-   :material-code-json:{ .lg .middle } **Typed structured output**

    ---

    Pass a Pydantic model, `list[...]`, `Literal[...]`, an `Enum` or a plain `int`, and get that type back.
    If the output fails your validators, the errors go back to the model and it gets another try.

    [:octicons-arrow-right-24: Structured output](guides/structured-output.md)

-   :material-text-search:{ .lg .middle } **Extraction in one line**

    ---

    `extract(text, Schema)` pulls fields out of emails, invoices, tickets or contracts. It uses `null`
    for anything the text doesn't say, instead of making something up.

    [:octicons-arrow-right-24: Extracting data](guides/extraction.md)

-   :material-lightning-bolt:{ .lg .middle } **Batches and async**

    ---

    `structured_many` runs hundreds of prompts on a thread pool and returns results in input order.
    `astructured_many` does the same with asyncio.

    [:octicons-arrow-right-24: Batches and async](guides/batch-and-async.md)

-   :material-cash-multiple:{ .lg .middle } **Token and cost tracking**

    ---

    Every call is counted: input, cached, output and reasoning tokens, plus dollars. Totals are available
    per process, per model, per tag, or for just one block of code.

    [:octicons-arrow-right-24: Tokens and cost](guides/tracking.md)

-   :material-file-document-multiple:{ .lg .middle } **A log of every call**

    ---

    One line switches on a JSONL log of every question, answer, status, token count and cost, failed calls
    included. Hooks can send the same records to your database.

    [:octicons-arrow-right-24: Logging and hooks](guides/logging.md)

-   :material-shield-check:{ .lg .middle } **Clear failures**

    ---

    Refusals, content filtering, truncated output and invalid output each raise their own exception,
    and every one of them still carries the call's usage and cost.

    [:octicons-arrow-right-24: Handling errors](guides/errors.md)

</div>

## Why a single file?

`azure_openai_utils.py` is a **drop-in module**, not a package. You copy it into your project, and from then
on it's your code: read it, change it, pin it. It depends only on `openai` and `pydantic`, plus
`azure-identity` if you want keyless sign-in.

- **Nothing to install from here.** There's no package to publish, version or upgrade.
- **Easy to read.** About a thousand lines, split into numbered sections, with no framework underneath.
- **Tested.** CI runs the offline suite on Python 3.10–3.14 and on the oldest supported `openai` and `pydantic`.

!!! tip "Where to go next"

    - New here? Follow [Getting started](getting-started.md). It takes you from nothing to your first typed
      answer.
    - Have a task in mind? The [Recipes](recipes.md) page has complete, copy-paste programs for common jobs.
    - Looking up a parameter? See the [API reference](reference.md).
