# Getting started

This page takes you from an empty folder to your first typed answer from Azure OpenAI. It takes about five
minutes.

## What you need

- **Python 3.10 or newer.**
- **An Azure OpenAI resource** with a chat model deployed (for example `gpt-4o-mini`, `gpt-4.1` or `gpt-5-mini`).
  You'll need two values from it:
    - the **endpoint**, which looks like `https://<resource-name>.openai.azure.com/`. You'll find it under
      *Keys and Endpoint* in the Azure portal, or on the resource's overview page in Azure AI Foundry.
    - the **deployment name**. This is the name *you* gave the deployment when you created it, which may
      differ from the model's name.

## 1. Copy the module into your project

The module is a single file. Download it next to your code:

```bash
curl -O https://raw.githubusercontent.com/utkarsh5026/llm_utils/main/azure_openai_utils.py
```

You can also open [the file on GitHub](https://github.com/utkarsh5026/llm_utils/blob/main/azure_openai_utils.py)
and save it. Keep the name `azure_openai_utils.py`, and don't rename it to `openai.py`, because that would hide
the real `openai` package.

## 2. Install the dependencies

=== "pip"

    ```bash
    pip install "openai>=1.106" "pydantic>=2.8"
    pip install azure-identity   # optional: only needed for keyless Entra ID sign-in
    ```

=== "uv"

    ```bash
    uv add "openai>=1.106" "pydantic>=2.8"
    uv add azure-identity   # optional: only needed for keyless Entra ID sign-in
    ```

=== "poetry"

    ```bash
    poetry add "openai>=1.106" "pydantic>=2.8"
    poetry add azure-identity   # optional: only needed for keyless Entra ID sign-in
    ```

## 3. Tell it where your deployment is

The module reads its settings from environment variables, so your code doesn't need to hold any secrets.
Choose how you want to sign in:

=== "API key"

    The quickest option. Copy a key from *Keys and Endpoint* in the Azure portal.

    ```bash
    export AZURE_OPENAI_ENDPOINT="https://<resource-name>.openai.azure.com/"
    export AZURE_OPENAI_DEPLOYMENT="my-gpt-4o-mini"
    export AZURE_OPENAI_API_KEY="<your-key>"
    ```

=== "Entra ID (keyless)"

    No secrets at all. Leave `AZURE_OPENAI_API_KEY` unset and the module signs in with
    [`DefaultAzureCredential`](https://learn.microsoft.com/python/api/azure-identity/azure.identity.defaultazurecredential).
    On your laptop that's your `az login` session; in Azure it's the app's managed identity.

    ```bash
    pip install azure-identity
    az login

    export AZURE_OPENAI_ENDPOINT="https://<resource-name>.openai.azure.com/"
    export AZURE_OPENAI_DEPLOYMENT="my-gpt-4o-mini"
    ```

    Your identity needs the **Cognitive Services OpenAI User** role on the Azure OpenAI resource.

!!! note "Using a `.env` file?"

    Load it before the first call, for example with `python-dotenv`'s `load_dotenv()`. The module reads the
    variables when it makes its first request, so they only have to be set by then, not when you import it.

## 4. Make your first call

Save this as `first_call.py` next to `azure_openai_utils.py`:

```python
from pydantic import BaseModel

import azure_openai_utils as az


class Movie(BaseModel):
    title: str
    year: int
    director: str
    genres: list[str]


res = az.structured("Tell me about the film that won Best Picture at the 1995 Oscars.", Movie)

print(res.parsed)
print(res.parsed.title, "was directed by", res.parsed.director)
print(res.usage)
print("cost in USD:", res.cost)
```

Run it:

```console
$ python first_call.py
title='Forrest Gump' year=1994 director='Robert Zemeckis' genres=['Drama', 'Romance']
Forrest Gump was directed by Robert Zemeckis
Usage(input_tokens=58, output_tokens=29, cached_tokens=0, reasoning_tokens=0)
cost in USD: 2.61e-05
```

`res.parsed` is a real `Movie` object. Your editor autocompletes `.title` and `.year`, and `year` is
guaranteed to be an `int`.

## 5. What came back

Every call returns a `StructuredResult`:

| Attribute | What it holds |
|---|---|
| `parsed` | Your answer, as an instance of the schema you passed. |
| `usage` | Token counts: `input_tokens`, `output_tokens`, `cached_tokens`, `reasoning_tokens`, `total_tokens`. |
| `cost` | Cost in US dollars, or `None` if the model has no known price (see [Tokens and cost](guides/tracking.md#pricing)). |
| `model` | The exact model Azure used, e.g. `gpt-4o-mini-2024-07-18`. |
| `deployment` | The deployment the call went to. |
| `latency_s` | Wall-clock seconds, including any retries. |
| `attempts` | `1`, or more if the answer failed validation and was retried. |
| `record` | The full [`CallRecord`](reference.md#callrecord) that was also sent to the tracker, log and hooks. |
| `raw` | The last raw `ChatCompletion` from the SDK, in case you need anything else. |

## 6. Turn on the extras

Add two lines to see where your tokens go:

```python
import azure_openai_utils as az

az.enable_logging("logs/llm_calls.jsonl")  # every call, one JSON line each

# ... your code ...

print(az.tracker.summary())  # tokens and cost for everything this process did
```

```text
tracker: global — 2 calls, 0 errors
model                   calls  input  cached  output  reasoning   cost($)
gpt-4o-2024-11-20           1    812       0      41          0  0.002440
gpt-4o-mini-2024-07-18      1    500     256      30          0  0.000074
TOTAL                       2  1,312     256      71          0  0.002514
```

## Next steps

<div class="grid cards" markdown>

-   **[Structured output](guides/structured-output.md)**

    How to write schemas, use lists, enums and literals, set prompts, and how validation retries work.

-   **[Extracting data from text](guides/extraction.md)**

    Pull fields from documents with `extract()`.

-   **[Batches and async](guides/batch-and-async.md)**

    Process many inputs at once.

-   **[Recipes](recipes.md)**

    Complete programs you can copy and adapt.

</div>
