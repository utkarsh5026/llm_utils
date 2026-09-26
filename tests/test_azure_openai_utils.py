"""Offline tests: a fake client replays canned ChatCompletions, so nothing touches the network."""

# FakeClient is duck-typed in place of OpenAI, and tracker.last is known to be set after a call.
# pyright: reportArgumentType=false, reportOptionalMemberAccess=false

import asyncio
import copy
import json
import logging
import threading
from enum import Enum
from types import SimpleNamespace
from typing import Literal

import openai
import pytest
from openai.types.chat import ChatCompletion
from pydantic import BaseModel, field_validator

import azure_openai_utils as az

try:  # openai>=3 ships on httpx2
    import httpx2 as httpx
except ImportError:
    import httpx  # pyright: ignore[reportMissingImports]

DEPLOY = "test-deploy"
GPT4O = "gpt-4o-2024-11-20"


class Invoice(BaseModel):
    vendor: str
    total: float
    due_date: str | None

    @field_validator("total")
    @classmethod
    def non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("total must be >= 0")
        return v


INVOICE = {"vendor": "Acme", "total": 1249.5, "due_date": None}


def completion(
    content=None,
    *,
    model=GPT4O,
    prompt_tokens=100,
    completion_tokens=20,
    cached=0,
    reasoning=0,
    finish_reason="stop",
    refusal=None,
):
    if content is not None and not isinstance(content, str):
        content = json.dumps(content)
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content, "refusal": refusal},
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_tokens_details": {"cached_tokens": cached},
                "completion_tokens_details": {"reasoning_tokens": reasoning},
            },
        }
    )


def cost(model_price, prompt_tokens=100, completion_tokens=20):
    return (prompt_tokens * model_price.input + completion_tokens * model_price.output) / 1e6


class FakeClient:
    """Stands in for OpenAI/AzureOpenAI: chat.completions.create() replays responses in order,
    or asks `responder(request_kwargs)` for one. Exceptions in the queue are raised."""

    def __init__(self, *responses, responder=None):
        self.requests = []
        self._responses = list(responses)
        self._responder = responder
        self._lock = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _next(self, kwargs):
        with self._lock:
            self.requests.append(copy.deepcopy(kwargs))
            resp = self._responder(kwargs) if self._responder else self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def _create(self, **kwargs):
        return self._next(kwargs)


class AsyncFakeClient(FakeClient):
    def __init__(self, *responses, responder=None):
        super().__init__(*responses, responder=responder)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._acreate))

    async def _acreate(self, **kwargs):
        await asyncio.sleep(0)
        return self._next(kwargs)


def numbered_invoice(kwargs):
    n = int(kwargs["messages"][-1]["content"].split()[-1])
    return completion({"vendor": f"v{n}", "total": n, "due_date": None})


def api_error(cls, code=None):
    request = httpx.Request("POST", "https://res.openai.azure.com/openai/v1/chat/completions")
    if cls is openai.APIConnectionError:
        return cls(request=request)
    return cls("request failed", response=httpx.Response(400, request=request), body={"code": code, "message": "x"})


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", DEPLOY)
    monkeypatch.setattr(az, "PRICING", dict(az.PRICING))
    az.tracker.reset()
    az.disable_logging()
    az._hooks.clear()
    az._unpriced_warned.clear()
    az._cached_client.cache_clear()
    yield
    az.tracker.reset()
    az.disable_logging()
    az._hooks.clear()


# --- structured output -------------------------------------------------------


def test_structured_returns_parsed_model_with_usage_and_cost():
    client = FakeClient(completion(INVOICE))
    res = az.structured("What's on this invoice?", Invoice, client=client)

    assert res.parsed == Invoice(**INVOICE)
    assert res.usage == az.Usage(input_tokens=100, output_tokens=20)
    assert res.cost == pytest.approx(cost(az.PRICING["gpt-4o"]))
    assert (res.model, res.deployment, res.attempts) == (GPT4O, DEPLOY, 1)

    request = client.requests[0]
    assert request["model"] == DEPLOY
    assert request["messages"] == [{"role": "user", "content": "What's on this invoice?"}]
    fmt = request["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Invoice"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert set(fmt["json_schema"]["schema"]["required"]) == {"vendor", "total", "due_date"}


def test_system_prompt_and_messages_list():
    client = FakeClient(completion(INVOICE))
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "parse it"},
    ]
    az.structured(history, Invoice, system="Be precise.", client=client)
    assert client.requests[0]["messages"] == [{"role": "system", "content": "Be precise."}, *history]


def test_list_schema_is_wrapped_and_unwrapped():
    client = FakeClient(completion({"value": [INVOICE, INVOICE]}))
    res = az.structured("two invoices", list[Invoice], client=client)

    assert res.parsed == [Invoice(**INVOICE)] * 2
    assert res.record.output == [INVOICE, INVOICE]
    assert client.requests[0]["response_format"]["json_schema"]["name"] == "ListOfInvoice"


def test_literal_and_enum_schemas():
    class Sentiment(str, Enum):
        positive = "positive"
        negative = "negative"

    client = FakeClient(completion({"value": "negative"}), completion({"value": "positive"}))
    label = az.structured("meh", Literal["positive", "negative"], client=client)
    assert label.parsed == "negative"
    enum_schema = client.requests[0]["response_format"]["json_schema"]["schema"]
    assert enum_schema["properties"]["value"]["enum"] == ["positive", "negative"]

    assert az.structured("great", Sentiment, client=client).parsed is Sentiment.positive


def test_validation_retry_feeds_errors_back_and_sums_usage():
    bad = {**INVOICE, "total": -5}
    client = FakeClient(completion(bad), completion(INVOICE, prompt_tokens=150))
    res = az.structured("invoice", Invoice, client=client)

    assert res.parsed.total == 1249.5
    assert res.attempts == 2
    assert res.usage.input_tokens == 250 and res.usage.output_tokens == 40
    assert len(res.record.errors) == 1 and "total must be >= 0" in res.record.errors[0]

    retry_messages = client.requests[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": json.dumps(bad)}
    assert retry_messages[-1]["role"] == "user" and "total must be >= 0" in retry_messages[-1]["content"]
    assert res.record.messages == [{"role": "user", "content": "invoice"}]  # log keeps the original question


def test_validation_failure_after_retries():
    client = FakeClient(completion("not json"), completion({"vendor": "Acme"}))
    with pytest.raises(az.ValidationFailedError) as info:
        az.structured("invoice", Invoice, client=client)

    rec = info.value.record
    assert rec.status == "invalid" and rec.attempts == 2 and len(rec.errors) == 2
    assert rec.raw_output == json.dumps({"vendor": "Acme"})
    assert rec.usage.input_tokens == 200
    assert az.tracker.errors == 1 and az.tracker.usage.input_tokens == 200


def test_zero_validation_retries_fails_fast():
    client = FakeClient(completion("{}"))
    with pytest.raises(az.ValidationFailedError):
        az.structured("invoice", Invoice, validation_retries=0, client=client)
    assert len(client.requests) == 1


@pytest.mark.parametrize(
    ("response", "exc_type", "status"),
    [
        (completion(None, refusal="I can't help with that."), az.RefusalError, "refusal"),
        (completion('{"vendor": "Ac', finish_reason="length"), az.TruncatedError, "truncated"),
        (completion(None, finish_reason="content_filter"), az.ContentFilterError, "content_filter"),
    ],
)
def test_terminal_responses_raise_and_are_recorded(response, exc_type, status):
    with pytest.raises(exc_type) as info:
        az.structured("q", Invoice, client=FakeClient(response))
    assert info.value.record.status == status
    assert info.value.record.usage.input_tokens == 100
    assert az.tracker.last.status == status and az.tracker.errors == 1


def test_azure_prompt_content_filter_400_is_mapped():
    error = api_error(openai.BadRequestError, code="content_filter")
    with pytest.raises(az.ContentFilterError) as info:
        az.structured("q", Invoice, client=FakeClient(error))
    assert info.value.__cause__ is error
    assert info.value.record.status == "content_filter"


def test_api_errors_propagate_unchanged_but_are_recorded():
    error = api_error(openai.APIConnectionError)
    with pytest.raises(openai.APIConnectionError) as info:
        az.structured("q", Invoice, client=FakeClient(error))
    assert info.value is error
    rec = az.tracker.last
    assert rec.status == "error" and rec.model is None and rec.cost_usd == 0.0
    assert rec.errors[0].startswith("APIConnectionError")


def test_json_mode_puts_schema_in_prompt():
    client = FakeClient(completion(INVOICE))
    res = az.structured("invoice", Invoice, mode="json", client=client)

    assert res.parsed.vendor == "Acme"
    request = client.requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert request["messages"][0]["role"] == "system"
    assert "JSON Schema" in request["messages"][0]["content"] and '"vendor"' in request["messages"][0]["content"]


def test_extract_adds_extraction_prompt_and_instructions():
    client = FakeClient(completion(INVOICE))
    res = az.extract("Invoice from Acme ...", Invoice, instructions="Dates as ISO-8601.", client=client, tag="inv")

    system = client.requests[0]["messages"][0]
    assert system["role"] == "system" and "Extract" in system["content"] and "Dates as ISO-8601." in system["content"]
    assert res.record.fn == "extract" and res.record.tag == "inv"


def test_kwargs_pass_through_and_reserved_names_are_rejected():
    client = FakeClient(completion(INVOICE))
    az.structured("q", Invoice, temperature=0, max_completion_tokens=300, deployment="other", client=client)
    assert client.requests[0]["temperature"] == 0
    assert client.requests[0]["max_completion_tokens"] == 300
    assert client.requests[0]["model"] == "other"

    with pytest.raises(TypeError, match="response_format"):
        az.structured("q", Invoice, response_format={"type": "text"}, client=client)


def test_missing_deployment(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT")
    with pytest.raises(ValueError, match="AZURE_OPENAI_DEPLOYMENT"):
        az.structured("q", Invoice, client=FakeClient())


# --- pricing -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "priced_as"),
    [
        (GPT4O, "gpt-4o"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        ("gpt-4.1-mini-2025-04-14", "gpt-4.1-mini"),
        ("gpt-5.1-2025-11-13", "gpt-5.1"),
        ("gpt-5.4-2026-03-05", "gpt-5.4"),
        ("gpt-5.4-mini-2026-03-17", "gpt-5.4-mini"),
        ("gpt-4o", "gpt-4o"),
    ],
)
def test_price_comes_from_reported_model(model, priced_as):
    res = az.structured("q", Invoice, client=FakeClient(completion(INVOICE, model=model)))
    assert res.cost == pytest.approx(cost(az.PRICING[priced_as]))


@pytest.mark.parametrize(("prompt_tokens", "long"), [(272_000, False), (272_001, True)])
def test_long_context_rate_applies_above_threshold(prompt_tokens, long):
    client = FakeClient(completion(INVOICE, model="gpt-5.4-2026-03-05", prompt_tokens=prompt_tokens))
    res = az.structured("q", Invoice, client=client)
    price = az.PRICING["gpt-5.4"]
    assert price.long_context is not None
    assert res.cost == pytest.approx(cost(price.long_context if long else price, prompt_tokens=prompt_tokens))


def test_cached_tokens_use_cached_rate():
    res = az.structured("q", Invoice, client=FakeClient(completion(INVOICE, cached=80)))
    price = az.PRICING["gpt-4o"]
    assert price.cached_input is not None
    assert res.cost == pytest.approx((20 * price.input + 80 * price.cached_input + 20 * price.output) / 1e6)


def test_deployment_price_overrides_model_price():
    az.set_price(DEPLOY, input=1.0, output=2.0)
    res = az.structured("q", Invoice, client=FakeClient(completion(INVOICE)))
    assert res.cost == pytest.approx((100 * 1.0 + 20 * 2.0) / 1e6)


def test_unknown_model_has_no_cost_and_warns_once(caplog):
    client = FakeClient(
        completion(INVOICE, model="gpt-99-mini-2030-01-01"), completion(INVOICE, model="gpt-99-mini-2030-01-01")
    )
    with caplog.at_level(logging.WARNING, logger="azure_openai_utils"):
        first = az.structured("q", Invoice, client=client)
        az.structured("q", Invoice, client=client)

    assert first.cost is None
    assert sum("gpt-99-mini" in m for m in caplog.messages) == 1
    assert az.tracker.totals().unpriced_calls == 2
    assert "have no price" in az.tracker.summary()


# --- tracking ------------------------------------------------------------------


def test_tracker_groups_by_model_and_tag():
    client = FakeClient(completion(INVOICE), completion(INVOICE, model="gpt-4o-mini-2024-07-18"), completion(INVOICE))
    az.structured("a", Invoice, tag="invoices", client=client)
    az.structured("b", Invoice, tag="receipts", client=client)
    az.structured("c", Invoice, client=client)

    by_model = az.tracker.by_model()
    assert by_model[GPT4O].calls == 2 and by_model["gpt-4o-mini-2024-07-18"].calls == 1
    by_tag = az.tracker.by_tag()
    assert by_tag["invoices"].calls == by_tag["receipts"].calls == by_tag[None].calls == 1
    assert az.tracker.calls == 3 and az.tracker.usage.input_tokens == 300

    summary = az.tracker.summary()
    assert "tracker: global — 3 calls, 0 errors" in summary
    assert "gpt-4o-mini-2024-07-18" in summary and "TOTAL" in summary
    assert "(none)" in az.tracker.summary(by="tag")


def test_nested_track_scopes():
    client = FakeClient(*(completion(INVOICE) for _ in range(3)))
    with az.track("outer") as outer:
        az.structured("1", Invoice, client=client)
        with az.track("inner") as inner:
            az.structured("2", Invoice, client=client)
    az.structured("3", Invoice, client=client)

    assert (outer.calls, inner.calls, az.tracker.calls) == (2, 1, 3)
    assert [r.messages[-1]["content"] for r in outer.records] == ["1", "2"]


def test_structured_many_keeps_order_and_tracks_worker_threads():
    prompts = [f"invoice {i}" for i in range(20)]
    with az.track("batch") as batch:
        results = az.structured_many(
            prompts, Invoice, concurrency=5, tag="bulk", client=FakeClient(responder=numbered_invoice)
        )

    assert [r.parsed.vendor for r in results] == [f"v{i}" for i in range(20)]
    assert batch.calls == 20 and az.tracker.calls == 20
    assert all(r.record.fn == "structured_many" and r.record.tag == "bulk" for r in results)


def test_structured_many_return_exceptions():
    def responder(kwargs):
        if kwargs["messages"][-1]["content"] == "bad":
            return completion(None, refusal="no")
        return completion(INVOICE)

    results = az.structured_many(
        ["ok", "bad", "ok"], Invoice, return_exceptions=True, client=FakeClient(responder=responder)
    )
    assert isinstance(results[1], az.RefusalError)
    assert results[0].parsed.vendor == results[2].parsed.vendor == "Acme"

    with pytest.raises(az.RefusalError):
        az.structured_many(["ok", "bad"], Invoice, client=FakeClient(responder=responder))


# --- async ---------------------------------------------------------------------


async def test_astructured():
    res = await az.astructured("q", list[Invoice], client=AsyncFakeClient(completion({"value": [INVOICE]})))
    assert res.parsed == [Invoice(**INVOICE)]
    assert az.tracker.last.fn == "astructured"


async def test_astructured_retries_validation():
    client = AsyncFakeClient(completion({**INVOICE, "total": -1}), completion(INVOICE))
    res = await az.astructured("q", Invoice, client=client)
    assert res.attempts == 2


async def test_astructured_many_tracked_across_tasks():
    prompts = [f"invoice {i}" for i in range(12)]
    with az.track("async-batch") as batch:
        results = await az.astructured_many(
            prompts, Invoice, concurrency=3, client=AsyncFakeClient(responder=numbered_invoice)
        )
    assert [r.parsed.vendor for r in results] == [f"v{i}" for i in range(12)]
    assert batch.calls == 12


async def test_aextract():
    client = AsyncFakeClient(completion(INVOICE))
    res = await az.aextract("text", Invoice, client=client)
    assert res.record.fn == "aextract" and "Extract" in client.requests[0]["messages"][0]["content"]


# --- logging -------------------------------------------------------------------


def test_log_file_has_question_output_and_usage(tmp_path):
    path = az.enable_logging(tmp_path / "logs" / "calls.jsonl")
    az.structured(
        "What's the total?", Invoice, tag="inv", metadata={"user_id": "u1"}, client=FakeClient(completion(INVOICE))
    )
    with pytest.raises(az.ValidationFailedError):
        az.structured("Again?", Invoice, validation_retries=0, client=FakeClient(completion("oops")))

    ok, failed = az.read_log(path)
    assert ok["messages"] == [{"role": "user", "content": "What's the total?"}]
    assert ok["output"] == INVOICE and ok["raw_output"] is None
    assert ok["status"] == "ok" and ok["schema"] == "Invoice" and ok["tag"] == "inv"
    assert ok["usage"] == {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 0, "reasoning_tokens": 0}
    assert ok["cost_usd"] == pytest.approx(cost(az.PRICING["gpt-4o"]))
    assert ok["metadata"] == {"user_id": "u1"} and ok["model"] == GPT4O and ok["deployment"] == DEPLOY

    assert failed["status"] == "invalid" and failed["output"] is None and failed["raw_output"] == "oops"


def test_log_without_content(tmp_path):
    path = az.enable_logging(tmp_path / "calls.jsonl", content=False)
    az.structured("secret question", Invoice, client=FakeClient(completion(INVOICE)))
    [row] = az.read_log(path)
    assert "messages" not in row and "output" not in row and "raw_output" not in row
    assert row["usage"]["input_tokens"] == 100
    assert "secret" not in path.read_text()


def test_hooks_receive_records_and_cannot_break_calls(caplog):
    seen = []

    @az.add_hook
    def collect(rec):
        seen.append(rec)

    az.add_hook(lambda rec: 1 / 0)
    with caplog.at_level(logging.ERROR, logger="azure_openai_utils"):
        res = az.structured("q", Invoice, client=FakeClient(completion(INVOICE)))

    assert seen == [res.record]
    assert any("hook" in m for m in caplog.messages)
    az.remove_hook(collect)


# --- clients -------------------------------------------------------------------


@pytest.fixture
def azure_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_VERSION", raising=False)


def test_get_client_uses_v1_api_by_default(azure_env):
    client = az.get_client()
    assert type(client) is openai.OpenAI
    assert str(client.base_url) == "https://res.openai.azure.com/openai/v1/"
    assert az.get_client() is client


def test_get_client_uses_classic_client_when_api_version_set(azure_env):
    client = az.get_client(api_version="2024-10-21")
    assert isinstance(client, openai.AzureOpenAI)


def test_get_client_without_key_uses_entra_id(azure_env, monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    assert type(az.get_client()) is openai.OpenAI  # token provider wired in; no network until a request


def test_get_client_requires_endpoint(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
        az.get_client()


def test_async_client_is_cached_per_event_loop(azure_env):
    async def same_client_twice():
        return az.get_async_client() is az.get_async_client(), az.get_async_client()

    same, first = asyncio.run(same_client_twice())
    _, second = asyncio.run(same_client_twice())
    assert same and first is not second
