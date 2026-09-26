"""azure_openai_utils: drop-in helpers for Azure OpenAI.

Pydantic structured output, token/cost tracking and call logging in a single file.
Copy this file into any project; nothing else from this repo is needed.

    pip install "openai>=1.106" "pydantic>=2.8"
    pip install azure-identity            # optional: keyless Entra ID auth

Environment
    AZURE_OPENAI_ENDPOINT     https://<resource>.openai.azure.com/
    AZURE_OPENAI_API_KEY      API key; leave unset to authenticate with Entra ID (azure-identity)
    AZURE_OPENAI_DEPLOYMENT   default deployment name
    OPENAI_API_VERSION        optional; set it to use a dated api-version instead of the v1 API

Quick start
    from pydantic import BaseModel
    import azure_openai_utils as az

    class Invoice(BaseModel):
        vendor: str
        total: float
        due_date: str | None

    az.enable_logging("logs/llm_calls.jsonl")      # every question + answer + usage, one JSON line each
    res = az.extract(invoice_text, Invoice, tag="invoices")
    res.parsed.total, res.usage, res.cost

    with az.track("batch") as t:                   # usage for just this block
        az.structured_many(texts, list[Invoice], concurrency=8)
    print(t.summary())
    print(az.tracker.summary())                    # whole process
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import json
import logging
import os
import re
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Generator, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Generic,
    Literal,
    TypeVar,
    cast,
    get_args,
    get_origin,
)

import openai
from openai import AsyncAzureOpenAI, AsyncOpenAI, AzureOpenAI, OpenAI
from pydantic import BaseModel, ValidationError, create_model

__version__ = "0.1.0"

__all__ = [
    "PRICING",
    "CallRecord",
    "ContentFilterError",
    "Price",
    "RefusalError",
    "Stats",
    "StructuredOutputError",
    "StructuredResult",
    "TruncatedError",
    "Usage",
    "UsageTracker",
    "ValidationFailedError",
    "add_hook",
    "aextract",
    "astructured",
    "astructured_many",
    "disable_logging",
    "enable_logging",
    "extract",
    "get_async_client",
    "get_client",
    "read_log",
    "remove_hook",
    "set_price",
    "structured",
    "structured_many",
    "track",
    "tracker",
]

log = logging.getLogger("azure_openai_utils")

T = TypeVar("T")
Messages = Sequence[dict[str, Any]]


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens."""

    input: float
    output: float
    cached_input: float | None = None  # None: cached tokens are billed at the input rate
    long_context: Price | None = None  # rates for the whole call once its input exceeds long_context_above
    long_context_above: int = 272_000


# OpenAI list prices, which Azure Global Standard deployments track (checked 2026-09).
# Data Zone / Regional deployments cost more; override those per deployment with set_price().
# gpt-5.4-pro (like other -pro models) is Responses-API only, so this module can't call it and it isn't listed.
PRICING: dict[str, Price] = {
    # gpt-5.4 bills 2x input / 1.5x output for a call with more than 272K input tokens.
    "gpt-5.4": Price(2.50, 15.00, 0.25, long_context=Price(5.00, 22.50, 0.50)),
    "gpt-5.4-mini": Price(0.75, 4.50, 0.075),
    "gpt-5.4-nano": Price(0.20, 1.25, 0.02),
    "gpt-5.2": Price(1.75, 14.00, 0.175),
    "gpt-5.1": Price(1.25, 10.00, 0.125),
    "gpt-5": Price(1.25, 10.00, 0.125),
    "gpt-5-mini": Price(0.25, 2.00, 0.025),
    "gpt-5-nano": Price(0.05, 0.40, 0.005),
    "gpt-4.1": Price(2.00, 8.00, 0.50),
    "gpt-4.1-mini": Price(0.40, 1.60, 0.10),
    "gpt-4.1-nano": Price(0.10, 0.40, 0.025),
    "gpt-4o": Price(2.50, 10.00, 1.25),
    "gpt-4o-mini": Price(0.15, 0.60, 0.075),
    "o3": Price(2.00, 8.00, 0.50),
    "o3-mini": Price(1.10, 4.40, 0.55),
    "o4-mini": Price(1.10, 4.40, 0.275),
}


def set_price(name: str, input: float, output: float, cached_input: float | None = None) -> None:
    """Add or override a price in USD per 1M tokens.

    `name` is a model ("gpt-4o") or one of your deployment names. A deployment entry takes
    precedence over the model, so it can carry Data Zone / Regional pricing.
    """
    PRICING[name] = Price(input, output, cached_input)


_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_unpriced_warned: set[str] = set()


def _price_for(model: str | None, deployment: str) -> Price | None:
    if deployment in PRICING:
        return PRICING[deployment]
    if model:
        # Azure reports the dated model ("gpt-4o-mini-2024-07-18"); strip only a trailing date so
        # "gpt-4o-mini-…" never falls back to "gpt-4o" and "gpt-5.1-…" never to "gpt-5".
        price = PRICING.get(model) or PRICING.get(_DATE_SUFFIX.sub("", model))
        if price:
            return price
    name = model or deployment
    if name not in _unpriced_warned:
        _unpriced_warned.add(name)
        log.warning(
            "azure_openai_utils: no price for %r, cost will be None; add one with set_price()",
            name,
        )
    return None


def _cost(usage: Usage, model: str | None, deployment: str) -> float | None:
    if usage.total_tokens == 0:
        return 0.0
    price = _price_for(model, deployment)
    if price is None:
        return None
    if price.long_context and usage.input_tokens > price.long_context_above:
        price = price.long_context
    cached = min(usage.cached_tokens, usage.input_tokens)
    cached_rate = price.input if price.cached_input is None else price.cached_input
    # Reasoning tokens are already part of output_tokens, so they aren't added again.
    dollars = (usage.input_tokens - cached) * price.input + cached * cached_rate + usage.output_tokens * price.output
    return round(dollars / 1_000_000, 8)


_V1_SCOPE = "https://ai.azure.com/.default"
_CLASSIC_SCOPE = "https://cognitiveservices.azure.com/.default"


def get_client(
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    api_version: str | None = None,
    max_retries: int = 3,
    timeout: float = 60.0,
) -> OpenAI:
    """Sync client, cached per argument set.

    Uses Azure's v1 API (`OpenAI` client on `<endpoint>/openai/v1/`, no api-version) unless an
    `api_version` is passed or OPENAI_API_VERSION is set, in which case the classic `AzureOpenAI`
    client is used. Auth: `api_key` or AZURE_OPENAI_API_KEY, otherwise Entra ID via azure-identity.
    Network retries and timeouts are handled by the SDK (`max_retries`, `timeout`).
    """
    return _cached_client(*_resolve(endpoint, api_key, api_version), max_retries, timeout)


# An async client is bound to the event loop it first ran on, so cache one per loop; a script
# that calls asyncio.run() twice gets a fresh client the second time instead of a dead one.
_async_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple, AsyncOpenAI]] = (
    weakref.WeakKeyDictionary()
)


def get_async_client(
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    api_version: str | None = None,
    max_retries: int = 3,
    timeout: float = 60.0,
) -> AsyncOpenAI:
    """Async counterpart of get_client(), cached per running event loop."""
    key = (*_resolve(endpoint, api_key, api_version), max_retries, timeout)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _make_client(True, *key)
    per_loop = _async_clients.setdefault(loop, {})
    if key not in per_loop:
        per_loop[key] = _make_client(True, *key)
    return per_loop[key]


def _resolve(endpoint: str | None, api_key: str | None, api_version: str | None) -> tuple[str, str | None, str | None]:
    endpoint = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not endpoint:
        raise ValueError("No Azure endpoint: pass endpoint=... or set AZURE_OPENAI_ENDPOINT")
    return (
        endpoint,
        api_key or os.environ.get("AZURE_OPENAI_API_KEY"),
        api_version or os.environ.get("OPENAI_API_VERSION"),
    )


@functools.cache
def _cached_client(endpoint, api_key, api_version, max_retries, timeout) -> OpenAI:
    return _make_client(False, endpoint, api_key, api_version, max_retries, timeout)


def _make_client(
    is_async: bool,
    endpoint: str,
    api_key: str | None,
    api_version: str | None,
    max_retries: int,
    timeout: float,
) -> Any:
    options: dict[str, Any] = {"max_retries": max_retries, "timeout": timeout}
    if api_version:
        azure_cls = AsyncAzureOpenAI if is_async else AzureOpenAI
        auth: dict[str, Any] = (
            {"api_key": api_key} if api_key else {"azure_ad_token_provider": _token_provider(_CLASSIC_SCOPE, is_async)}
        )
        return azure_cls(azure_endpoint=endpoint, api_version=api_version, **auth, **options)
    v1_cls = AsyncOpenAI if is_async else OpenAI
    return v1_cls(
        base_url=_v1_base_url(endpoint),
        api_key=api_key or _token_provider(_V1_SCOPE, is_async),
        **options,
    )


def _v1_base_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if not base.endswith("/openai/v1"):
        base += "/openai/v1"
    return base + "/"


def _token_provider(scope: str, is_async: bool) -> Callable:
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as exc:
        raise RuntimeError(
            "No API key: pass api_key=... or set AZURE_OPENAI_API_KEY, "
            "or `pip install azure-identity` for keyless Entra ID auth"
        ) from exc
    provider = get_bearer_token_provider(DefaultAzureCredential(), scope)
    if not is_async:
        return provider

    async def async_provider() -> str:
        return await asyncio.to_thread(provider)

    return async_provider


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0  # includes reasoning_tokens
    cached_tokens: int = 0  # subset of input_tokens served from the prompt cache
    reasoning_tokens: int = 0  # subset of output_tokens spent on hidden reasoning

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_tokens + other.cached_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    @classmethod
    def from_response(cls, resp: Any) -> Usage:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return cls()
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        completion_details = getattr(usage, "completion_tokens_details", None)
        return cls(
            input_tokens=usage.prompt_tokens or 0,
            output_tokens=usage.completion_tokens or 0,
            cached_tokens=getattr(prompt_details, "cached_tokens", None) or 0,
            reasoning_tokens=getattr(completion_details, "reasoning_tokens", None) or 0,
        )


@dataclass
class CallRecord:
    """One logical call, with validation retries folded in. Trackers, the JSONL log and hooks all get this."""

    id: str
    ts: str  # UTC start time, ISO-8601
    fn: str  # "structured", "extract", "astructured_many", ...
    deployment: str
    model: str | None  # model Azure reported, e.g. "gpt-4o-2024-11-20"; None if no response arrived
    schema: str
    mode: str
    tag: str | None
    messages: list[dict[str, Any]]  # the conversation as first sent: the user's question lives here
    output: Any  # parsed output as JSON data (ok calls only)
    raw_output: str | None  # the model's last raw text, kept when it could not be parsed
    status: str  # ok | refusal | content_filter | truncated | invalid | error | cancelled
    errors: list[str]
    attempts: int
    usage: Usage
    cost_usd: float | None
    latency_s: float
    metadata: dict[str, Any]

    def to_dict(self, *, content: bool = True) -> dict[str, Any]:
        """JSON-ready dict. content=False drops the prompt and output text."""
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["usage"] = asdict(self.usage)
        if not content:
            for key in ("messages", "output", "raw_output"):
                del data[key]
        return data


@dataclass
class StructuredResult(Generic[T]):
    parsed: T
    usage: Usage  # summed across validation retries
    cost: float | None  # None when the model has no entry in PRICING
    model: str | None
    deployment: str
    latency_s: float
    attempts: int
    record: CallRecord = field(repr=False)
    raw: Any = field(repr=False)  # last ChatCompletion


class StructuredOutputError(Exception):
    """A structured call failed. `.record` still holds its usage, cost and the model's raw output."""

    def __init__(self, message: str, record: CallRecord):
        super().__init__(message)
        self.record = record

    def __reduce__(
        self,
    ):  # keep picklable (e.g. across multiprocessing) despite the extra argument
        return type(self), (str(self), self.record)


class RefusalError(StructuredOutputError):
    """The model refused to answer (message.refusal was set)."""


class ContentFilterError(StructuredOutputError):
    """Azure's content filter blocked the prompt or the response."""


class TruncatedError(StructuredOutputError):
    """The output hit the token limit before the JSON was complete (finish_reason="length")."""


class ValidationFailedError(StructuredOutputError):
    """The output still failed Pydantic validation after every retry."""


# ---------------------------------------------------------------------------
# 4. Tracking
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    calls: int = 0
    errors: int = 0
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0  # excludes unpriced calls
    unpriced_calls: int = 0
    latency_s: float = 0.0

    def add(self, rec: CallRecord) -> None:
        self.calls += 1
        self.errors += rec.status != "ok"
        self.usage += rec.usage
        if rec.cost_usd is None:
            self.unpriced_calls += 1
        else:
            self.cost += rec.cost_usd
        self.latency_s += rec.latency_s


class UsageTracker:
    """Thread-safe running totals of calls, tokens and cost, overall and per model / tag."""

    def __init__(self, name: str = "tracker", *, keep_records: bool = False):
        self.name = name
        self.keep_records = keep_records
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._total = Stats()
            self._by_model: dict[str, Stats] = {}
            self._by_tag: dict[str | None, Stats] = {}
            self.records: list[CallRecord] = []  # filled only when keep_records=True
            self.last: CallRecord | None = None

    def record(self, rec: CallRecord) -> None:
        with self._lock:
            self._total.add(rec)
            self._by_model.setdefault(rec.model or rec.deployment, Stats()).add(rec)
            self._by_tag.setdefault(rec.tag, Stats()).add(rec)
            self.last = rec
            if self.keep_records:
                self.records.append(rec)

    @property
    def calls(self) -> int:
        return self._total.calls

    @property
    def errors(self) -> int:
        return self._total.errors

    @property
    def usage(self) -> Usage:
        return copy.copy(self._total.usage)

    @property
    def cost(self) -> float:
        return self._total.cost

    def totals(self) -> Stats:
        with self._lock:
            return copy.deepcopy(self._total)

    def by_model(self) -> dict[str, Stats]:
        with self._lock:
            return copy.deepcopy(self._by_model)

    def by_tag(self) -> dict[str | None, Stats]:
        with self._lock:
            return copy.deepcopy(self._by_tag)

    def summary(self, by: Literal["model", "tag"] = "model") -> str:
        """Plain-text table of usage and cost grouped by model or tag."""
        total = self.totals()
        groups = self.by_model() if by == "model" else self.by_tag()
        rows = [("(none)" if key is None else str(key), stats) for key, stats in groups.items()]
        rows.sort(key=lambda row: row[0])
        rows.append(("TOTAL", total))
        table = [(by, "calls", "input", "cached", "output", "reasoning", "cost($)")] + [
            (
                label,
                f"{s.calls:,}",
                f"{s.usage.input_tokens:,}",
                f"{s.usage.cached_tokens:,}",
                f"{s.usage.output_tokens:,}",
                f"{s.usage.reasoning_tokens:,}",
                f"{s.cost:.6f}" + ("*" if s.unpriced_calls else ""),
            )
            for label, s in rows
        ]
        widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
        lines = [f"tracker: {self.name} — {total.calls:,} calls, {total.errors:,} errors"]
        for row in table:
            cells = (c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(row, widths, strict=True)))
            lines.append("  ".join(cells))
        if total.unpriced_calls:
            lines.append(f"* {total.unpriced_calls} call(s) have no price and are left out of cost; see set_price()")
        return "\n".join(lines)

    def __repr__(self) -> str:
        tokens = self._total.usage.total_tokens
        return f"UsageTracker({self.name!r}, calls={self.calls}, tokens={tokens}, cost=${self.cost:.6f})"


tracker = UsageTracker("global")
"""Always on: sees every call made through this module in this process."""

_scopes: contextvars.ContextVar[tuple[UsageTracker, ...]] = contextvars.ContextVar(
    "azure_openai_utils_scopes", default=()
)


@contextmanager
def track(name: str = "scope") -> Generator[UsageTracker, None, None]:
    """Collect only the calls made inside this block, including asyncio tasks and the
    structured_many() threads it starts. Nestable; the global `tracker` still sees everything.
    The yielded tracker keeps its records in `.records`.

    Threads you start yourself don't inherit the scope unless you run them through
    `contextvars.copy_context().run`.
    """
    scope = UsageTracker(name, keep_records=True)
    token = _scopes.set((*_scopes.get(), scope))
    try:
        yield scope
    finally:
        _scopes.reset(token)


# ---------------------------------------------------------------------------
# 5. Logging
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()
_log_path: Path | None = None
_log_content = True
_hooks: list[Callable[[CallRecord], Any]] = []


def enable_logging(path: str | os.PathLike = "llm_calls.jsonl", *, content: bool = True) -> Path:
    """Append every call (question, output, usage, cost, errors) to a JSONL file.

    content=False keeps usage, cost and status but leaves out prompts and outputs.
    """
    global _log_path, _log_content
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_path, _log_content = log_path, content
    return log_path


def disable_logging() -> None:
    global _log_path
    _log_path = None


def add_hook(fn: Callable[[CallRecord], Any]) -> Callable[[CallRecord], Any]:
    """Call `fn(record)` after every call, e.g. to write to a DB. Returns `fn`, so it also works
    as a decorator. A hook that raises is logged and skipped; it never breaks the call."""
    _hooks.append(fn)
    return fn


def remove_hook(fn: Callable[[CallRecord], Any]) -> None:
    _hooks.remove(fn)


def read_log(path: str | os.PathLike = "llm_calls.jsonl") -> list[dict[str, Any]]:
    """Load the calls written by enable_logging()."""
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _emit(rec: CallRecord) -> None:
    tracker.record(rec)
    for scope in _scopes.get():
        scope.record(rec)

    path = _log_path
    if path is not None:
        try:
            line = json.dumps(rec.to_dict(content=_log_content), ensure_ascii=False, default=str)
            with _log_lock, open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            log.exception("azure_openai_utils: could not write call log to %s", path)

    for hook in list(_hooks):
        try:
            hook(rec)
        except Exception:
            log.exception("azure_openai_utils: hook %r failed", hook)

    log.debug(
        "%s %s status=%s tokens=%d cost=%s latency=%.2fs",
        rec.fn,
        rec.model or rec.deployment,
        rec.status,
        rec.usage.total_tokens,
        rec.cost_usd,
        rec.latency_s,
    )


# ---------------------------------------------------------------------------
# 6. Structured output
# ---------------------------------------------------------------------------


def structured(
    prompt: str | Messages,
    schema: type[T] | Any,
    *,
    system: str | None = None,
    deployment: str | None = None,
    mode: Literal["strict", "json"] = "strict",
    validation_retries: int = 1,
    tag: str | None = None,
    metadata: dict[str, Any] | None = None,
    client: OpenAI | None = None,
    **kwargs: Any,
) -> StructuredResult[T]:
    """Ask the model and get back a validated `schema` instance in `.parsed`.

    prompt              a question, or a full messages list
    schema              a Pydantic model, or any type Pydantic can validate: list[Model],
                        Literal["a", "b"], an Enum, int, ... (wrapped in {"value": ...} on the wire)
    mode                "strict": Azure structured outputs, where the model is constrained to the schema.
                        "json": JSON mode with the schema in the prompt, for deployments without
                        structured-output support.
    validation_retries  how many times to show the model its validation errors and ask again
                        (covers custom @field_validator rules the JSON schema can't express)
    tag, metadata       copied onto the call record, so usage and logs can be grouped and filtered
    **kwargs            passed to chat.completions.create: temperature, max_completion_tokens,
                        reasoning_effort, seed, ...

    Strict mode makes every field required and doesn't support defaults other than None, so write
    optional fields as `x: str | None`.

    Raises RefusalError, ContentFilterError, TruncatedError or ValidationFailedError (all carry
    `.record`); network and API errors from the SDK propagate unchanged. Every call, even a failed
    or cancelled one, is tracked and logged.
    """
    call = _Call(
        "structured",
        prompt,
        schema,
        system=system,
        deployment=deployment,
        mode=mode,
        validation_retries=validation_retries,
        tag=tag,
        metadata=metadata,
        **kwargs,
    )
    return _run(call, client or get_client())


async def astructured(
    prompt: str | Messages,
    schema: type[T] | Any,
    *,
    system: str | None = None,
    deployment: str | None = None,
    mode: Literal["strict", "json"] = "strict",
    validation_retries: int = 1,
    tag: str | None = None,
    metadata: dict[str, Any] | None = None,
    client: AsyncOpenAI | None = None,
    **kwargs: Any,
) -> StructuredResult[T]:
    """Async structured()."""
    call = _Call(
        "astructured",
        prompt,
        schema,
        system=system,
        deployment=deployment,
        mode=mode,
        validation_retries=validation_retries,
        tag=tag,
        metadata=metadata,
        **kwargs,
    )
    return await _arun(call, client or get_async_client())


def structured_many(
    prompts: Iterable[str | Messages],
    schema: type[T] | Any,
    *,
    concurrency: int = 8,
    return_exceptions: bool = False,
    client: OpenAI | None = None,
    **options: Any,
) -> list[StructuredResult[T]]:
    """structured() over many prompts on a thread pool; results come back in input order.

    `options` are structured()'s keyword arguments. With return_exceptions=True a failed prompt
    yields its exception in place of a result instead of aborting the batch. Works in scripts and
    notebooks alike (no event loop needed).
    """
    client = client or get_client()

    def one(prompt: str | Messages) -> StructuredResult[T]:
        return _run(_Call("structured_many", prompt, schema, **options), client)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        # copy_context() so calls inside a track() block are still counted from worker threads
        futures = [pool.submit(contextvars.copy_context().run, one, p) for p in prompts]
        # Typed as results only; with return_exceptions=True failed slots hold the exception.
        return cast("list[StructuredResult[T]]", _collect(futures, return_exceptions))


async def astructured_many(
    prompts: Iterable[str | Messages],
    schema: type[T] | Any,
    *,
    concurrency: int = 8,
    return_exceptions: bool = False,
    client: AsyncOpenAI | None = None,
    **options: Any,
) -> list[StructuredResult[T]]:
    """Async structured_many(): at most `concurrency` requests in flight, results in input order."""
    client = client or get_async_client()
    limit = asyncio.Semaphore(max(1, concurrency))

    async def one(prompt: str | Messages) -> StructuredResult[T]:
        async with limit:
            return await _arun(_Call("astructured_many", prompt, schema, **options), client)

    tasks = [asyncio.ensure_future(one(p)) for p in prompts]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=return_exceptions)
        return cast("list[StructuredResult[T]]", list(results))
    except BaseException:
        for task in tasks:
            task.cancel()
        # Wait for the cancelled calls to record themselves, so they're tracked before the error surfaces.
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


_EXTRACT_SYSTEM = (
    "Extract the requested information from the user's text. Use only what the text states; "
    "use null for anything it does not mention."
)


def extract(
    text: str, schema: type[T] | Any, *, instructions: str | None = None, **options: Any
) -> StructuredResult[T]:
    """structured() with an extraction system prompt. Add task-specific guidance via `instructions`;
    `options` are structured()'s keyword arguments."""
    client = options.pop("client", None)
    call = _Call("extract", text, schema, system=_extract_system(instructions), **options)
    return _run(call, client or get_client())


async def aextract(
    text: str, schema: type[T] | Any, *, instructions: str | None = None, **options: Any
) -> StructuredResult[T]:
    """Async extract()."""
    client = options.pop("client", None)
    call = _Call("aextract", text, schema, system=_extract_system(instructions), **options)
    return await _arun(call, client or get_async_client())


def _extract_system(instructions: str | None) -> str:
    return f"{_EXTRACT_SYSTEM}\n\n{instructions}" if instructions else _EXTRACT_SYSTEM


_FIX_PROMPT = (
    "Your previous reply did not match the required schema.\n"
    "Validation errors: {errors}\n"
    "Reply again with only the corrected JSON."
)
_RESERVED_KWARGS = ("model", "messages", "response_format", "stream")


@dataclass(frozen=True)
class _Spec:
    model_cls: type[BaseModel]
    wrapped: bool  # schema isn't a BaseModel, so it travels as {"value": ...}
    name: str
    response_format: dict[str, Any]
    instructions: str | None  # extra system prompt for JSON mode


def _get_spec(schema: Any, mode: str) -> _Spec:
    try:
        hash(schema)
    except TypeError:
        return _build_spec(schema, mode)
    return _cached_spec(schema, mode)


@functools.lru_cache(maxsize=256)
def _cached_spec(schema: Any, mode: str) -> _Spec:
    return _build_spec(schema, mode)


def _build_spec(schema: Any, mode: str) -> _Spec:
    if mode not in ("strict", "json"):
        raise ValueError(f"mode must be 'strict' or 'json', got {mode!r}")
    wrapped = not _is_model(schema)
    name = _schema_name(schema)
    model_cls = create_model(name, value=(schema, ...)) if wrapped else schema
    if mode == "strict":
        # The SDK's public helper produces the strict-mode schema (all fields required, no extra keys).
        params = dict(openai.pydantic_function_tool(model_cls)["function"])["parameters"]
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": params, "strict": True},
        }
        return _Spec(model_cls, wrapped, name, response_format, None)
    instructions = (
        "Reply with only a JSON object (no prose, no code fences) that validates against this JSON Schema:\n"
        + json.dumps(model_cls.model_json_schema())
    )
    return _Spec(model_cls, wrapped, name, {"type": "json_object"}, instructions)


def _is_model(tp: Any) -> bool:
    return get_origin(tp) is None and isinstance(tp, type) and issubclass(tp, BaseModel)


def _schema_name(tp: Any) -> str:
    def name(t: Any) -> str:
        if get_origin(t) is None and isinstance(t, type):
            return t.__name__
        origin = get_origin(t)
        if origin is Literal:
            return "Choice"
        inner = "".join(name(a) for a in get_args(t) if a is not type(None) and a is not Ellipsis)
        base = getattr(origin, "__name__", None) or "Value"
        return base.capitalize() + (f"Of{inner}" if inner else "")

    # Azure requires ^[a-zA-Z0-9_-]{1,64}$ for json_schema names.
    return re.sub(r"[^A-Za-z0-9_-]", "_", name(tp))[:64] or "Value"


def _build_messages(prompt: str | Messages, system: str | None, instructions: str | None) -> list[dict[str, Any]]:
    messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else [dict(m) for m in prompt]
    return [{"role": "system", "content": text} for text in (system, instructions) if text] + messages


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_url=False)
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Call:
    """State for one logical structured call across all its attempts.

    The sync and async runners differ only in how they await create(); everything else
    (request building, response checks, retries, recording) lives here.
    """

    def __init__(
        self,
        fn: str,
        prompt: str | Messages,
        schema: Any,
        *,
        system: str | None = None,
        deployment: str | None = None,
        mode: str = "strict",
        validation_retries: int = 1,
        tag: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        clash = [k for k in _RESERVED_KWARGS if k in kwargs]
        if clash:
            raise TypeError(f"{fn}() sets {', '.join(clash)} itself; use deployment= to pick the model")
        deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        if not deployment:
            raise ValueError("No deployment: pass deployment=... or set AZURE_OPENAI_DEPLOYMENT")

        self.fn = fn
        self.spec = _get_spec(schema, mode)
        self.mode = mode
        self.deployment = deployment
        self.messages = _build_messages(prompt, system, self.spec.instructions)
        self.sent_messages = list(self.messages)  # retries append to self.messages; the log keeps the original
        self.validation_retries = validation_retries
        self.tag = tag
        self.metadata = dict(metadata or {})
        self.kwargs = kwargs

        self.id = uuid.uuid4().hex
        self.ts = _utc_now()
        self.t0 = time.perf_counter()
        self.attempts = 0
        self.usage = Usage()
        self.errors: list[str] = []
        self.model: str | None = None
        self.content: str | None = None
        self.obj: BaseModel | None = None
        self.raw: Any = None
        self.record: CallRecord | None = None  # set once finish() has emitted the call

    def request(self) -> dict[str, Any]:
        self.attempts += 1
        return {
            **self.kwargs,
            "model": self.deployment,
            "messages": self.messages,
            "response_format": self.spec.response_format,
        }

    def handle(self, resp: Any) -> bool:
        """Take in one response: True when it parsed, False to retry, raises on a terminal failure."""
        self.raw, self.model = resp, resp.model
        self.usage += Usage.from_response(resp)  # counted even when this attempt fails
        if not resp.choices:
            raise self.stop("error", StructuredOutputError, "response contained no choices")
        choice = resp.choices[0]
        self.content = choice.message.content
        if getattr(choice.message, "refusal", None):
            raise self.stop("refusal", RefusalError, f"model refused: {choice.message.refusal}")
        if choice.finish_reason == "content_filter":
            raise self.stop(
                "content_filter",
                ContentFilterError,
                "Azure content filter blocked the response",
            )
        if choice.finish_reason == "length":
            raise self.stop(
                "truncated",
                TruncatedError,
                "output hit the token limit before the JSON was complete; raise max_completion_tokens",
            )
        try:
            self.obj = self.spec.model_cls.model_validate_json(self.content or "")
            return True
        except ValidationError as exc:
            problems = _format_validation_error(exc)
            self.errors.append(problems)
            if self.attempts > self.validation_retries:
                raise self.stop(
                    "invalid",
                    ValidationFailedError,
                    f"output failed validation after {self.attempts} attempt(s): {problems}",
                ) from None
            self.messages += [
                {"role": "assistant", "content": self.content or ""},
                {"role": "user", "content": _FIX_PROMPT.format(errors=problems)},
            ]
            return False

    def stop(self, status: str, exc_type: type[StructuredOutputError], message: str) -> StructuredOutputError:
        if status != "invalid":  # validation problems are already in self.errors
            self.errors.append(message)
        return exc_type(message, self.finish(status))

    def fail(self, exc: BaseException) -> BaseException:
        """Record a call that raised, and return the exception to surface."""
        if self.record is not None:  # already recorded: raised by stop(), or interrupted while emitting
            return exc
        if getattr(exc, "code", None) == "content_filter":  # Azure rejects a filtered prompt with a 400
            return self.stop(
                "content_filter",
                ContentFilterError,
                f"Azure content filter blocked the prompt: {exc}",
            )
        self.errors.append(f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
        # CancelledError, KeyboardInterrupt, ...: the call was interrupted rather than failed
        self.finish("error" if isinstance(exc, Exception) else "cancelled")
        return exc

    def finish(self, status: str) -> CallRecord:
        ok = status == "ok"
        output = None
        if ok:
            assert self.obj is not None
            output = self.obj.model_dump(mode="json")
            if self.spec.wrapped:
                output = output["value"]
        rec = CallRecord(
            id=self.id,
            ts=self.ts,
            fn=self.fn,
            deployment=self.deployment,
            model=self.model,
            schema=self.spec.name,
            mode=self.mode,
            tag=self.tag,
            messages=self.sent_messages,
            output=output,
            raw_output=None if ok else self.content,
            status=status,
            errors=self.errors,
            attempts=self.attempts,
            usage=self.usage,
            cost_usd=_cost(self.usage, self.model, self.deployment),
            latency_s=round(time.perf_counter() - self.t0, 3),
            metadata=self.metadata,
        )
        self.record = rec
        _emit(rec)
        return rec

    def result(self) -> StructuredResult:
        rec = self.finish("ok")
        return StructuredResult(
            parsed=cast(Any, self.obj).value if self.spec.wrapped else self.obj,
            usage=rec.usage,
            cost=rec.cost_usd,
            model=rec.model,
            deployment=rec.deployment,
            latency_s=rec.latency_s,
            attempts=rec.attempts,
            record=rec,
            raw=self.raw,
        )


def _run(call: _Call, client: OpenAI) -> StructuredResult:
    try:
        while not call.handle(client.chat.completions.create(**call.request())):
            pass
    except BaseException as exc:  # BaseException too, so cancelled and interrupted calls are recorded
        err = call.fail(exc)
        if err is exc:
            raise
        raise err from exc
    return call.result()


async def _arun(call: _Call, client: AsyncOpenAI) -> StructuredResult:
    try:
        while not call.handle(await client.chat.completions.create(**call.request())):
            pass
    except BaseException as exc:  # BaseException too, so cancelled and interrupted calls are recorded
        err = call.fail(exc)
        if err is exc:
            raise
        raise err from exc
    return call.result()


def _collect(futures: list[Future], return_exceptions: bool) -> list:
    results = []
    for future in futures:
        try:
            results.append(future.result())
        except Exception as exc:
            if not return_exceptions:
                for pending in futures:
                    pending.cancel()
                raise
            results.append(exc)
    return results
