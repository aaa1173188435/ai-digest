"""The LLM seam.

One narrow interface -- ``complete_json`` -- so that the pipeline never learns
which model it is talking to. That is what lets the test suite run offline, and
what makes "swap in a cheaper model" a one-line config change instead of a
refactor.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    """Raised when the provider fails in a way the caller cannot paper over."""


@runtime_checkable
class LLMClient(Protocol):
    name: str
    usage: dict[str, int]

    async def complete_json(self, *, system: str, user: str) -> dict: ...


def build_llm(settings) -> LLMClient:
    """Construct the configured provider. Import here to keep import cost lazy."""
    provider = (settings.llm_provider or "deepseek").lower()

    if provider == "fake":
        from .fake import FakeLLM

        return FakeLLM()

    if provider == "deepseek":
        from .deepseek import DeepSeekClient

        return DeepSeekClient(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            timeout=settings.llm_timeout,
        )

    raise LLMError(f"unknown llm_provider: {provider!r}")
