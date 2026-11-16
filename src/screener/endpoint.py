"""Opening a System One client for whichever backend the settings name.

TypeSafe's SDK is used for both backends. A self-hosted server such as CLM (`clm-serve`)
speaks the same protocol — `POST /v1/systemone` and `GET /v1/models` with a Bearer key,
answers in the same shape — so pointing the SDK at a different `base_url` is all it takes.
The one thing the SDK cannot guess is the model name: it defaults to `jev-latest`, which a
self-hosted server does not know, so when none is set the server is asked what it has.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, TypeSafeClient, TypeSafeError

from .config import Settings, SetupError


class EndpointUnavailable(SetupError):
    """The custom endpoint could not be reached, or lists no models."""


def _retry() -> RetryPolicy:
    return RetryPolicy(max_retries=4)


def _probe_retry() -> RetryPolicy:
    """For "is it there?" checks: answer fast rather than back off for seconds."""
    return RetryPolicy(max_retries=1, backoff_initial=0.2, backoff_max=0.5)


def list_models(settings: Settings) -> list[str]:
    """The model names the configured backend offers. Raises EndpointUnavailable."""
    options = settings.client_options()
    try:
        with TypeSafeClient(**options, retry=_probe_retry()) as client:
            return [m.name for m in client.models.list().models]
    except TypeSafeError as error:
        raise EndpointUnavailable(f"{_where(settings)}: {error}") from error


@asynccontextmanager
async def open_client(settings: Settings) -> AsyncIterator[AsyncTypeSafeClient]:
    """An async client for the configured backend, with the model resolved."""
    options = settings.client_options()
    if settings.custom_endpoint and not options["model"]:
        async with AsyncTypeSafeClient(**options, retry=_probe_retry()) as probe:
            try:
                listed = await probe.models.list()
            except TypeSafeError as error:
                raise EndpointUnavailable(f"{_where(settings)}: {error}") from error
        if not listed.models:
            raise EndpointUnavailable(f"The endpoint at {settings.endpoint_url} lists no models.")
        options["model"] = listed.models[0].name
    async with AsyncTypeSafeClient(**options, retry=_retry()) as client:
        yield client


def _where(settings: Settings) -> str:
    return f"Could not use the endpoint at {settings.endpoint_url}" if settings.custom_endpoint \
        else "Could not reach TypeSafe"
