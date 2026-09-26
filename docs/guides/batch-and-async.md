# Batches and async

When you have many inputs, send them concurrently. The module offers two ways to do it, and both return
results **in the same order as the inputs**.

| Function | Runs on | Use it from |
|---|---|---|
| `structured_many(prompts, schema)` | a thread pool | normal scripts, notebooks, Django/Flask, anywhere |
| `await astructured_many(prompts, schema)` | asyncio | async code: FastAPI, aiohttp, async workers |

## `structured_many`: the easy default

```python
from typing import Literal

import azure_openai_utils as az

reviews = [
    "Arrived broken and support never replied.",
    "Exactly as described, fast shipping!",
    "It's fine. Does the job.",
    # ... hundreds more
]

results = az.structured_many(
    reviews,
    Literal["positive", "negative", "neutral"],
    concurrency=8,
    system="Classify the sentiment of the product review.",
    tag="review-sentiment",
)

for review, res in zip(reviews, results):
    print(f"{res.parsed:8}  {review}")
```

- **`concurrency`** is the number of requests in flight at once (default 8). Raise it until you reach your
  deployment's rate limit. The SDK waits and retries on HTTP 429 by itself.
- Every other keyword (`system`, `deployment`, `tag`, `metadata`, `mode`, `validation_retries`, `temperature`,
  ...) works the same way as in [`structured()`](structured-output.md) and applies to every prompt.
- It needs no event loop, so it also works in Jupyter.
- Each prompt can be a string or a [list of messages](structured-output.md#prompts-a-string-or-a-conversation).

### Keep going when some prompts fail

By default, the first failure raises and the remaining work is cancelled. Pass `return_exceptions=True` to get
the exception in that prompt's slot instead:

```python
results = az.structured_many(texts, Invoice, return_exceptions=True)

ok = [r.parsed for r in results if isinstance(r, az.StructuredResult)]
failed = [(text, r) for text, r in zip(texts, results) if isinstance(r, Exception)]

print(f"{len(ok)} parsed, {len(failed)} failed")
for text, err in failed:
    print(type(err).__name__, "-", str(err)[:100])
```

Failed calls are still counted by the tracker and written to the log.

## Async functions

Every function has an async twin that takes the same arguments:

| Sync | Async |
|---|---|
| `structured(...)` | `await astructured(...)` |
| `extract(...)` | `await aextract(...)` |
| `structured_many(...)` | `await astructured_many(...)` |

```python
import asyncio

import azure_openai_utils as az


async def main() -> None:
    one = await az.astructured("Name a prime number above 100.", int)
    print(one.parsed)

    many = await az.astructured_many(
        ["Summarize: ...", "Summarize: ...", "Summarize: ..."],
        Summary,
        concurrency=16,
    )
    print([r.parsed for r in many])


asyncio.run(main())
```

`astructured_many` keeps at most `concurrency` requests in flight. If one fails and `return_exceptions` is
`False`, the others are cancelled and the error is raised. The cancelled calls are recorded with
`status="cancelled"` first, so a `track()` block around the batch still counts their tokens.

### In a web app

```python
from fastapi import FastAPI
from pydantic import BaseModel

import azure_openai_utils as az

app = FastAPI()


class Triage(BaseModel):
    category: str
    urgent: bool
    summary: str


@app.post("/triage")
async def triage(ticket: str) -> Triage:
    res = await az.astructured(ticket, Triage, system="Triage this support ticket.", tag="triage")
    return res.parsed
```

Async clients are cached per event loop, so calling `asyncio.run()` several times in one script works too.

## Tracking a batch

Wrap the batch in [`track()`](tracking.md#track-a-block-of-code) to get the usage for just that batch. This
works across the worker threads and asyncio tasks:

```python
with az.track("nightly-classification") as t:
    az.structured_many(reviews, Literal["positive", "negative", "neutral"], concurrency=16)

print(t.summary())
print(f"{t.calls} calls, {t.errors} errors, ${t.cost:.4f}")
```

!!! warning "Using your own threads"

    `structured_many` and asyncio tasks carry `track()` scopes with them. Threads you start yourself don't,
    unless you submit the work through `contextvars.copy_context().run`:

    ```python
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    with az.track("mine") as t, ThreadPoolExecutor() as pool:
        futures = [pool.submit(contextvars.copy_context().run, az.structured, p, Label) for p in prompts]
    ```

    The global `az.tracker` counts every call either way.
