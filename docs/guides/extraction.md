# Extracting data from text

`extract()` is `structured()` with a system prompt written for pulling facts out of a document: *use only what
the text states, and use `null` for anything it doesn't mention*. Use it for emails, invoices, contracts, CVs,
support tickets and scraped pages.

```python
import azure_openai_utils as az

res = az.extract(text, Schema)
```

## A first example

```python
from pydantic import BaseModel

import azure_openai_utils as az


class Contact(BaseModel):
    name: str
    company: str | None
    email: str | None
    phone: str | None


email = """
Hi team, great meeting you at the expo! Let's follow up next week.
Priya Raman | Head of Ops, Northwind Logistics
priya.raman@northwind.example
"""

contact = az.extract(email, Contact).parsed
# Contact(name='Priya Raman', company='Northwind Logistics',
#         email='priya.raman@northwind.example', phone=None)
```

`phone` is `None` because the email doesn't contain a phone number. The system prompt tells the model to
return `null` rather than guess, and the `| None` type gives it a valid way to do that.

## Adding instructions

Use `instructions=` for rules specific to your task, such as formats, units, conventions or edge cases.
They're added after the built-in extraction prompt:

```python
res = az.extract(
    text,
    Invoice,
    instructions=(
        "Dates as YYYY-MM-DD. Amounts as plain numbers in the invoice currency, without symbols. "
        "If there are several totals, use the final amount due."
    ),
)
```

## Designing an extraction schema

A good extraction schema mirrors the document:

```python
from typing import Literal

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    description: str
    quantity: float
    unit_price: float


class Invoice(BaseModel):
    invoice_number: str | None
    vendor: str
    currency: str = Field(description="ISO 4217 code, e.g. USD, EUR, INR")
    issue_date: str | None = Field(description="YYYY-MM-DD")
    due_date: str | None = Field(description="YYYY-MM-DD")
    line_items: list[LineItem]
    total: float
    status: Literal["paid", "unpaid", "unknown"]


invoice = az.extract(invoice_text, Invoice).parsed
for item in invoice.line_items:
    print(f"{item.quantity} x {item.description} @ {item.unit_price}")
```

Some guidelines:

- **Anything that might be absent should be `| None`.** Otherwise the model is forced to invent a value.
- **Repeating things are lists.** Use `list[LineItem]`, not `item_1`, `item_2`, ...
- **Categories are `Literal`s.** Include an escape hatch such as `"unknown"` or `"other"`.
- **Put the format in the description.** `Field(description="YYYY-MM-DD")` is clearer than a separate instruction.
- **Add validators for what matters.** If `total` must equal the sum of the line items, write a
  `model_validator`. When it fails, the model is shown the error and asked again (see
  [validation retries](structured-output.md#validation-retries)).

## Many documents at once

There's no `extract_many`. Use [`structured_many`](batch-and-async.md) with your own extraction system prompt:

```python
from pathlib import Path

import azure_openai_utils as az

texts = [p.read_text() for p in sorted(Path("invoices").glob("*.txt"))]

results = az.structured_many(
    texts,
    Invoice,
    system="Extract the invoice details from the user's text. Use null for anything it does not state.",
    concurrency=8,
    tag="invoice-extraction",
)
invoices = [r.parsed for r in results]
```

For the async version, see [`aextract`](batch-and-async.md#async-functions).

## `extract` or `structured`?

| Use | When |
|---|---|
| `extract(text, Schema)` | The answer should come *only* from the text you pass in. |
| `structured(prompt, Schema)` | You want the model to use its own knowledge, reason, classify, generate or summarize. |

Both accept the same options (`deployment`, `mode`, `validation_retries`, `tag`, `metadata`, `client` and
model parameters such as `temperature`).
