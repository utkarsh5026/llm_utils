# Handling errors

When a call can't give you a valid answer, it raises an exception that tells you why. Each of these
exceptions still carries the call's record, so you can see the tokens you paid for and what the model actually
said.

## The exceptions

```text
StructuredOutputError          base class; every one has .record
├── RefusalError               the model refused to answer
├── ContentFilterError         Azure's content filter blocked the prompt or the response
├── TruncatedError             the answer hit the token limit before the JSON was complete
└── ValidationFailedError      the answer still failed validation after every retry
```

| Exception | `record.status` | What to do |
|---|---|---|
| `RefusalError` | `refusal` | Rephrase the request, or treat it as "can't answer". |
| `ContentFilterError` | `content_filter` | Check the input. Azure blocked the prompt (HTTP 400) or the output. |
| `TruncatedError` | `truncated` | Raise `max_completion_tokens`, or ask for less output. Reasoning models need extra room. |
| `ValidationFailedError` | `invalid` | Raise `validation_retries`, loosen the validator, or make the schema or instructions clearer. |

All of them are **recorded before they're raised**: they count in `az.tracker` and `track()` scopes, appear in
the log, and are passed to hooks.

## Catching them

```python
import azure_openai_utils as az

try:
    res = az.extract(text, Invoice)
except az.TruncatedError:
    res = az.extract(text, Invoice, max_completion_tokens=4000)  # try again with more room
except az.StructuredOutputError as err:
    rec = err.record
    print(f"failed: {rec.status} after {rec.attempts} attempt(s)")
    print("errors:", rec.errors)
    print("model said:", rec.raw_output)
    print("tokens spent:", rec.usage.total_tokens, "cost:", rec.cost_usd)
    raise
```

## What gets retried automatically

| Problem | Retried? | By |
|---|---|---|
| The answer fails Pydantic validation (or isn't valid JSON) | Yes, `validation_retries` times (default 1), with the errors shown to the model | this module |
| Rate limit (429), timeouts, connection errors, 5xx | Yes, up to `max_retries` times (default 3), with backoff | the OpenAI SDK |
| Refusal, content filter, truncation | No, raised immediately | n/a |

## Errors from the OpenAI SDK

Network and API errors (`openai.RateLimitError`, `openai.APITimeoutError`, `openai.APIConnectionError`,
`openai.AuthenticationError`, `openai.NotFoundError`, ...) are raised **unchanged**, after the SDK has used up
its own retries. They're still recorded with `status="error"`:

```python
import openai

try:
    res = az.structured(prompt, Summary)
except openai.RateLimitError:
    ...  # back off at a higher level
except openai.NotFoundError:
    ...  # usually a wrong deployment name
```

The one exception to "unchanged" is Azure's content filter. Azure rejects a filtered *prompt* with an HTTP 400
whose code is `content_filter`, and that's converted to `ContentFilterError`, so both kinds of filtering can
be caught the same way. The original SDK error is kept as `__cause__`.

## Errors in batches

In `structured_many` and `astructured_many`, the first error is raised and the rest of the batch is cancelled.
To collect failures instead, use `return_exceptions=True`:

```python
results = az.structured_many(texts, Invoice, return_exceptions=True)

for text, res in zip(texts, results):
    if isinstance(res, az.StructuredOutputError):
        print("model problem:", res.record.status, text[:60])
    elif isinstance(res, Exception):
        print("API problem:", type(res).__name__, text[:60])
    else:
        save(res.parsed)
```

## Setup mistakes

These are raised *before* any request is sent. They aren't recorded, because no call happened:

| Error | Cause |
|---|---|
| `ValueError: No deployment` | Neither `deployment=` nor `AZURE_OPENAI_DEPLOYMENT` is set. |
| `ValueError: No Azure endpoint` | Neither `endpoint=` nor `AZURE_OPENAI_ENDPOINT` is set. |
| `RuntimeError: No API key ...` | No API key, and `azure-identity` isn't installed for Entra ID. |
| `TypeError: ... sets model itself` | You passed `model=`, `messages=`, `response_format=` or `stream=`. Use `deployment=` instead of `model=`. |
| `ValueError: mode must be 'strict' or 'json'` | Typo in `mode=`. |

See [Troubleshooting](../troubleshooting.md) for more.
