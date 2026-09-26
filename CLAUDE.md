# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A collection of **drop-in, single-file LLM provider modules**. Each module (currently only
`azure_openai_utils.py`) is copied as-is into other projects, so these rules are hard constraints:

- **No imports between project files.** A module may import only its provider SDK, `pydantic`, and the stdlib.
  Shared concepts (`Usage`, `CallRecord`, `UsageTracker`, …) are deliberately duplicated per module rather than factored into a common file.
- **Import optional deps lazily** inside the function that needs them (e.g. `azure.identity` in `_token_provider`) so the module imports without them.
- **Never name a module after its SDK package** (`openai.py` would shadow `openai`). Use `<provider>_utils.py`.
- **Python 3.10+.** No 3.11+ syntax or stdlib (no `TaskGroup`, `Self`, `except*`, PEP 695 generics).
- Declared minimums in the module docstring, README and `pyproject.toml` are `openai>=1.106` (first release where `OpenAI(api_key=<callable>)` calls a token provider, needed for Entra ID on the v1 API) and `pydantic>=2.8`. These are tested floors; don't lower them without re-running the suite on the older versions.
- Future provider modules should expose the same public names and result shapes (`structured`, `astructured`, `structured_many`, `extract`, `tracker`, `track`, `enable_logging`, …) so switching providers means changing the import.

## Commands

`make help` lists everything. The `make` targets are what CI runs, so prefer them over ad-hoc commands.

```bash
make install        # uv sync (deps live in the `dev` dependency group; no package build) + pre-commit hook
make lint           # pre-commit on all files: ruff check, ruff format (incl. Python blocks in *.md), pyright, hygiene
make fmt            # ruff --fix + ruff format
make test           # offline suite; the live test auto-skips
make cov            # same, with a coverage report
make test-min       # suite on Python 3.10 + openai==1.106.0 + pydantic==2.8.0 (the declared floors)
make check-dropin   # imports the module alone in an empty dir with only its required deps
make check          # lint + test + test-min + check-dropin
make live           # real Azure call; needs AZURE_OPENAI_ENDPOINT / _DEPLOYMENT (+ _API_KEY or Entra ID)
make docs           # live-preview the MkDocs site; make docs-build = strict build (what CI runs)

uv run pytest tests/test_azure_openai_utils.py::test_validation_retry_feeds_errors_back_and_sums_usage   # single test
uv run pytest -k "track or log"                                                                         # by keyword
```

- Tool config (ruff, pyright, pytest, coverage) is all in `pyproject.toml`. Both ruff (`target-version = "py310"`)
  and pyright (`pythonVersion = "3.10"`) target the 3.10 floor, so they flag newer syntax/stdlib.
- The pre-commit hooks call ruff/pyright via `uv run`, so tool versions come from `uv.lock`; bump them with
  `uv lock --upgrade-package ruff` (etc.), not in `.pre-commit-config.yaml`.
- `pre-commit --all-files` only sees git-tracked files; `git add` new files before `make lint` or they are skipped.
- `pytest-asyncio` runs in `asyncio_mode = "auto"`, so `async def test_*` needs no decorator.

CI (`.github/workflows/ci.yml`): a `lint` job (`make lint`), a `test` matrix over Python 3.10–3.14 (`uv sync --locked`
then `make cov`, so a stale `uv.lock` fails CI), and a `compat` job (`make test-min`, `make check-dropin`). A `live`
job runs only on manual dispatch with the `live` input ticked, using `AZURE_OPENAI_*` repository secrets.
`astral-sh/setup-uv` is pinned to an exact release because it no longer publishes floating major tags.
`.github/workflows/docs.yml` builds the site with `--strict` on PRs touching docs and deploys it to GitHub Pages from `main`.
Dependabot updates action pins only. Refresh Python deps with `uv lock --upgrade`, which leaves the `pyproject.toml` floors alone.

## Architecture of `azure_openai_utils.py`

The file is organized in numbered sections (config/pricing → client → records → tracking → logging →
public structured-output API → internals). Things that span sections:

**One call = one `_Call` object.** Every public function (`structured`, `astructured`, `structured_many`,
`astructured_many`, `extract`, `aextract`) builds a `_Call` and hands it to `_run` (sync) or `_arun` (async).
Those two runners are the only sync/async duplication. They just loop `call.request()` → `create()` →
`call.handle(resp)`. All request building, response checks, validation retries and recording live in `_Call`.
Add new behaviour there, not in the public functions.

**`chat.completions.create()` + our own validation, never `.parse()`.** The strict JSON schema comes from the
SDK's public `openai.pydantic_function_tool(model)["function"]["parameters"]`, and the response is validated
with `model_validate_json`. This is deliberate: `.parse()` raises inside the SDK when a Pydantic validator
fails, and that response's token usage is lost. Our way, every attempt's usage is summed and validation errors
are fed back to the model for a retry.

**Non-`BaseModel` schemas are wrapped.** `list[X]`, `Literal[...]`, `Enum`, `int`, etc. become
`create_model(name, value=(schema, ...))` on the wire and are unwrapped from `.value` afterwards (`_Spec.wrapped`).
Specs are `lru_cache`d per `(schema, mode)`. Public signatures type the schema as `type[T] | Any`, which keeps
`StructuredResult[T]` inference for classes without pyright errors for `Literal`/unions.

**Single emission point: `_emit(record)`.** Every finished call, **including failures**, produces exactly one
`CallRecord` via `_Call.finish()`. `_emit` sends it to the global `tracker`, every active `track()` scope,
the JSONL log (if enabled) and user hooks. Because tracker totals and log lines come from the same object,
they always agree. Log-write and hook failures are caught and logged; they must never break a call.

**Failure paths.** `_Call.stop()` records the call and returns a `StructuredOutputError` subclass carrying
`.record` (status `refusal` / `content_filter` / `truncated` / `invalid`). `_Call.fail()` handles exceptions
raised by the SDK. It maps Azure's 400 `code == "content_filter"` to `ContentFilterError` and records every
other exception as status `error`, re-raising the original unchanged. `_run`/`_arun` catch `BaseException`, so a
`CancelledError` or `KeyboardInterrupt` is recorded as status `cancelled`; `_Call.record` (set in `finish()`) stops
`fail()` from recording a call twice. `astructured_many` awaits the tasks it cancels so they record before it raises.

**Scoped tracking uses `contextvars`.** `track()` pushes a `UsageTracker` onto the `_scopes` ContextVar.
asyncio tasks inherit it automatically. Threads don't, which is why `structured_many` submits work via
`contextvars.copy_context().run`. Keep that for any new thread-based helper.

**Pricing lookup** (`_price_for`): an exact match on the deployment name comes first (so `set_price("<deployment>", …)`
can carry Data Zone/Regional prices), then the model name Azure reports with only a trailing `-YYYY-MM-DD`
stripped. Don't switch to prefix matching: it would price `gpt-4o-mini-…` as `gpt-4o` and `gpt-5.1-…` as `gpt-5`.
Unknown models give `cost=None` plus a one-time warning. Calls with zero tokens cost `0.0`.

**Client selection** (`_make_client`): the default is Azure's v1 API (plain `OpenAI` on `<endpoint>/openai/v1/`,
no api-version, Entra scope `https://ai.azure.com/.default`). If `api_version` / `OPENAI_API_VERSION` is set, it uses
`AzureOpenAI` instead (scope `https://cognitiveservices.azure.com/.default`). Sync clients are `lru_cache`d.
Async clients are cached **per running event loop** in a `WeakKeyDictionary`, because an async client can't
outlive its loop (repeated `asyncio.run()` would otherwise break).

## Tests

- `FakeClient` / `AsyncFakeClient` in `tests/test_azure_openai_utils.py` mimic `client.chat.completions.create`.
  They replay a queue of responses or call a `responder(kwargs)` function (use a responder for concurrent tests,
  where call order isn't deterministic). Build responses with the `completion(...)` helper, which creates **real**
  `ChatCompletion` objects so attribute names stay faithful to the SDK.
- The autouse `clean_state` fixture resets module-global state: `tracker`, logging, `_hooks`, `PRICING`,
  `_unpriced_warned` and the sync client cache. **Any new module-level state must be reset there too.**
- The test file disables pyright's `reportArgumentType`/`reportOptionalMemberAccess` in its header because the
  fakes are duck-typed. The module itself must stay at 0 pyright errors.
- openai 3.x depends on `httpx2`, not `httpx`, so tests import `httpx2` with a fallback when building SDK exceptions.

## Docs site

User docs live in `docs/` (MkDocs Material, config in `mkdocs.yml`, deps in the `docs` dependency group) and are
published to https://utkarsh5026.github.io/llm_utils/. `docs/reference.md` is hand-written, so **any change to a
public name, signature, default, exception or record field must be reflected there and in the relevant guide.**
Python blocks in `docs/*.md` are ruff-formatted by pre-commit; blocks that aren't valid Python (signatures) are skipped.
