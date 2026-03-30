"""
Agent engine for OWL Agent Runtime.

Orchestrates the agentic loop: assembles system prompt with current OWL state
and tool schemas, streams to an LLM provider, executes tool calls via the tool
registry, feeds results back, and repeats until the model returns a text
response or the iteration limit is reached.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from agent.llm_provider import (
    LLMProvider,
    StreamChunk,
    ToolCall,
    create_provider,
    ProviderError,
    AuthenticationError,
    RateLimitError,
)
from agent.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_ITERATIONS = 10
MAX_HISTORY = 50

# Static portion of the system prompt
SYSTEM_PROMPT_STATIC = """You are OWL Assistant, a helpful AI agent built into the OpenWeedLocator dashboard.

OWL is a camera-based weed detection system for precision agriculture. It runs on Raspberry Pi, uses computer vision to detect weeds, and triggers relay-controlled solenoids for spot spraying.

Detection algorithms:
- exg: Excess Green (2g - r - b) — default, works in most conditions
- maxg: Max Green (24g - 19r - 2b) — more aggressive green detection
- exhsv: ExG + HSV combined — best all-round algorithm
- hsv: Hue/Saturation/Value thresholding
- gndvi: Green Normalized Difference Vegetation Index
- gog: Green-on-Green — uses Ultralytics YOLO model (NCNN/PyTorch) for in-crop detection
- gog-hybrid: Combines GoG model with colour thresholding

Key parameters (GreenOnBrown):
- exg_min/exg_max: Excess green thresholds (0-255)
- hue_min/hue_max: Hue thresholds (0-179, OpenCV HSV range)
- saturation_min/saturation_max: Saturation thresholds (0-255)
- brightness_min/brightness_max: Brightness thresholds (0-255)
- min_detection_area: Minimum blob size in pixels

Detection modes: 0=spot spray (targeted), 1=off, 2=blanket (all nozzles)

Sensitivity presets store 9 GreenOnBrown thresholds. Built-in presets: low, medium, high. Users can save custom presets.

You can inspect system state, change settings, manage presets, and create UI widgets. Always explain what you're doing in plain language. Keep responses concise and farmer-friendly.

Protected sections (Relays, MQTT, Network, WebDashboard) cannot be modified — these are hardware and network settings that require manual configuration.

When changing detection thresholds (hue_min, exg_min, saturation_min, etc.), use set_config_param with section='GreenOnBrown'. The system auto-normalizes threshold keys to GreenOnBrown regardless of the section you specify, but always use the correct section for clarity. You can read current threshold values with get_config(section='GreenOnBrown') or get_system_status().

When changing settings, always confirm the current value first by reading it, then make the change and verify.

Custom algorithms:
You can create custom detection algorithms. Use list_custom_algorithms to see existing ones.
When creating algorithms, follow this pattern:
- Function signature: def my_algo(image, params) — image is BGR numpy array (uint8, HxWx3), params is a dict of current threshold values
- params dict contains: exg_min, exg_max, hue_min, hue_max, saturation_min, saturation_max, brightness_min, brightness_max, min_detection_area, invert_hue
- Returns grayscale image (uint8, HxW, values 0-255) — gets thresholded automatically by the system using exg_min/exg_max
- OR returns (binary_image, True) if you do your own thresholding — WARNING: this bypasses the system's exg_min/exg_max clip entirely
- Only import cv2, numpy, math
- Use existing algorithms (exg, maxg, exhsv) as reference
- After creating, use run_algorithm_test to verify it works
- Use deploy_algorithm to send it to connected OWL devices
- Use set_algorithm to switch detection to the custom algorithm

CRITICAL — Sensitivity and custom algorithms:
- The sensitivity system (low/medium/high presets) works by changing threshold values (exg_min, hue_min, etc.)
- These values flow into the params dict that your algorithm receives. If your algorithm reads from params, sensitivity changes WILL affect detection.
- NEVER hardcode threshold values. Always read from params: use params['hue_min'] instead of hardcoding 30, params['saturation_min'] instead of 150.
- If your algorithm returns (binary, True), the system's exg_min/exg_max post-processing is skipped. You MUST use params thresholds inside your algorithm code instead.
- You CAN create custom presets with values tuned for your algorithm (e.g., hue 5-22 for orange instead of 30-90 for green). These flow through the standard sensitivity pipeline into params.
- Do NOT create widgets that try to control the algorithm outside of the sensitivity/preset system. The preset system is the correct way to offer tuneable sensitivity for custom algorithms.

When the user sends an image, analyze it carefully. You can see:
- Weed species and growth stages
- Soil conditions and crop rows
- Lighting conditions that affect detection
- Green-on-brown contrast levels

If asked to create a detection algorithm from an image, examine the colour channels (BGR), hue/saturation patterns, and contrast between target plants and background. Use this to set appropriate thresholds or write custom algorithm code.
"""


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """Per-conversation state."""
    messages: List[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    created: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Agent engine
# ---------------------------------------------------------------------------

class AgentEngine:
    """Orchestrator for the OWL agent loop."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        provider: Optional[LLMProvider] = None,
        context: Optional[Dict[str, Any]] = None,
        sessions_dir: Optional[str] = None,
    ):
        self.tool_registry = tool_registry
        self.provider = provider
        self.context = context or {}
        self._sessions: Dict[str, Session] = {}
        self.sessions_dir: Optional[Path] = None
        if sessions_dir:
            self.sessions_dir = Path(sessions_dir)
            self.sessions_dir.mkdir(parents=True, exist_ok=True)

    # -- provider management ------------------------------------------------

    def set_provider(
        self,
        api_key: str,
        provider_name: str = "anthropic",
        model: Optional[str] = None,
        **kwargs,
    ) -> bool:
        """Configure the LLM provider. Returns True on successful validation."""
        try:
            self.provider = create_provider(
                provider_name, api_key, model=model, **kwargs
            )
            valid = self.provider.validate_key()
            if not valid:
                self.provider = None
                return False
            logger.info(f"Agent provider set: {provider_name} ({self.provider.model})")
            return True
        except Exception as e:
            logger.error(f"Failed to set provider: {e}")
            self.provider = None
            raise

    @property
    def connected(self) -> bool:
        return self.provider is not None

    # -- session management -------------------------------------------------

    def _get_session(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            self._sessions[session_id] = Session()
        return self._sessions[session_id]

    def _trim_history(self, session: Session):
        """Keep only the last MAX_HISTORY messages."""
        if len(session.messages) > MAX_HISTORY:
            session.messages = session.messages[-MAX_HISTORY:]

    def get_session_info(self, session_id: str) -> dict:
        """Return token counts and message count for a session."""
        session = self._get_session(session_id)
        return {
            'input_tokens': session.input_tokens,
            'output_tokens': session.output_tokens,
            'message_count': len(session.messages),
        }

    # -- session persistence ------------------------------------------------

    def _session_title(self, session: Session) -> str:
        """Extract title from first user message."""
        for msg in session.messages:
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                if isinstance(content, str):
                    return content[:80]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'text':
                            return block['text'][:80]
        return 'New conversation'

    def _save_session(self, session_id: str):
        """Persist session to disk as JSON."""
        if not self.sessions_dir:
            return
        session = self._sessions.get(session_id)
        if not session or not session.messages:
            return
        data = {
            'id': session_id,
            'title': self._session_title(session),
            'created': session.created,
            'updated': time.time(),
            'input_tokens': session.input_tokens,
            'output_tokens': session.output_tokens,
            'message_count': len(session.messages),
            'messages': session.messages,
        }
        filepath = self.sessions_dir / f"{session_id}.json"
        tmp = filepath.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(str(tmp), str(filepath))

    def list_sessions(self) -> list:
        """Return metadata for all saved sessions, newest first."""
        if not self.sessions_dir:
            return []
        sessions = []
        for filepath in self.sessions_dir.glob('session_*.json'):
            try:
                with open(filepath) as f:
                    data = json.load(f)
                sessions.append({
                    'id': data['id'],
                    'title': data.get('title', 'Untitled'),
                    'created': data.get('created', 0),
                    'updated': data.get('updated', 0),
                    'input_tokens': data.get('input_tokens', 0),
                    'output_tokens': data.get('output_tokens', 0),
                    'message_count': data.get('message_count', 0),
                })
            except Exception as e:
                logger.warning(f"Failed to read session {filepath.name}: {e}")
        sessions.sort(key=lambda s: s.get('updated', 0), reverse=True)
        return sessions

    def load_session(self, session_id: str) -> Optional[dict]:
        """Load a session from disk into memory and return full data."""
        if not self.sessions_dir:
            return None
        filepath = self.sessions_dir / f"{session_id}.json"
        if not filepath.exists():
            return None
        try:
            with open(filepath) as f:
                data = json.load(f)
            session = Session(
                messages=data.get('messages', []),
                input_tokens=data.get('input_tokens', 0),
                output_tokens=data.get('output_tokens', 0),
                created=data.get('created', time.time()),
            )
            self._sessions[session_id] = session
            return data
        except Exception as e:
            logger.error(f"Failed to load session {session_id}: {e}")
            return None

    def delete_session(self, session_id: str) -> bool:
        """Delete a session from disk and memory."""
        self._sessions.pop(session_id, None)
        if not self.sessions_dir:
            return False
        filepath = self.sessions_dir / f"{session_id}.json"
        if filepath.exists():
            filepath.unlink()
            return True
        return False

    # -- system prompt assembly ---------------------------------------------

    def _build_system_prompt(self) -> str:
        """Assemble the full system prompt with dynamic state."""
        parts = [SYSTEM_PROMPT_STATIC.strip()]

        # Inject current state if available
        mqtt_client = self.context.get('mqtt_client')
        if mqtt_client is not None:
            state = getattr(mqtt_client, 'current_state', {})
            if state:
                parts.append("\nCurrent OWL state:")
                for key in (
                    'detection_enable', 'algorithm', 'detection_mode',
                    'sensitivity_level', 'exg_min', 'exg_max',
                    'hue_min', 'hue_max', 'saturation_min', 'saturation_max',
                    'brightness_min', 'brightness_max', 'min_detection_area',
                    'cpu_percent', 'memory_percent', 'cpu_temp',
                    'tracking_enabled', 'model_available', 'current_model',
                ):
                    if key in state:
                        parts.append(f"  {key}: {state[key]}")

        return "\n".join(parts)

    # -- main chat loop -----------------------------------------------------

    def chat(
        self, session_id: str, message
    ) -> Generator[StreamChunk, None, None]:
        """Run the agent loop for a single user message.

        message can be a string or an Anthropic-format content array
        (list of dicts with type 'text' and/or 'image' blocks).

        Yields StreamChunk objects: text deltas, tool call results, usage
        updates, and errors.
        """
        if self.provider is None:
            yield StreamChunk(type="error", data="No provider configured. Please connect first.")
            return

        session = self._get_session(session_id)
        fmt = self._schema_format()

        # Add user message
        session.messages.append({"role": "user", "content": message})
        self._trim_history(session)

        system_prompt = self._build_system_prompt()
        tools = self.tool_registry.get_schemas(format=fmt)

        iteration = 0
        while iteration < MAX_ITERATIONS:
            iteration += 1

            try:
                # Collect the full response from streaming
                text_parts = []
                tool_calls = []

                for chunk in self.provider.stream_chat(
                    messages=self._translate_images(session.messages),
                    tools=tools if tools else None,
                    system=system_prompt,
                ):
                    if chunk.type == "text_delta":
                        text_parts.append(chunk.data)
                        yield chunk
                    elif chunk.type == "tool_call":
                        tool_calls.append(chunk.data)
                    elif chunk.type == "usage":
                        self._update_tokens(session, chunk.data)
                        yield chunk
                    elif chunk.type == "error":
                        yield chunk

                # If no tool calls, we're done — model gave a text response
                if not tool_calls:
                    final_text = "".join(text_parts)
                    if final_text:
                        session.messages.append({
                            "role": "assistant",
                            "content": final_text,
                        })
                    self._save_session(session_id)
                    return

                # Build assistant message with tool use (format-specific)
                self._append_assistant_tool_use(
                    session, text_parts, tool_calls, fmt
                )

                # Execute tool calls and append results
                for tc in tool_calls:
                    result = self._execute_tool(tc)
                    yield StreamChunk(
                        type="tool_result",
                        data={
                            "tool_name": tc.name,
                            "tool_id": tc.id,
                            "result": result,
                        },
                    )
                    self._append_tool_result(
                        session, tc, result, fmt
                    )

            except RateLimitError as e:
                yield StreamChunk(
                    type="error",
                    data=f"Rate limit exceeded. Try again in {e.retry_after or 30:.0f} seconds.",
                )
                return
            except AuthenticationError as e:
                yield StreamChunk(type="error", data=f"Authentication failed: {e}")
                return
            except ProviderError as e:
                yield StreamChunk(type="error", data=f"Provider error: {e}")
                return
            except Exception as e:
                logger.exception("Unexpected error in agent loop")
                yield StreamChunk(type="error", data=f"Unexpected error: {e}")
                return

        # Iteration limit reached
        self._save_session(session_id)
        yield StreamChunk(
            type="error",
            data="Reached maximum tool iterations. Please try a simpler request.",
        )

    # -- internal helpers ---------------------------------------------------

    def _schema_format(self) -> str:
        """Return 'anthropic' or 'openai' based on current provider."""
        if self.provider is None:
            return "anthropic"
        from agent.llm_provider import OpenAIProvider
        if isinstance(self.provider, OpenAIProvider):
            return "openai"
        return "anthropic"

    def _translate_images(self, messages):
        """Convert Anthropic image blocks to OpenAI format if needed.

        Internal storage uses Anthropic format. When using OpenAI, translate
        image blocks to data-URL image_url blocks before sending.
        """
        if self._schema_format() != "openai":
            return messages
        translated = []
        for msg in messages:
            content = msg.get('content')
            if not isinstance(content, list):
                translated.append(msg)
                continue
            new_content = []
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'image':
                    source = block.get('source', {})
                    media_type = source.get('media_type', 'image/jpeg')
                    data = source.get('data', '')
                    new_content.append({
                        'type': 'image_url',
                        'image_url': {'url': f'data:{media_type};base64,{data}'}
                    })
                else:
                    new_content.append(block)
            translated.append({**msg, 'content': new_content})
        return translated

    def _update_tokens(self, session: Session, usage: dict):
        """Accumulate token counts from a usage chunk."""
        session.input_tokens += usage.get("input_tokens", 0)
        session.output_tokens += usage.get("output_tokens", 0)

    def _append_assistant_tool_use(
        self,
        session: Session,
        text_parts: List[str],
        tool_calls: List[ToolCall],
        fmt: str,
    ):
        """Append the assistant's tool-use message in the correct format."""
        if fmt == "openai":
            tc_list = []
            for tc in tool_calls:
                args = tc.arguments
                if not isinstance(args, str):
                    args = json.dumps(args)
                tc_list.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": args,
                    },
                })
            msg = {"role": "assistant", "tool_calls": tc_list}
            text = "".join(text_parts)
            if text:
                msg["content"] = text
            session.messages.append(msg)
        else:
            # Anthropic format
            content = []
            text = "".join(text_parts)
            if text:
                content.append({"type": "text", "text": text})
            for tc in tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                })
            session.messages.append({"role": "assistant", "content": content})

    def _append_tool_result(
        self,
        session: Session,
        tool_call: ToolCall,
        result: dict,
        fmt: str,
    ):
        """Append a tool result message in the correct format."""
        result_str = json.dumps(result)
        if fmt == "openai":
            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_str,
            })
        else:
            # Anthropic format — tool results go in a user message
            session.messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": result_str,
                }],
            })

    def _execute_tool(self, tool_call: ToolCall) -> dict:
        """Execute a single tool call, returning the result dict."""
        try:
            result = self.tool_registry.call(
                name=tool_call.name,
                params=tool_call.arguments,
                context=self.context,
            )
            return result
        except (PermissionError, ValueError, TypeError) as e:
            return {"error": str(e)}
        except KeyError as e:
            return {"error": f"Unknown tool: {e}"}
        except Exception as e:
            logger.exception(f"Tool {tool_call.name} failed")
            return {"error": f"Tool execution failed: {e}"}

    def get_status(self) -> dict:
        """Return engine status for the /api/agent/status endpoint."""
        provider_name = None
        model_name = None
        if self.provider is not None:
            from agent.llm_provider import AnthropicProvider, OpenAIProvider
            if isinstance(self.provider, AnthropicProvider):
                provider_name = "anthropic"
            elif isinstance(self.provider, OpenAIProvider):
                provider_name = "openai"
            model_name = getattr(self.provider, 'model', None)

        return {
            'connected': self.connected,
            'provider': provider_name,
            'model': model_name,
        }
