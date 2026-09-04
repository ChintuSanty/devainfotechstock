"""Local LLM access.

Default provider is Ollama on 127.0.0.1:11434. An OpenAI-compatible provider is
included so anything exposing that API (llama.cpp server, LM Studio, vLLM) can
be pointed at without touching the rest of the app. No provider here talks to a
remote service unless the user changes the base URL themselves.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMClient(ABC):
    def __init__(self, base_url: str, model: str, temperature: float, timeout: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    @abstractmethod
    def complete(self, system: str, user: str) -> str: ...

    @abstractmethod
    def health(self) -> dict[str, Any]: ...

    def _post(self, path: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"{self.base_url}{path} returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(
                f"Could not reach the local model at {self.base_url}. "
                f"Is it running? ({exc.reason})"
            ) from exc

    def _get(self, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(f"{self.base_url}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise LLMError(f"Could not reach {self.base_url}{path}: {exc}") from exc


class OllamaClient(LLMClient):
    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": self.temperature},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = self._post("/api/chat", payload)
        content = (data.get("message") or {}).get("content", "")
        if not content:
            raise LLMError("The local model returned an empty response.")
        return _strip_reasoning(content)

    def health(self) -> dict[str, Any]:
        try:
            data = self._get("/api/tags")
        except LLMError as exc:
            return {"available": False, "error": str(exc), "models": []}
        models = [m.get("name", "") for m in data.get("models", [])]
        return {
            "available": True,
            "models": models,
            "model_installed": any(m == self.model or m.startswith(f"{self.model}:") for m in models),
            "model": self.model,
        }


class OpenAICompatibleClient(LLMClient):
    def __init__(self, base_url: str, model: str, temperature: float, timeout: int, api_key: str = "") -> None:
        super().__init__(base_url, model, temperature, timeout)
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = self._post("/v1/chat/completions", payload, self._headers())
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("The local model returned no choices.")
        return _strip_reasoning(choices[0].get("message", {}).get("content", ""))

    def health(self) -> dict[str, Any]:
        try:
            data = self._get("/v1/models", self._headers())
        except LLMError as exc:
            return {"available": False, "error": str(exc), "models": []}
        models = [m.get("id", "") for m in data.get("data", [])]
        return {
            "available": True,
            "models": models,
            "model_installed": self.model in models,
            "model": self.model,
        }


def build_client(settings: Any) -> LLMClient:
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
            api_key=settings.llm_api_key,
        )
    return OllamaClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
    )


def _strip_reasoning(text: str) -> str:
    """Drop <think>...</think> blocks that reasoning models emit."""
    while "<think>" in text and "</think>" in text:
        start = text.index("<think>")
        end = text.index("</think>") + len("</think>")
        text = text[:start] + text[end:]
    return text.strip()
