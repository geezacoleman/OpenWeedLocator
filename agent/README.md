# OWL Agent Runtime

Optional AI chat assistant for the OWL dashboard. Allows users to adjust
detection settings, create custom algorithms, manage widgets, and more through
natural language conversation.

## Structure

```
agent/
  __init__.py        # Package re-exports
  engine.py          # Agentic loop: prompt assembly, LLM streaming, tool execution
  llm_provider.py    # HTTP adapter for Anthropic and OpenAI APIs (requires httpx)
  tool_registry.py   # Decorator-based tool system with tier permissions
  widget_manager.py  # Dashboard widget discovery, validation, and rendering
  sessions/          # Chat session history (git-ignored)
  widgets/           # User-installed dashboard widgets (git-ignored)
```

## Dependencies

The agent requires `httpx` for LLM API calls. This is an **optional** dependency
not included in the base `requirements.txt`.

Install manually:
```bash
pip install httpx
```

Or select "Install AI agent support" during in-cab controller setup.

## Usage

Both controllers import the agent with a `try/except` guard. If `httpx` is not
installed, the agent tab is simply disabled -- the rest of the dashboard works
normally.

```python
from agent import AgentEngine, ToolRegistry, WidgetManager
```

## How it works

1. The **ToolRegistry** discovers `@owl_tool` decorated functions and exposes
   them as JSON schemas to the LLM.
2. The **AgentEngine** assembles a system prompt with current OWL state, streams
   the user's message to an LLM provider, executes any tool calls, and loops
   until the model returns a text response.
3. Tools are organised into tiers: `observe` (read-only), `apply` (change
   settings), and `developer` (file editing, git operations).

## Related

- `custom_algorithms/` (repo root) -- user-created detection algorithms. Shared
  with the core detection pipeline (`greenonbrown.py`), so it lives outside this
  package.
- `controller/shared/js/agent.js` -- frontend chat UI
- `controller/shared/css/_agent.css` -- frontend chat styles
