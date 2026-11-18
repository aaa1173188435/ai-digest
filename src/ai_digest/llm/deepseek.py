"""DeepSeek provider (OpenAI-compatible chat completions).

Two things beyond the happy path:

* **Token accounting.** ``usage`` is accumulated per run so the digest can
  report what it cost. A pipeline whose cost is invisible is a pipeline whose
  cost creeps.
* **JSON mode + local validation.** ``response_format={"type": "json_object"}``
  is a hint, not a guarantee; the caller still validates every field with
  Pydantic. Malformed output raises :class:`LLMError` and the caller degrades to
  a heuristic instead of failing the run.
"""

from __future__ import annotations

import json

import httpx

from .base import LLMError


class DeepSeekClient:
    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: float = 90.0,
    ) -> None:
        if not api_key:
            raise LLMError(
                "DEEPSEEK_API_KEY is empty. Set it in .env, or run with "
                "LLM_PROVIDER=fake to exercise the pipeline offline."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self.usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "calls": 0,
        }

    async def complete_json(self, *, system: str, user: str) -> dict:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 3000,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=headers
            )

        self.usage["calls"] += 1

        if response.status_code >= 400:
            body = response.text[:400]
            raise LLMError(f"deepseek HTTP {response.status_code}: {body}")

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"deepseek returned non-JSON body: {exc}") from exc

        usage = data.get("usage") or {}
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected deepseek response shape: {data!r:.400}") from exc

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model did not emit valid JSON: {content[:300]!r}") from exc

        if not isinstance(parsed, dict):
            raise LLMError(f"expected a JSON object, got {type(parsed).__name__}")
        return parsed
