"""Tests for utils/llm_provider.py — all HTTP is mocked, no real API calls."""

import json
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from agent.llm_provider import (
    AnthropicProvider,
    AuthenticationError,
    LLMProvider,
    ModelResponse,
    OpenAIProvider,
    ProviderError,
    RateLimitError,
    RateLimiter,
    StreamChunk,
    ToolCall,
    create_provider,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class MockHTTPResponse:
    """Fake httpx.Response for non-streaming calls."""

    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or json.dumps(self._json)

    def json(self):
        return self._json


class MockStreamResponse:
    """Fake streaming response that yields SSE lines."""

    def __init__(self, lines, status_code=200, text=""):
        self.lines = lines
        self.status_code = status_code
        self.text = text

    def iter_lines(self):
        yield from self.lines

    def read(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@contextmanager
def mock_stream_context(lines, status_code=200, text=""):
    """Context manager that mimics httpx client.stream()."""
    yield MockStreamResponse(lines, status_code=status_code, text=text)


# ---------------------------------------------------------------------------
# RateLimiter tests
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_the_limit_is_exact(self):
        rl = RateLimiter(max_per_minute=5)
        for _ in range(5):
            rl.check()          # the 1st to 5th call are allowed
            rl.record()
        with pytest.raises(RateLimitError, match=r"Rate limit exceeded \(5/min\)"):
            rl.check()          # the 6th is not

    def test_the_wait_is_until_the_oldest_call_is_a_minute_old(self):
        rl = RateLimiter(max_per_minute=2)
        rl._calls = [time.monotonic() - 45, time.monotonic() - 1]
        with pytest.raises(RateLimitError) as exc_info:
            rl.check()
        assert 14 <= exc_info.value.retry_after <= 15

    def test_calls_older_than_a_minute_no_longer_count(self):
        rl = RateLimiter(max_per_minute=1)
        rl._calls.append(time.monotonic() - 61)
        rl.check()
        rl.record()
        assert len(rl._calls) == 1, 'the old call was dropped, the new one recorded'
        with pytest.raises(RateLimitError):
            rl.check()

    def test_a_refused_call_is_not_counted(self):
        provider = AnthropicProvider(api_key="key", max_calls_per_minute=1)
        provider._check_rate_limit()
        for _ in range(3):
            with pytest.raises(RateLimitError):
                provider._check_rate_limit()
        assert len(provider._rate_limiter._calls) == 1


# ---------------------------------------------------------------------------
# AnthropicProvider tests
# ---------------------------------------------------------------------------


class TestAnthropicChat:
    def _make_provider(self):
        return AnthropicProvider(api_key="test-key", max_calls_per_minute=100)

    def test_text_response(self):
        provider = self._make_provider()
        mock_resp = MockHTTPResponse(json_data={
            "content": [{"type": "text", "text": "Hello world"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": "end_turn",
        })

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            result = provider.chat([{"role": "user", "content": "hi"}])

        assert isinstance(result, ModelResponse)
        assert result.text == "Hello world"
        assert result.tool_calls == []
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert result.stop_reason == "end_turn"

    def test_tool_use_response(self):
        provider = self._make_provider()
        mock_resp = MockHTTPResponse(json_data={
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "get_status",
                    "input": {"section": "System"},
                },
            ],
            "usage": {"input_tokens": 20, "output_tokens": 15},
            "stop_reason": "tool_use",
        })

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            result = provider.chat([{"role": "user", "content": "status?"}])

        assert result.text == "Let me check."
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert isinstance(tc, ToolCall)
        assert tc.id == "call_1"
        assert tc.name == "get_status"
        assert tc.arguments == {"section": "System"}
        assert result.stop_reason == "tool_use"

    def test_system_and_tools_in_request(self):
        provider = self._make_provider()
        mock_resp = MockHTTPResponse(json_data={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 5, "output_tokens": 1},
            "stop_reason": "end_turn",
        })

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_post = MagicMock(return_value=mock_resp)
            MockClient.return_value.post = mock_post

            provider.chat(
                [{"role": "user", "content": "hi"}],
                tools=[{"name": "t1", "description": "d", "input_schema": {}}],
                system="You are helpful.",
            )

            body = mock_post.call_args[1]["json"]
            assert body["system"] == "You are helpful."
            assert body["tools"] == [{"name": "t1", "description": "d", "input_schema": {}}]
            assert "stream" not in body


class TestAnthropicStreamChat:
    def _make_provider(self):
        return AnthropicProvider(api_key="test-key", max_calls_per_minute=100)

    def test_text_stream(self):
        provider = self._make_provider()
        sse_lines = [
            'data: {"type": "message_start", "message": {"usage": {"input_tokens": 8}}}',
            'data: {"type": "content_block_start", "content_block": {"type": "text"}}',
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}',
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " world"}}',
            'data: {"type": "content_block_stop"}',
            'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}',
        ]

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.stream = MagicMock(
                return_value=mock_stream_context(sse_lines)
            )

            chunks = list(provider.stream_chat([{"role": "user", "content": "hi"}]))

        text_chunks = [c for c in chunks if c.type == "text_delta"]
        assert len(text_chunks) == 2
        assert text_chunks[0].data == "Hello"
        assert text_chunks[1].data == " world"

        assert [c.data for c in chunks if c.type == "usage"] == [
            {"input_tokens": 8}, {"output_tokens": 4}]

    def test_tool_use_stream(self):
        provider = self._make_provider()
        sse_lines = [
            'data: {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}',
            'data: {"type": "content_block_start", "content_block": {"type": "tool_use", "id": "tc_1", "name": "get_status"}}',
            'data: {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{\\"section\\""}}',
            'data: {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": ": \\"System\\"}"}}',
            'data: {"type": "content_block_stop"}',
            'data: {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}}',
        ]

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.stream = MagicMock(
                return_value=mock_stream_context(sse_lines)
            )

            chunks = list(provider.stream_chat([{"role": "user", "content": "status?"}]))

        tool_chunks = [c for c in chunks if c.type == "tool_call"]
        assert len(tool_chunks) == 1
        tc = tool_chunks[0].data
        assert isinstance(tc, ToolCall)
        assert tc.id == "tc_1"
        assert tc.name == "get_status"
        assert tc.arguments == {"section": "System"}


class TestAnthropicValidateKey:
    def test_valid_key(self):
        provider = AnthropicProvider(api_key="good-key", max_calls_per_minute=100)
        mock_resp = MockHTTPResponse(status_code=200, json_data={"content": []})

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            assert provider.validate_key() is True

    def test_invalid_key(self):
        provider = AnthropicProvider(api_key="bad-key", max_calls_per_minute=100)
        mock_resp = MockHTTPResponse(status_code=401, text="Unauthorized")

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            assert provider.validate_key() is False

    def test_network_error(self):
        provider = AnthropicProvider(api_key="key", max_calls_per_minute=100)

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(side_effect=Exception("conn refused"))

            assert provider.validate_key() is False


# ---------------------------------------------------------------------------
# OpenAIProvider tests
# ---------------------------------------------------------------------------


class TestOpenAIChat:
    def _make_provider(self):
        return OpenAIProvider(api_key="test-key", max_calls_per_minute=100)

    def test_text_response(self):
        provider = self._make_provider()
        mock_resp = MockHTTPResponse(json_data={
            "choices": [{
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3},
        })

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            result = provider.chat([{"role": "user", "content": "hi"}])

        assert isinstance(result, ModelResponse)
        assert result.text == "Hello!"
        assert result.tool_calls == []
        assert result.input_tokens == 8
        assert result.output_tokens == 3
        assert result.stop_reason == "end_turn"  # normalised from "stop"

    def test_tool_calls_response(self):
        provider = self._make_provider()
        mock_resp = MockHTTPResponse(json_data={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "get_config",
                            "arguments": '{"section": "Camera"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8},
        })

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            result = provider.chat([{"role": "user", "content": "config?"}])

        assert result.text == ""
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc.id == "call_abc"
        assert tc.name == "get_config"
        assert tc.arguments == {"section": "Camera"}
        assert result.stop_reason == "tool_use"  # normalised from "tool_calls"

    def test_system_prepended_to_messages(self):
        provider = self._make_provider()
        mock_resp = MockHTTPResponse(json_data={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        })

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_post = MagicMock(return_value=mock_resp)
            MockClient.return_value.post = mock_post

            provider.chat(
                [{"role": "user", "content": "hi"}],
                system="Be brief.",
            )

            body = mock_post.call_args[1]["json"]
            assert body["messages"][0] == {"role": "system", "content": "Be brief."}
            assert body["messages"][1] == {"role": "user", "content": "hi"}


class TestOpenAIStreamChat:
    def _make_provider(self):
        return OpenAIProvider(api_key="test-key", max_calls_per_minute=100)

    def test_text_stream(self):
        provider = self._make_provider()
        sse_lines = [
            'data: {"choices": [{"delta": {"content": "Hi"}, "finish_reason": null}]}',
            'data: {"choices": [{"delta": {"content": " there"}, "finish_reason": null}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}',
            'data: {"usage": {"prompt_tokens": 5, "completion_tokens": 3}}',
            'data: [DONE]',
        ]

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.stream = MagicMock(
                return_value=mock_stream_context(sse_lines)
            )

            chunks = list(provider.stream_chat([{"role": "user", "content": "hi"}]))

        text_chunks = [c for c in chunks if c.type == "text_delta"]
        assert len(text_chunks) == 2
        assert text_chunks[0].data == "Hi"
        assert text_chunks[1].data == " there"

        usage_chunks = [c for c in chunks if c.type == "usage"]
        assert len(usage_chunks) == 1
        assert usage_chunks[0].data["input_tokens"] == 5
        assert usage_chunks[0].data["output_tokens"] == 3

    def test_tool_call_stream(self):
        provider = self._make_provider()
        sse_lines = [
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "get_status", "arguments": ""}}]}, "finish_reason": null}]}',
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\\"section\\""}}]}, "finish_reason": null}]}',
            'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": \\"System\\"}"}}]}, "finish_reason": null}]}',
            'data: {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}',
            'data: [DONE]',
        ]

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.stream = MagicMock(
                return_value=mock_stream_context(sse_lines)
            )

            chunks = list(provider.stream_chat([{"role": "user", "content": "status?"}]))

        tool_chunks = [c for c in chunks if c.type == "tool_call"]
        assert len(tool_chunks) == 1
        tc = tool_chunks[0].data
        assert tc.id == "call_1"
        assert tc.name == "get_status"
        assert tc.arguments == {"section": "System"}


class TestOpenAIValidateKey:
    def test_valid_key(self):
        provider = OpenAIProvider(api_key="good-key", max_calls_per_minute=100)
        mock_resp = MockHTTPResponse(status_code=200, json_data={"choices": []})

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            assert provider.validate_key() is True

    def test_invalid_key(self):
        provider = OpenAIProvider(api_key="bad-key", max_calls_per_minute=100)
        mock_resp = MockHTTPResponse(status_code=401, text="Unauthorized")

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            assert provider.validate_key() is False


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestCreateProvider:
    def test_anthropic(self):
        p = create_provider("anthropic", api_key="key")
        assert isinstance(p, AnthropicProvider)
        assert p.model == AnthropicProvider.DEFAULT_MODEL

    def test_openai(self):
        p = create_provider("openai", api_key="key")
        assert isinstance(p, OpenAIProvider)
        assert p.model == OpenAIProvider.DEFAULT_MODEL

    def test_custom_model(self):
        p = create_provider("anthropic", api_key="key", model="claude-haiku-4-20250514")
        assert p.model == "claude-haiku-4-20250514"

    def test_unknown_provider(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("gemini", api_key="key")

    def test_kwargs_forwarded(self):
        p = create_provider("openai", api_key="key", timeout=120, max_calls_per_minute=5)
        assert p.timeout == 120
        assert p._rate_limiter.max_per_minute == 5


# ---------------------------------------------------------------------------
# Rate limiting integration
# ---------------------------------------------------------------------------


class TestRateLimitIntegration:
    def test_chat_blocked_when_limit_exceeded(self):
        provider = AnthropicProvider(api_key="key", max_calls_per_minute=1)
        mock_resp = MockHTTPResponse(json_data={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        })

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            # First call succeeds
            provider.chat([{"role": "user", "content": "hi"}])

            # Second call blocked
            with pytest.raises(RateLimitError, match="Rate limit exceeded"):
                provider.chat([{"role": "user", "content": "hi again"}])


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_401_raises_authentication_error(self):
        provider = AnthropicProvider(api_key="bad", max_calls_per_minute=100)
        mock_resp = MockHTTPResponse(status_code=401, text="Unauthorized")

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            with pytest.raises(AuthenticationError, match="HTTP 401"):
                provider.chat([{"role": "user", "content": "hi"}])

    def test_429_raises_rate_limit_error(self):
        provider = OpenAIProvider(api_key="key", max_calls_per_minute=100)
        mock_resp = MockHTTPResponse(status_code=429, text="Too Many Requests")

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            with pytest.raises(RateLimitError, match="HTTP 429"):
                provider.chat([{"role": "user", "content": "hi"}])

    def test_500_raises_provider_error(self):
        provider = AnthropicProvider(api_key="key", max_calls_per_minute=100)
        mock_resp = MockHTTPResponse(status_code=500, text="Internal Server Error")

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(return_value=mock_resp)

            with pytest.raises(ProviderError, match="HTTP 500"):
                provider.chat([{"role": "user", "content": "hi"}])

    def test_timeout_raises_provider_error(self):
        import httpx as httpx_mod
        provider = OpenAIProvider(api_key="key", timeout=1, max_calls_per_minute=100)

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.post = MagicMock(
                side_effect=httpx_mod.TimeoutException("timed out")
            )

            with pytest.raises(ProviderError, match="timed out"):
                provider.chat([{"role": "user", "content": "hi"}])

    def test_stream_error_status(self):
        provider = AnthropicProvider(api_key="bad", max_calls_per_minute=100)

        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__ = lambda s: s
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            MockClient.return_value.stream = MagicMock(
                return_value=mock_stream_context([], status_code=401, text="Unauthorized")
            )

            with pytest.raises(AuthenticationError):
                list(provider.stream_chat([{"role": "user", "content": "hi"}]))


# ---------------------------------------------------------------------------
# Key check: only a 401 means "wrong key"
# ---------------------------------------------------------------------------


class TestValidateKeyReasons:
    """A 400 (no credit) or 429 used to come back as False and the farmer was
    told the key was invalid. Both providers, through the shared HTTP fake."""

    @pytest.mark.parametrize("kind", [AnthropicProvider, OpenAIProvider])
    @pytest.mark.parametrize("status, raised", [
        (400, ProviderError), (403, ProviderError), (429, RateLimitError), (500, ProviderError)])
    def test_other_failures_raise_with_the_providers_text(self, llm, kind, status, raised):
        from agent_fakes import HTTPError
        llm.key_check = HTTPError(status, "credit balance too low")
        with pytest.raises(raised, match="credit balance too low") as exc_info:
            kind(api_key="key").validate_key()
        assert type(exc_info.value) is raised

    @pytest.mark.parametrize("kind", [AnthropicProvider, OpenAIProvider])
    def test_the_check_costs_one_token_at_most(self, llm, kind):
        assert kind(api_key="key").validate_key() is True
        [request] = llm.requests
        assert request["max_tokens"] == 1


# ---------------------------------------------------------------------------
# Tool calls in an Anthropic stream
# ---------------------------------------------------------------------------


class TestAnthropicToolStreams:
    def _chunks(self, llm, lines):
        llm.replies = [lines]
        return list(AnthropicProvider(api_key="key").stream_chat([{"role": "user", "content": "x"}]))

    def test_two_tool_calls_after_text_each_keep_their_own_arguments(self, llm):
        from agent_fakes import calls, says
        lines = says("On it.") + calls(("set_sensitivity", {"level": "low"}),
                                       ("set_detection", {"state": "off"}))
        chunks = self._chunks(llm, lines)
        assert [(c.type, c.data) for c in chunks] == [
            ("text_delta", "On it."),
            ("tool_call", ToolCall(id="tc_1", name="set_sensitivity", arguments={"level": "low"})),
            ("tool_call", ToolCall(id="tc_2", name="set_detection", arguments={"state": "off"})),
        ]

    def test_arguments_split_over_many_events_are_joined(self, llm):
        from agent_fakes import calls
        lines = calls(("set_detection", {"state": "blanket"}))
        whole = json.loads(lines[1][len("data: "):])["delta"]["partial_json"]
        pieces = [whole[:3], whole[3:11], whole[11:]]
        lines[1:2] = ["data: " + json.dumps({"type": "content_block_delta", "delta": {
            "type": "input_json_delta", "partial_json": piece}}) for piece in pieces]
        [chunk] = self._chunks(llm, lines)
        assert chunk.data.arguments == {"state": "blanket"}

    def test_arguments_that_are_not_json_become_no_arguments(self, llm):
        """What the code does. The tools must therefore treat 'no arguments'
        as a refusal, never as a default (see set_detection)."""
        from agent_fakes import calls
        lines = calls(("set_detection", {"state": "blanket"}))
        lines[1] = lines[1].replace('\\"blanket\\"}', "")
        [chunk] = self._chunks(llm, lines)
        assert (chunk.data.name, chunk.data.arguments) == ("set_detection", {})

    def test_noise_between_events_is_ignored(self, llm):
        from agent_fakes import says
        lines = ["event: ping", "", "data: ", "data: {not json", ": keep-alive"] + says("Hello")
        assert [(c.type, c.data) for c in self._chunks(llm, lines)] == [("text_delta", "Hello")]
