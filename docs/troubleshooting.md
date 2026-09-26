# Troubleshooting

Common problems, what causes them, and how to fix them.

## Setup

??? failure "`ValueError: No Azure endpoint: pass endpoint=... or set AZURE_OPENAI_ENDPOINT`"

    The module doesn't know which Azure resource to call. Set the variable in the environment your code runs
    in:

    ```bash
    export AZURE_OPENAI_ENDPOINT="https://<resource-name>.openai.azure.com/"
    ```

    If you use a `.env` file, make sure `load_dotenv()` runs before the first call.

??? failure "`ValueError: No deployment: pass deployment=... or set AZURE_OPENAI_DEPLOYMENT`"

    Set `AZURE_OPENAI_DEPLOYMENT` to your deployment's name, or pass `deployment="..."` on each call. This is
    the name you gave the deployment in Azure, which may differ from the model's name.

??? failure "`RuntimeError: No API key: pass api_key=... or set AZURE_OPENAI_API_KEY, or pip install azure-identity ...`"

    No API key was found, so the module tried to sign in with Entra ID, but `azure-identity` isn't installed.
    Either set `AZURE_OPENAI_API_KEY`, or run `pip install azure-identity` and sign in (for example with
    `az login`).

??? failure "`openai.NotFoundError` (404) on every call"

    Usually one of these:

    - The **deployment name** is wrong. Check it in Azure AI Foundry under *Deployments*.
    - The **endpoint** points to a different resource from the one that has the deployment.
    - You set `OPENAI_API_VERSION` to a version your resource doesn't support. Unset it to use the v1 API.

??? failure "`openai.AuthenticationError` / `PermissionDeniedError` (401 / 403)"

    - **API key:** the key belongs to another resource, or it has been rotated.
    - **Entra ID:** your identity needs the **Cognitive Services OpenAI User** role on the resource. New role
      assignments can take a few minutes to take effect. Check which account you're signed in as with
      `az account show`.

??? failure "`ImportError` or strange attribute errors from `openai`"

    Check that no file in your project is named `openai.py`, since it would hide the real package. Also check
    your versions: the module needs `openai>=1.106` and `pydantic>=2.8`.

## Schemas

??? failure "`BadRequestError` (400) mentioning `response_format`, `json_schema`, `default` or `additionalProperties`"

    Azure's strict mode rejected the schema. Common causes:

    - **A default other than `None`**, e.g. `count: int = 0`. Use `count: int` or `count: int | None = None`.
    - **A free-form dict**, e.g. `extra: dict[str, str]`. Use a list of small models instead, such as
      `list[KeyValue]`.
    - **An older deployment or api-version without structured outputs.** Use `mode="json"`.

??? failure "`TypeError: structured() sets model itself; use deployment= to pick the model`"

    You passed `model=`, `messages=`, `response_format=` or `stream=`. The module builds those itself. Pass
    `deployment="..."` to choose the model, and pass the conversation as the first argument.

## Answers

??? failure "`TruncatedError`: output hit the token limit"

    The JSON was cut off. Pass a larger limit, such as `max_completion_tokens=4000`, or ask for less output,
    for example with fewer items or shorter text fields. Reasoning models (o-series, gpt-5) spend part of the
    limit on hidden reasoning, so they need more headroom. `reasoning_effort="low"` also helps.

??? failure "`ValidationFailedError` even with retries"

    Look at `err.record.errors` to see what failed on each attempt, and `err.record.raw_output` to see the
    model's last answer. Then:

    - make the rule visible to the model with a `Field(description=...)` that states it,
    - give it more tries with `validation_retries=2`,
    - or check whether the validator is too strict for real data.

??? failure "The model makes up values instead of leaving fields empty"

    Make those fields nullable (`str | None`), and use `extract()`, whose system prompt tells the model to
    return `null` for anything the text doesn't state.

??? failure "`ContentFilterError`"

    Azure's content filter blocked the prompt or the answer. `err.record.errors` says which. If it's a false
    positive for your use case, the filter configuration of your Azure OpenAI resource can be adjusted in
    Azure AI Foundry.

## Tracking

??? question "`cost` is `None` and I see a warning about a missing price"

    The model Azure reported isn't in the price table. Add it:

    ```python
    az.set_price("my-deployment-name", input=2.50, output=10.00, cached_input=1.25)  # USD per 1M tokens
    ```

    See [Pricing](guides/tracking.md#pricing).

??? question "Calls made in my own threads don't show up in `track()`"

    Threads you start yourself don't inherit the `track()` scope. Submit the work with
    `contextvars.copy_context().run`, or use `structured_many`, which does this for you. The global
    `az.tracker` counts those calls either way. See
    [Batches and async](guides/batch-and-async.md#tracking-a-batch).

??? question "The costs don't match my Azure bill exactly"

    The built-in prices are Global Standard list prices. Data Zone, Regional and Provisioned (PTU)
    deployments are billed differently, so use `set_price("<deployment>", ...)` with your actual rates.

## Still stuck?

[Open an issue](https://github.com/utkarsh5026/llm_utils/issues) with the error message and, if you can, the
`CallRecord` from the log (use `content=False` if the prompt is sensitive).
