"""
LLM provider abstraction for OWL Agent Runtime.

Thin adapter layer over AI provider HTTP APIs (Anthropic, OpenAI).
Uses httpx for HTTP. Prevents vendor lock-in by normalizing request/response
formats behind a common interface.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base exception for provider errors."""


class AuthenticationError(ProviderError):
    """Invalid or missing API key."""


class RateLimitError(ProviderError):
    """Rate limit exceeded (local or remote)."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""
    id: str
    name: str
    arguments: dict  # parsed from JSON string


@dataclass
class ModelResponse:
    """Normalised response from a chat completion."""
    text: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"  # "end_turn" | "tool_use" | "max_tokens"


@dataclass
class StreamChunk:
    """A single piece of a streamed response."""
    type: str  # "text_delta" | "tool_call" | "usage" | "error"
    data: Any  # str for text, ToolCall for tool_call, dict for usage, str for error


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Simple sliding-window rate limiter."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._calls: List[float] = []

    def _prune(self):
        """Remove timestamps older than 60 seconds."""
        cutoff = time.monotonic() - 60.0
        self._calls = [t for t in self._calls if t > cutoff]

    def check(self):
        """Raise RateLimitError if the limit would be exceeded."""
        self._prune()
        if len(self._calls) >= self.max_per_minute:
            oldest = self._calls[0]
            retry_after = 60.0 - (time.monotonic() - oldest)
            raise RateLimitError(
                f"Rate limit exceeded ({self.max_per_minute}/min). "
                f"Retry after {retry_after:.1f}s.",
                retry_after=max(retry_after, 0.1),
            )

    def record(self):
        """Record that a call was made right now."""
        self._calls.append(time.monotonic())


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class LLMProvider:
    """Abstract base for LLM providers."""

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        timeout: float = 60,
        max_calls_per_minute: int = 10,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._rate_limiter = RateLimiter(max_calls_per_minute)

    def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        system: Optional[str] = None,
    ) -> ModelResponse:
        """Synchronous chat completion with tool support."""
        raise NotImplementedError

    def stream_chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        system: Optional[str] = None,
    ):
        """Generator yielding StreamChunk objects."""
        raise NotImplementedError

    def validate_key(self) -> bool:
        """Make a minimal API call to verify the key works."""
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------

    def _check_rate_limit(self):
        self._rate_limiter.check()
        self._rate_limiter.record()

    def _handle_http_error(self, status_code: int, body: str):
        """Map HTTP status codes to typed exceptions."""
        if status_code == 401:
            raise AuthenticationError(f"Invalid API key (HTTP 401): {body}")
        if status_code == 429:
            raise RateLimitError(f"Provider rate limit (HTTP 429): {body}")
        if status_code >= 400:
            raise ProviderError(f"API error (HTTP {status_code}): {body}")


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------


class AnthropicProvider(LLMProvider):
    """Adapter for the Anthropic Messages API."""

    API_URL = "https://api.anthropic.com/v1/messages"
    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.model is None:
            self.model = self.DEFAULT_MODEL

    # -- public API ---------------------------------------------------------

    def chat(self, messages, tools=None, system=None) -> ModelResponse:
        self._check_rate_limit()

        body = self._build_request(messages, tools, system, stream=False)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self.API_URL,
                    json=body,
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"Request timed out after {self.timeout}s") from exc

        if resp.status_code != 200:
            self._handle_http_error(resp.status_code, resp.text)

        return self._parse_response(resp.json())

    def stream_chat(self, messages, tools=None, system=None):
        self._check_rate_limit()

        body = self._build_request(messages, tools, system, stream=True)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream(
                    "POST",
                    self.API_URL,
                    json=body,
                    headers=self._headers(),
                ) as resp:
                    if resp.status_code != 200:
                        resp.read()
                        self._handle_http_error(resp.status_code, resp.text)

                    current_tool_id = None
                    current_tool_name = None
                    current_tool_json = ""

                    for line in resp.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[len("data: "):]
                        if not payload.strip():
                            continue
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        yield from self._handle_stream_event(event)

                        # Track tool state across events
                        etype = event.get("type", "")
                        if etype == "content_block_start":
                            cb = event.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                current_tool_id = cb.get("id", "")
                                current_tool_name = cb.get("name", "")
                                current_tool_json = ""
                        elif etype == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "input_json_delta":
                                current_tool_json += delta.get("partial_json", "")
                        elif etype == "content_block_stop":
                            if current_tool_name:
                                try:
                                    args = json.loads(current_tool_json) if current_tool_json else {}
                                except json.JSONDecodeError:
                                    args = {}
                                yield StreamChunk(
                                    type="tool_call",
                                    data=ToolCall(
                                        id=current_tool_id or "",
                                        name=current_tool_name,
                                        arguments=args,
                                    ),
                                )
                                current_tool_id = None
                                current_tool_name = None
                                current_tool_json = ""

        except httpx.TimeoutException as exc:
            raise ProviderError(f"Stream timed out after {self.timeout}s") from exc

    def validate_key(self) -> bool:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self.API_URL,
                    json={
                        "model": self.model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                return True
            self._handle_http_error(resp.status_code, resp.text)
            return False  # unreachable — _handle_http_error always raises
        except AuthenticationError:
            return False  # genuinely invalid key
        except ProviderError:
            raise  # 400, 429 etc — caller needs the real error
        except Exception:
            return False  # network errors, timeouts

    # -- internal -----------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _build_request(self, messages, tools, system, stream) -> dict:
        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
        return body

    def _parse_response(self, data: dict) -> ModelResponse:
        text_parts = []
        tool_calls = []

        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}),
                ))

        usage = data.get("usage", {})
        stop = data.get("stop_reason", "end_turn")

        return ModelResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            stop_reason=stop,
        )

    def _handle_stream_event(self, event):
        """Yield StreamChunks for a single SSE event."""
        etype = event.get("type", "")

        if etype == "message_start":
            msg = event.get("message", {})
            usage = msg.get("usage", {})
            if usage:
                yield StreamChunk(
                    type="usage",
                    data={"input_tokens": usage.get("input_tokens", 0)},
                )

        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                yield StreamChunk(type="text_delta", data=delta.get("text", ""))

        elif etype == "message_delta":
            usage = event.get("usage", {})
            if usage:
                yield StreamChunk(
                    type="usage",
                    data={"output_tokens": usage.get("output_tokens", 0)},
                )


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------


class OpenAIProvider(LLMProvider):
    """Adapter for the OpenAI Chat Completions API."""

    API_URL = "https://api.openai.com/v1/chat/completions"
    DEFAULT_MODEL = "gpt-4o-mini"

    # Map OpenAI finish reasons to normalised stop reasons
    _STOP_MAP = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.model is None:
            self.model = self.DEFAULT_MODEL

    # -- public API ---------------------------------------------------------

    def chat(self, messages, tools=None, system=None) -> ModelResponse:
        self._check_rate_limit()

        body = self._build_request(messages, tools, system, stream=False)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self.API_URL,
                    json=body,
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"Request timed out after {self.timeout}s") from exc

        if resp.status_code != 200:
            self._handle_http_error(resp.status_code, resp.text)

        return self._parse_response(resp.json())

    def stream_chat(self, messages, tools=None, system=None):
        self._check_rate_limit()

        body = self._build_request(messages, tools, system, stream=True)
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream(
                    "POST",
                    self.API_URL,
                    json=body,
                    headers=self._headers(),
                ) as resp:
                    if resp.status_code != 200:
                        resp.read()
                        self._handle_http_error(resp.status_code, resp.text)

                    # Accumulate tool calls across chunks
                    tool_accum: Dict[int, dict] = {}

                    for line in resp.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[len("data: "):]
                        if payload.strip() == "[DONE]":
                            break
                        if not payload.strip():
                            continue
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        yield from self._handle_stream_event(event, tool_accum)

        except httpx.TimeoutException as exc:
            raise ProviderError(f"Stream timed out after {self.timeout}s") from exc

    def validate_key(self) -> bool:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    self.API_URL,
                    json={
                        "model": self.model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                return True
            self._handle_http_error(resp.status_code, resp.text)
            return False  # unreachable — _handle_http_error always raises
        except AuthenticationError:
            return False  # genuinely invalid key
        except ProviderError:
            raise  # 400, 429 etc — caller needs the real error
        except Exception:
            return False  # network errors, timeouts

    # -- internal -----------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_request(self, messages, tools, system, stream) -> dict:
        all_messages = list(messages)
        if system:
            all_messages.insert(0, {"role": "system", "content": system})

        body: Dict[str, Any] = {
            "model": self.model,
            "messages": all_messages,
        }
        if tools:
            body["tools"] = tools
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    def _parse_response(self, data: dict) -> ModelResponse:
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        usage = data.get("usage", {})

        text = msg.get("content") or ""
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=args,
            ))

        raw_reason = choice.get("finish_reason", "stop")
        stop_reason = self._STOP_MAP.get(raw_reason, raw_reason)

        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            stop_reason=stop_reason,
        )

    def _handle_stream_event(self, event, tool_accum):
        """Yield StreamChunks for a single SSE chunk."""
        choices = event.get("choices", [])

        # Usage-only chunk (final)
        if not choices and "usage" in event:
            usage = event["usage"]
            yield StreamChunk(
                type="usage",
                data={
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                },
            )
            return

        if not choices:
            return

        delta = choices[0].get("delta", {})

        # Text content
        content = delta.get("content")
        if content:
            yield StreamChunk(type="text_delta", data=content)

        # Tool calls (incremental)
        for tc_delta in delta.get("tool_calls") or []:
            idx = tc_delta.get("index", 0)
            if idx not in tool_accum:
                tool_accum[idx] = {
                    "id": tc_delta.get("id", ""),
                    "name": tc_delta.get("function", {}).get("name", ""),
                    "arguments": "",
                }
            else:
                # Accumulate argument fragments
                fn = tc_delta.get("function", {})
                if "arguments" in fn:
                    tool_accum[idx]["arguments"] += fn["arguments"]
                if tc_delta.get("id"):
                    tool_accum[idx]["id"] = tc_delta["id"]
                if fn.get("name"):
                    tool_accum[idx]["name"] = fn["name"]

        # Emit completed tool calls when finish_reason = tool_calls
        finish = choices[0].get("finish_reason")
        if finish == "tool_calls":
            for idx in sorted(tool_accum.keys()):
                tc = tool_accum[idx]
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}
                yield StreamChunk(
                    type="tool_call",
                    data=ToolCall(
                        id=tc["id"],
                        name=tc["name"],
                        arguments=args,
                    ),
                )
            tool_accum.clear()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


def create_provider(
    provider_name: str,
    api_key: str,
    model: Optional[str] = None,
    **kwargs,
) -> LLMProvider:
    """Factory: 'anthropic' -> AnthropicProvider, 'openai' -> OpenAIProvider."""
    if provider_name not in _PROVIDERS:
        raise ValueError(
            f"Unknown provider: {provider_name}. "
            f"Choose from: {sorted(_PROVIDERS.keys())}"
        )
    return _PROVIDERS[provider_name](api_key=api_key, model=model, **kwargs)
