# API reference

Everything the module exports, grouped by purpose. All of it is reached through the module, e.g.
`az.structured(...)` after `import azure_openai_utils as az`.

`T` below stands for whatever schema you pass: a Pydantic model, `list[...]`, `Literal[...]`, an `Enum`, `int`,
and so on.

## Structured output

### `structured`

```python
structured(
    prompt: str | list[dict],
    schema: type[T],
    *,
    system: str | None = None,
    deployment: str | None = None,
    mode: Literal["strict", "json"] = "strict",
    validation_retries: int = 1,
    tag: str | None = None,
    metadata: dict | None = None,
    client: OpenAI | None = None,
    **kwargs,
) -> StructuredResult[T]
```

Ask the model and get a validated instance of `schema` back in `.parsed`.

| Parameter | Description |
|---|---|
| `prompt` | A question (becomes one user message) or a full list of chat messages. |
| `schema` | A Pydantic model, or any type Pydantic can validate: `list[Model]`, `Literal[...]`, an `Enum`, `int`, `bool`, ... |
| `system` | A system prompt, placed before the messages. |
| `deployment` | The deployment to call. Defaults to `AZURE_OPENAI_DEPLOYMENT`. |
| `mode` | `"strict"` uses Azure structured outputs. `"json"` uses JSON mode with the schema in the prompt, for deployments without structured outputs. |
| `validation_retries` | How many times to show the model its validation errors and ask again. |
| `tag`, `metadata` | Copied onto the call record, for grouping and filtering usage and logs. |
| `client` | A client to use instead of `get_client()`. |
| `**kwargs` | Passed to `chat.completions.create`, e.g. `temperature`, `max_completion_tokens`, `reasoning_effort`, `seed`. `model`, `messages`, `response_format` and `stream` aren't allowed. |

**Raises** `RefusalError`, `ContentFilterError`, `TruncatedError` or `ValidationFailedError`. SDK network and
API errors propagate unchanged. See [Handling errors](guides/errors.md).

### `astructured`

```python
await astructured(prompt, schema, *, ..., client: AsyncOpenAI | None = None, **kwargs) -> StructuredResult[T]
```

Async version of `structured()`, with the same arguments.

### `extract`

```python
extract(text: str, schema: type[T], *, instructions: str | None = None, **options) -> StructuredResult[T]
```

`structured()` with a built-in extraction system prompt: use only what the text states, and `null` for
anything missing. `instructions` is appended to that prompt. `options` are `structured()`'s keyword arguments
(`deployment`, `tag`, `client`, `temperature`, ...).

### `aextract`

```python
await aextract(text: str, schema: type[T], *, instructions: str | None = None, **options) -> StructuredResult[T]
```

Async version of `extract()`.

### `structured_many`

```python
structured_many(
    prompts: Iterable[str | list[dict]],
    schema: type[T],
    *,
    concurrency: int = 8,
    return_exceptions: bool = False,
    client: OpenAI | None = None,
    **options,
) -> list[StructuredResult[T]]
```

Run `structured()` over many prompts on a thread pool. Results come back **in input order**. `options` are
`structured()`'s keyword arguments and apply to every prompt. With `return_exceptions=True`, a failed prompt's
slot holds its exception instead of stopping the batch. It needs no event loop, so it works in notebooks.

### `astructured_many`

```python
await astructured_many(
    prompts, schema, *, concurrency: int = 8, return_exceptions: bool = False,
    client: AsyncOpenAI | None = None, **options,
) -> list[StructuredResult[T]]
```

Async version of `structured_many()`. At most `concurrency` requests are in flight at once. If one fails and
`return_exceptions` is `False`, the others are cancelled, and they're recorded as `cancelled` before the error
is raised.

### `StructuredResult`

What every structured call returns.

| Attribute | Type | Description |
|---|---|---|
| `parsed` | `T` | The validated answer. |
| `usage` | `Usage` | Tokens, summed across validation retries. |
| `cost` | `float | None` | USD, or `None` if the model has no price. |
| `model` | `str | None` | The model Azure reported, e.g. `gpt-4o-2024-11-20`. |
| `deployment` | `str` | The deployment called. |
| `latency_s` | `float` | Wall-clock seconds, including retries. |
| `attempts` | `int` | Requests made (1 unless validation retries happened). |
| `record` | `CallRecord` | The record also sent to trackers, the log and hooks. |
| `raw` | `ChatCompletion` | The last raw SDK response. |

## Errors

### `StructuredOutputError`

Base class of the errors below. Every instance has `.record`, the `CallRecord` of the failed call, with usage,
cost, `raw_output` and `errors`. It's raised directly only in the rare case of a response with no choices.

| Subclass | `record.status` | Raised when |
|---|---|---|
| `RefusalError` | `refusal` | The model refused (`message.refusal` was set). |
| `ContentFilterError` | `content_filter` | Azure's content filter blocked the prompt (HTTP 400) or the response. |
| `TruncatedError` | `truncated` | The output hit the token limit before the JSON was complete. |
| `ValidationFailedError` | `invalid` | The output still failed validation after every retry. |

## Usage and cost tracking

### `Usage`

```python
Usage(input_tokens=0, output_tokens=0, cached_tokens=0, reasoning_tokens=0)
```

| Field | Description |
|---|---|
| `input_tokens` | Prompt tokens. |
| `output_tokens` | Completion tokens, including reasoning tokens. |
| `cached_tokens` | The part of `input_tokens` served from the prompt cache. |
| `reasoning_tokens` | The part of `output_tokens` spent on hidden reasoning. |
| `total_tokens` | Property: `input_tokens + output_tokens`. |

`Usage` objects can be added together: `u1 + u2`.

### `tracker`

The global `UsageTracker`. It's always on and sees every call in the process.

### `track`

```python
with track(name: str = "scope") as t:  # t is a UsageTracker with keep_records=True
    ...
```

Collects only the calls made inside the block, including `structured_many` worker threads and asyncio tasks.
Scopes can be nested, and the global `tracker` still counts everything. See
[Track a block of code](guides/tracking.md#track-a-block-of-code).

### `UsageTracker`

```python
UsageTracker(name: str = "tracker", *, keep_records: bool = False)
```

Thread-safe running totals.

| Member | Description |
|---|---|
| `calls`, `errors` | Number of calls, and how many didn't end with `status == "ok"`. |
| `usage` | Total `Usage`. |
| `cost` | Total USD, excluding unpriced calls. |
| `last` | The most recent `CallRecord`, or `None`. |
| `records` | Every `CallRecord`, if `keep_records=True`. |
| `totals()` | A `Stats` snapshot of everything. |
| `by_model()` | `dict[model, Stats]`. |
| `by_tag()` | `dict[tag, Stats]`. |
| `summary(by="model" | "tag")` | A plain-text table. |
| `record(rec)` | Add a `CallRecord`. Pass it to `add_hook` to feed a tracker of your own. |
| `reset()` | Clear everything. |

### `Stats`

A snapshot of totals: `calls`, `errors`, `usage`, `cost`, `unpriced_calls`, `latency_s` (summed).

### `PRICING`, `Price`, `set_price`

```python
PRICING: dict[str, Price]  # model or deployment name -> price
Price(
    input: float,
    output: float,
    cached_input: float | None = None,
    long_context: Price | None = None,
    long_context_above: int = 272_000,
)  # USD per 1M tokens
set_price(name: str, input: float, output: float, cached_input: float | None = None) -> None
```

`set_price` adds or overrides an entry. `name` can be a model (`"gpt-4o"`) or one of your deployment names, and a
deployment entry takes precedence over the model's. If `cached_input` is `None`, cached tokens are billed at the
input rate. If `long_context` is set, a call with more than `long_context_above` input tokens is billed entirely at
those rates (used for `gpt-5.4`). `set_price` doesn't take `long_context`; assign a `Price` to `PRICING[name]` for
that. See [Pricing](guides/tracking.md#pricing).

## Logging and hooks

### `enable_logging`

```python
enable_logging(path: str | PathLike = "llm_calls.jsonl", *, content: bool = True) -> Path
```

Append every call to a JSONL file, creating parent directories as needed. `content=False` leaves out
`messages`, `output` and `raw_output`. Returns the path.

### `disable_logging`

```python
disable_logging() -> None
```

Stop writing the log.

### `read_log`

```python
read_log(path: str | PathLike = "llm_calls.jsonl") -> list[dict]
```

Load a log file written by `enable_logging()`.

### `add_hook`, `remove_hook`

```python
add_hook(fn: Callable[[CallRecord], Any]) -> fn
remove_hook(fn) -> None
```

Call `fn(record)` after every call. `add_hook` works as a decorator. Exceptions raised in a hook are logged and
ignored.

### `CallRecord`

One logical call, with its validation retries folded in.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique hex id. |
| `ts` | `str` | UTC start time, ISO-8601. |
| `fn` | `str` | `structured`, `extract`, `structured_many`, `astructured`, ... |
| `deployment` | `str` | Deployment called. |
| `model` | `str | None` | Model Azure reported. `None` if no response arrived. |
| `schema` | `str` | Schema name, e.g. `Invoice`, `ListOfInvoice`, `Choice`. |
| `mode` | `str` | `strict` or `json`. |
| `tag` | `str | None` | As passed. |
| `messages` | `list[dict]` | The conversation as first sent. |
| `output` | `Any` | Parsed answer as JSON data (only when `status == "ok"`). |
| `raw_output` | `str | None` | The model's last raw text, when it couldn't be used. |
| `status` | `str` | `ok`, `refusal`, `content_filter`, `truncated`, `invalid`, `error`, `cancelled`. |
| `errors` | `list[str]` | Every problem encountered, in order. |
| `attempts` | `int` | Requests made. |
| `usage` | `Usage` | Summed across attempts. |
| `cost_usd` | `float | None` | USD, or `None` if unpriced. |
| `latency_s` | `float` | Seconds. |
| `metadata` | `dict` | As passed. |

`record.to_dict(content=True)` returns a JSON-ready dict. With `content=False` it leaves out `messages`,
`output` and `raw_output`.

## Clients

### `get_client`

```python
get_client(
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    api_version: str | None = None,
    max_retries: int = 3,
    timeout: float = 60.0,
) -> OpenAI
```

A sync client, cached per argument set. Arguments default to `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`
and `OPENAI_API_VERSION`. Without an `api_version` it uses the v1 API (`OpenAI` on `<endpoint>/openai/v1/`);
with one, it uses `AzureOpenAI`. Without an API key it signs in with Entra ID through `azure-identity`. See
[Clients and authentication](guides/clients.md).

### `get_async_client`

```python
get_async_client(*, endpoint=None, api_key=None, api_version=None, max_retries=3, timeout=60.0) -> AsyncOpenAI
```

Async version of `get_client()`, cached per running event loop.
