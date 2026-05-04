"""Shared OpenAI / Azure OpenAI provider setup for this lab.

The three notebooks in this folder all call ``get_model()`` to build a
single model handle for the OpenAI Agents SDK. Configuration is read
(in order of precedence) from:

  1. A local ``provider_config.py`` next to this file (gitignored).
     Copy ``provider_config.py.example`` to ``provider_config.py`` and
     edit it once -- all three notebooks pick it up.
  2. Environment variables (``OPENAI_API_KEY``, ``AZURE_OPENAI_*``).

Azure OpenAI wins if both are configured. Entra ID auth is the default
for Azure OpenAI -- supply ``AZURE_OPENAI_API_KEY`` only if the resource
has key auth enabled and you prefer to use it.
"""

from __future__ import annotations

import os


def _load_local_config():
    """Return the local ``provider_config`` module if it exists, else None."""
    try:
        import provider_config  # type: ignore
        return provider_config
    except ImportError:
        return None


def get_model():
    """Build and return a model handle for ``Agent(model=...)``.

    Also disables OpenAI Agents tracing, since the tracing exporter posts
    to ``api.openai.com`` -- which is unreachable on Azure-OpenAI-only
    networks and surfaces as a confusing ConnectError on every run.
    """
    cfg = _load_local_config()

    def _v(name: str, default: str = '') -> str:
        if cfg is not None:
            v = getattr(cfg, name, '')
            if v:
                return v
        return os.environ.get(name, default)

    openai_api_key  = _v('OPENAI_API_KEY')
    openai_model    = _v('OPENAI_MODEL', 'gpt-4o')
    aoai_endpoint   = _v('AZURE_OPENAI_ENDPOINT')
    aoai_deployment = _v('AZURE_OPENAI_DEPLOYMENT')
    aoai_api_ver    = _v('AZURE_OPENAI_API_VERSION', '2024-10-21')
    aoai_key        = _v('AZURE_OPENAI_API_KEY')

    from agents import set_tracing_disabled
    set_tracing_disabled(True)

    if aoai_endpoint and aoai_deployment:
        from openai import AsyncAzureOpenAI
        from agents import OpenAIChatCompletionsModel

        if aoai_key:
            client = AsyncAzureOpenAI(
                api_key=aoai_key,
                azure_endpoint=aoai_endpoint,
                api_version=aoai_api_ver,
            )
            auth_mode = 'API key'
        else:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(),
                'https://cognitiveservices.azure.com/.default',
            )
            client = AsyncAzureOpenAI(
                azure_ad_token_provider=token_provider,
                azure_endpoint=aoai_endpoint,
                api_version=aoai_api_ver,
            )
            auth_mode = 'Entra ID (DefaultAzureCredential)'

        print(f'Using Azure OpenAI: {aoai_endpoint} (deployment: {aoai_deployment}, auth: {auth_mode})')
        return OpenAIChatCompletionsModel(model=aoai_deployment, openai_client=client)

    if openai_api_key:
        # The OpenAI Agents SDK reads OPENAI_API_KEY from the env automatically.
        os.environ['OPENAI_API_KEY'] = openai_api_key
        print(f'Using OpenAI: {openai_model}')
        return openai_model

    raise RuntimeError(
        'No OpenAI provider configured. Either copy provider_config.py.example '
        'to provider_config.py and fill it in, or set environment variables: '
        'AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_DEPLOYMENT (preferred), or OPENAI_API_KEY.'
    )
