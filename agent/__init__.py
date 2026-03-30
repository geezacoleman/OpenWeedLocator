"""
OWL Agent Runtime package.

Groups the agent engine, LLM provider abstraction, tool registry, and widget
manager into a single importable package.
"""

from agent.engine import AgentEngine
from agent.tool_registry import ToolRegistry
from agent.widget_manager import WidgetManager
