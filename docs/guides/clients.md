# Clients and authentication

Most of the time you set a few environment variables and never touch a client. This page covers what
happens behind the scenes and how to take control when you need to.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | yes | `https://<resource>.openai.azure.com/`. The module adds the API path itself. |
| `AZURE_OPENAI_DEPLOYMENT` | yes, unless you pass `deployment=` on each call | The default deployment name. |
| `AZURE_OPENAI_API_KEY` | no | API key. If it's unset, the module signs in with Entra ID. |
| `OPENAI_API_VERSION` | no | Set it only to use a dated api-version instead of the v1 API (see below). |

The variables are read when the first request is made, not at import time, so you can set them (or call
`load_dotenv()`) after importing the module.

## API key or Entra ID

=== "API key"

    Set `AZURE_OPENAI_API_KEY`, or pass `api_key=` to [`get_client`](#building-a-client-yourself).

=== "Entra ID (keyless)"

    Leave the API key unset and install `azure-identity`:

    ```bash
    pip install azure-identity
    ```

    The module then authenticates with
    [`DefaultAzureCredential`](https://learn.microsoft.com/python/api/azure-identity/azure.identity.defaultazurecredential),
    which tries a chain of credentials: a service principal from environment variables, workload identity,
    managed identity, and then developer sign-ins such as `az login` or `azd auth login`. Tokens are fetched
    and refreshed automatically.

    The identity needs the **Cognitive Services OpenAI User** role (or higher) on the Azure OpenAI resource.

## v1 API or a dated api-version

By default the module uses Azure OpenAI's **v1 API**: a plain `openai.OpenAI` client pointed at
`<endpoint>/openai/v1/`. There's no `api-version` to choose or update.

If you set `OPENAI_API_VERSION` (or pass `api_version=`), it switches to the classic `openai.AzureOpenAI`
client with that version. You'd do this for an older resource, or when your company requires a pinned version.

```bash
export OPENAI_API_VERSION="2024-10-21"
```

The rest of the module works the same way with either client.

## Building a client yourself

Every function accepts `client=`. `get_client()` and `get_async_client()` build clients with the same rules as
above, and any argument you pass overrides the matching environment variable:

```python
import os

import azure_openai_utils as az

client = az.get_client(
    endpoint="https://my-other-resource.openai.azure.com/",
    api_key=os.environ["OTHER_RESOURCE_KEY"],
    max_retries=5,
    timeout=120,
)

res = az.structured(prompt, Invoice, client=client, deployment="gpt-4o-eu")
```

Clients are cached per set of arguments, so calling `get_client(...)` repeatedly returns the same client and
reuses its connection pool.

For async code:

```python
aclient = az.get_async_client(endpoint="https://my-other-resource.openai.azure.com/")
res = await az.astructured(prompt, Invoice, client=aclient)
```

Async clients are cached **per event loop**, because an async client can't be used after its loop has
closed. Scripts that call `asyncio.run()` more than once work without any extra setup.

### Using two resources at once

```python
us = az.get_client(endpoint="https://contoso-us.openai.azure.com/")
eu = az.get_client(endpoint="https://contoso-eu.openai.azure.com/")

az.structured(prompt, Answer, client=us, deployment="gpt-4o", tag="us")
az.structured(prompt, Answer, client=eu, deployment="gpt-4o", tag="eu")
```

### Bringing your own SDK client

`client=` accepts any OpenAI SDK client (`OpenAI`, `AzureOpenAI` or their async versions). Build one yourself
when you need options that `get_client()` doesn't expose, such as default headers or a custom HTTP client:

```python
import os

from openai import OpenAI

client = OpenAI(
    base_url="https://my-resource.openai.azure.com/openai/v1/",
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    default_headers={"x-app-name": "invoice-service"},
)
res = az.structured(prompt, Invoice, client=client)
```

## Timeouts and network retries

The OpenAI SDK handles network retries, with exponential backoff on connection errors, timeouts, 429s and 5xx
responses. The module's defaults are:

| Setting | Default | Change it with |
|---|---|---|
| `max_retries` | 3 | `get_client(max_retries=...)` |
| `timeout` | 60 seconds | `get_client(timeout=...)` |

These are separate from [validation retries](structured-output.md#validation-retries), which re-ask the model
when its *answer* is invalid.
