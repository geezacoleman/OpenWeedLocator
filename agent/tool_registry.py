"""
Tool registry for OWL Agent Runtime.

Provides a decorator-based system for registering tools that an LLM agent can
call.  Each tool has a tier (observe / apply / developer), a JSON-describable
parameter schema, and a plain-Python implementation that receives an injected
``context`` dict at call time.

The registry validates inputs, enforces tier permissions, and generates tool
schemas in both Anthropic and OpenAI formats.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_TIERS = frozenset({'observe', 'apply', 'developer'})

VALID_ALGORITHMS = frozenset({
    'exg', 'exgr', 'maxg', 'nexg', 'exhsv', 'hsv', 'gndvi', 'gog', 'gog-hybrid',
})

PROTECTED_SECTIONS = frozenset({'Relays', 'MQTT', 'Network', 'WebDashboard'})

from utils.config_manager import GREENONBROWN_PARAMS

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolDef:
    """Metadata + callable for a single registered tool."""
    name: str
    tier: str
    description: str
    parameters: Dict[str, dict]
    func: Callable
    required: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def owl_tool(tier: str, description: str, parameters: Optional[Dict[str, dict]] = None):
    """Decorate a function as an OWL tool.

    Parameters
    ----------
    tier : str
        One of 'observe', 'apply', or 'developer'.
    description : str
        Human-readable description shown to the LLM.
    parameters : dict or None
        Mapping of param_name -> {"type": ..., "description": ..., "required": bool}.
    """
    if tier not in VALID_TIERS:
        raise ValueError(f"Invalid tier '{tier}'. Must be one of {sorted(VALID_TIERS)}")

    if parameters is None:
        parameters = {}

    def decorator(func):
        func._owl_tool_meta = {
            'tier': tier,
            'description': description,
            'parameters': parameters,
        }
        return func

    return decorator


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Discovers, stores, validates and invokes OWL tools."""

    def __init__(self, developer_mode: bool = False):
        self.developer_mode = developer_mode
        self._tools: Dict[str, ToolDef] = {}

    # -- registration -------------------------------------------------------

    def register(self, func: Callable) -> ToolDef:
        """Register a decorated function.  Returns the ToolDef."""
        meta = getattr(func, '_owl_tool_meta', None)
        if meta is None:
            raise ValueError(f"{func.__name__} is not decorated with @owl_tool")

        params = meta['parameters']
        required = [k for k, v in params.items() if v.get('required', False)]

        tool_def = ToolDef(
            name=func.__name__,
            tier=meta['tier'],
            description=meta['description'],
            parameters=params,
            func=func,
            required=required,
        )
        self._tools[tool_def.name] = tool_def
        return tool_def

    def discover(self) -> int:
        """Auto-register all @owl_tool decorated functions defined in this
        module.  Returns count of newly registered tools."""
        import agent.tool_registry as _self_module

        count = 0
        for attr_name in dir(_self_module):
            obj = getattr(_self_module, attr_name)
            if callable(obj) and hasattr(obj, '_owl_tool_meta'):
                if attr_name not in self._tools:
                    self.register(obj)
                    count += 1
        return count

    # -- invocation ---------------------------------------------------------

    def call(self, name: str, params: Dict[str, Any],
             context: Dict[str, Any]) -> Dict[str, Any]:
        """Validate params, check tier, execute tool, return result dict."""
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")

        tool = self._tools[name]

        # Tier enforcement
        if tool.tier == 'developer' and not self.developer_mode:
            raise PermissionError(
                f"Tool '{name}' requires developer mode"
            )

        # Required-param check
        for key in tool.required:
            if key not in params:
                raise TypeError(
                    f"Missing required parameter '{key}' for tool '{name}'"
                )

        # Execute
        result = tool.func(**params, **context)
        if not isinstance(result, dict):
            result = {'result': result}
        return result

    # -- schema export ------------------------------------------------------

    def get_schemas(self, format: str = "anthropic") -> List[dict]:
        """Return tool schemas in Anthropic or OpenAI format.

        Parameters
        ----------
        format : str
            ``"anthropic"`` or ``"openai"``.
        """
        schemas = []
        for tool in self._tools.values():
            if tool.tier == 'developer' and not self.developer_mode:
                continue
            if format == "anthropic":
                schemas.append(self._anthropic_schema(tool))
            elif format == "openai":
                schemas.append(self._openai_schema(tool))
            else:
                raise ValueError(f"Unknown schema format: {format}")
        return schemas

    def list_tools(self, include_developer: bool = False) -> List[dict]:
        """Return list of tool metadata dicts."""
        result = []
        for tool in self._tools.values():
            if tool.tier == 'developer' and not include_developer:
                continue
            result.append({
                'name': tool.name,
                'tier': tool.tier,
                'description': tool.description,
                'parameters': tool.parameters,
            })
        return result

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _build_json_properties(tool: ToolDef):
        """Convert tool parameters to JSON Schema properties + required."""
        properties = {}
        required = []
        for pname, pdef in tool.parameters.items():
            prop = {'type': pdef.get('type', 'string')}
            if 'description' in pdef:
                prop['description'] = pdef['description']
            if 'enum' in pdef:
                prop['enum'] = pdef['enum']
            properties[pname] = prop
            if pdef.get('required', False):
                required.append(pname)
        return properties, required

    @classmethod
    def _anthropic_schema(cls, tool: ToolDef) -> dict:
        properties, required = cls._build_json_properties(tool)
        return {
            'name': tool.name,
            'description': tool.description,
            'input_schema': {
                'type': 'object',
                'properties': properties,
                'required': required,
            },
        }

    @classmethod
    def _openai_schema(cls, tool: ToolDef) -> dict:
        properties, required = cls._build_json_properties(tool)
        return {
            'type': 'function',
            'function': {
                'name': tool.name,
                'description': tool.description,
                'parameters': {
                    'type': 'object',
                    'properties': properties,
                    'required': required,
                },
            },
        }


# =========================================================================
# Tool implementations
# =========================================================================

# ---------------------------------------------------------------------------
# Tier A — observe
# ---------------------------------------------------------------------------

@owl_tool(
    tier='observe',
    description='Get current system status including CPU, memory, detection state',
    parameters={
        'section': {
            'type': 'string',
            'description': 'Optional status section to filter',
            'required': False,
        },
    },
)
def get_system_status(section=None, **context):
    mqtt_client = context.get('mqtt_client')
    if mqtt_client is None:
        return {'error': 'No MQTT client available'}
    state = getattr(mqtt_client, 'current_state', {})
    if isinstance(state, dict):
        state = state.copy()
    else:
        state = {}

    # On the networked controller, also report connected OWL count
    owls_state = getattr(mqtt_client, 'owls_state', None)
    if owls_state is not None:
        connected = [oid for oid, s in owls_state.items() if s.get('connected')]
        state['connected_owls'] = connected
        state['owl_count'] = len(connected)
        # If the primary state is empty but we have OWL states, note that
        if not state.get('detection_enable') and connected:
            # Pull key fields from first connected OWL for visibility
            first = owls_state[connected[0]]
            for key in ('detection_enable', 'algorithm', 'sensitivity_level',
                        'detection_mode', 'image_sample_enable', 'tracking_enabled',
                        'exg_min', 'exg_max', 'hue_min', 'hue_max',
                        'saturation_min', 'saturation_max',
                        'brightness_min', 'brightness_max',
                        'min_detection_area', 'invert_hue'):
                if key in first and key not in state:
                    state[key] = first[key]

    if section:
        filtered = {k: v for k, v in state.items() if k.startswith(section)}
        return {'status': filtered}
    return {'status': state}


@owl_tool(
    tier='observe',
    description='Read configuration values. Returns full config, a section, or a single key.',
    parameters={
        'section': {
            'type': 'string',
            'description': 'Config section name',
            'required': False,
        },
        'key': {
            'type': 'string',
            'description': 'Config key within section',
            'required': False,
        },
    },
)
def get_config(section=None, key=None, **context):
    config = context.get('config')
    if config is None:
        return {'error': 'No config available'}

    # On the networked controller the local config only has infrastructure
    # sections (MQTT, GPS, Actuation). Detection config (GreenOnBrown, etc.)
    # lives on the OWL devices. Try the OWL's published config as fallback.
    mqtt_client = context.get('mqtt_client')
    owl_cfg = getattr(mqtt_client, 'owl_config', None) if mqtt_client else None

    def _get_section(sec_name):
        """Try local config first, then OWL device config, then OWL MQTT state."""
        if config.has_section(sec_name):
            return dict(config.items(sec_name))
        if owl_cfg and sec_name in owl_cfg:
            return dict(owl_cfg[sec_name]) if isinstance(owl_cfg[sec_name], dict) else None
        # Construct GreenOnBrown from OWL published state (threshold values)
        if sec_name == 'GreenOnBrown' and mqtt_client:
            owls_state = getattr(mqtt_client, 'owls_state', {})
            if owls_state:
                for s in owls_state.values():
                    if s.get('connected'):
                        vals = {k: str(s[k]) for k in GREENONBROWN_PARAMS if k in s}
                        if vals:
                            return vals
        return None

    if section and key:
        values = _get_section(section)
        if values is None:
            return {'error': f'Section [{section}] not found'}
        if key not in values:
            return {'error': f'Key "{key}" not found in [{section}]'}
        return {'section': section, 'key': key, 'value': values[key]}

    if section:
        values = _get_section(section)
        if values is None:
            return {'error': f'Section [{section}] not found'}
        return {'section': section, 'values': values}

    # Return all sections (local + OWL device)
    result = {}
    for sec in config.sections():
        result[sec] = dict(config.items(sec))
    if owl_cfg:
        for sec, vals in owl_cfg.items():
            if sec not in result and isinstance(vals, dict):
                result[sec] = dict(vals)
    return {'config': result}


@owl_tool(
    tier='observe',
    description='List available UI widgets',
    parameters={},
)
def list_widgets(**context):
    wm = context.get('widget_manager')
    if wm is None:
        return {'widgets': []}
    try:
        return {'widgets': wm.scan()}
    except Exception as e:
        logger.warning(f"widget_manager.scan() failed: {e}")
        return {'widgets': [], 'error': str(e)}


@owl_tool(
    tier='observe',
    description='List available sensitivity presets',
    parameters={},
)
def list_presets(**context):
    # Read from MQTT state — presets are synced as part of OWL state
    mqtt_client = context.get('mqtt_client')
    if mqtt_client is not None:
        state = getattr(mqtt_client, 'current_state', {})
        presets = state.get('sensitivity_presets')
        if presets is not None:
            return {'presets': presets}

        # On the networked controller, try reading preset sections from OWL config
        owl_cfg = getattr(mqtt_client, 'owl_config', None)
        if owl_cfg:
            found = []
            for sec_name in owl_cfg:
                if sec_name.startswith('Sensitivity_'):
                    preset_name = sec_name.replace('Sensitivity_', '', 1)
                    found.append(preset_name)
            if found:
                return {'presets': found}

    # Fallback: local config Sensitivity_* sections
    config = context.get('config')
    if config is not None:
        found = []
        for sec_name in config.sections():
            if sec_name.startswith('Sensitivity_'):
                found.append(sec_name.replace('Sensitivity_', '', 1))
        if found:
            return {'presets': found}

    # Fallback to sensitivity_manager if available (e.g. in tests)
    sm = context.get('sensitivity_manager')
    if sm is not None:
        try:
            return {'presets': sm.list_presets()}
        except Exception as e:
            logger.warning(f"sensitivity_manager.list_presets() failed: {e}")
            return {'presets': [], 'error': str(e)}
    return {'presets': []}


# ---------------------------------------------------------------------------
# Tier B — apply
# ---------------------------------------------------------------------------

@owl_tool(
    tier='apply',
    description='Set a configuration parameter. Protected sections (Relays, MQTT, Network, WebDashboard) are rejected.',
    parameters={
        'section': {
            'type': 'string',
            'description': 'Config section name',
            'required': True,
        },
        'key': {
            'type': 'string',
            'description': 'Config key to set',
            'required': True,
        },
        'value': {
            'type': 'string',
            'description': 'Value to set',
            'required': True,
        },
    },
)
def set_config_param(section, key, value, **context):
    if section in PROTECTED_SECTIONS:
        raise ValueError(
            f"Section [{section}] is protected and cannot be modified"
        )
    # Normalize: detection threshold keys always target GreenOnBrown
    if key in GREENONBROWN_PARAMS:
        section = 'GreenOnBrown'
    mqtt_client = context.get('mqtt_client')
    if mqtt_client is None:
        return {'error': 'No MQTT client available'}
    result = mqtt_client._send_command('set_config', section=section, key=key, value=value)
    return result


@owl_tool(
    tier='apply',
    description='Set the sensitivity level (e.g. low, medium, high, or custom preset name)',
    parameters={
        'level': {
            'type': 'string',
            'description': 'Preset name',
            'required': True,
        },
    },
)
def set_sensitivity(level, **context):
    mqtt_client = context.get('mqtt_client')
    if mqtt_client is None:
        return {'error': 'No MQTT client available'}
    return mqtt_client.set_sensitivity_level(level)


@owl_tool(
    tier='apply',
    description='Save current detection values as a new sensitivity preset',
    parameters={
        'name': {
            'type': 'string',
            'description': 'Name for the new preset',
            'required': True,
        },
    },
)
def create_preset(name, **context):
    # Use MQTT command — the OWL's MQTT handler reads current values from
    # the owl instance and saves via SensitivityManager
    mqtt_client = context.get('mqtt_client')
    if mqtt_client is None:
        return {'error': 'No MQTT client available'}
    return mqtt_client._send_command('save_sensitivity_preset', name=name)


@owl_tool(
    tier='apply',
    description='Set the detection algorithm. Use list_custom_algorithms to see custom options.',
    parameters={
        'algorithm': {
            'type': 'string',
            'description': 'Algorithm name (builtin or custom)',
            'required': True,
        },
    },
)
def set_algorithm(algorithm, **context):
    try:
        from utils.config_manager import ConfigValidator
        valid = ConfigValidator.get_valid_algorithms()
    except Exception:
        valid = VALID_ALGORITHMS
    if algorithm not in valid:
        raise ValueError(
            f"Invalid algorithm '{algorithm}'. Valid: {sorted(valid)}"
        )
    mqtt_client = context.get('mqtt_client')
    if mqtt_client is None:
        return {'error': 'No MQTT client available'}
    return mqtt_client._send_command('set_algorithm', value=algorithm)


@owl_tool(
    tier='apply',
    description='Enable or disable detection, optionally setting detection mode (0=spot spray, 1=off, 2=blanket)',
    parameters={
        'enabled': {
            'type': 'boolean',
            'description': 'Enable or disable detection',
            'required': True,
        },
        'mode': {
            'type': 'integer',
            'description': 'Detection mode: 0=spot spray, 1=off, 2=blanket',
            'required': False,
        },
    },
)
def set_detection(enabled, mode=None, **context):
    mqtt_client = context.get('mqtt_client')
    if mqtt_client is None:
        return {'error': 'No MQTT client available'}
    if mode is not None:
        if mode not in (0, 1, 2):
            raise ValueError(f"Invalid detection mode {mode}. Must be 0, 1, or 2.")
        return mqtt_client._send_command('set_detection_mode', value=int(mode))
    return mqtt_client.set_detection_enable(bool(enabled))


@owl_tool(
    tier='apply',
    description='Create and install a new UI widget from a JSON spec',
    parameters={
        'spec': {
            'type': 'object',
            'description': 'Widget specification JSON',
            'required': True,
        },
    },
)
def create_widget(spec, **context):
    wm = context.get('widget_manager')
    if wm is None:
        return {'error': 'No widget manager available'}
    try:
        widget_id = spec.get('id') if isinstance(spec, dict) else None
        if not widget_id:
            return {'success': False, 'error': 'Widget spec must include an "id" field'}
        ok, err = wm.install(widget_id, spec)
        if ok:
            return {'success': True, 'widget_id': widget_id}
        return {'success': False, 'error': err or 'Install failed'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@owl_tool(
    tier='apply',
    description='Remove a UI widget by ID',
    parameters={
        'widget_id': {
            'type': 'string',
            'description': 'Widget ID to remove',
            'required': True,
        },
    },
)
def remove_widget(widget_id, **context):
    wm = context.get('widget_manager')
    if wm is None:
        return {'error': 'No widget manager available'}
    try:
        ok, err = wm.remove(widget_id)
        if ok:
            return {'success': True, 'message': f'Widget "{widget_id}" removed'}
        return {'success': False, 'error': err or 'Remove failed'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@owl_tool(
    tier='apply',
    description='Update configuration of an existing UI widget',
    parameters={
        'widget_id': {
            'type': 'string',
            'description': 'Widget ID to update',
            'required': True,
        },
        'updates': {
            'type': 'object',
            'description': 'Dict of fields to update',
            'required': True,
        },
    },
)
def update_widget(widget_id, updates, **context):
    wm = context.get('widget_manager')
    if wm is None:
        return {'error': 'No widget manager available'}
    try:
        ok, err = wm.update(widget_id, updates)
        if ok:
            return {'success': True, 'widget_id': widget_id}
        return {'success': False, 'error': err or 'Update failed'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# Custom algorithm tools
# ---------------------------------------------------------------------------

@owl_tool(
    tier='observe',
    description='List custom detection algorithms. Returns name and description for each.',
    parameters={},
)
def list_custom_algorithms(**context):
    try:
        from custom_algorithms import list_algorithms
        algos = list_algorithms()
        return {'algorithms': algos}
    except Exception as e:
        return {'algorithms': [], 'error': str(e)}


@owl_tool(
    tier='apply',
    description='Create a custom detection algorithm. Code must define a function with "image" as first parameter that returns a grayscale uint8 array. Only cv2, numpy, and math imports allowed.',
    parameters={
        'name': {
            'type': 'string',
            'description': 'Algorithm name (lowercase, letters/numbers/underscores, max 31 chars)',
            'required': True,
        },
        'code': {
            'type': 'string',
            'description': 'Python source code for the algorithm',
            'required': True,
        },
        'description': {
            'type': 'string',
            'description': 'Short description of what the algorithm does',
            'required': False,
        },
    },
)
def create_algorithm(name, code, description='', **context):
    try:
        from custom_algorithms import save_algorithm
        return save_algorithm(name, code, description)
    except Exception as e:
        return {'success': False, 'error': str(e)}


@owl_tool(
    tier='apply',
    description='Test a custom algorithm on a synthetic green-on-brown image. Returns timing and detection count.',
    parameters={
        'name': {
            'type': 'string',
            'description': 'Algorithm name to test',
            'required': True,
        },
    },
)
def run_algorithm_test(name, **context):
    try:
        import numpy as np
        import cv2
        import time as _time
        from custom_algorithms import load_custom_algorithm

        func = load_custom_algorithm(name)
        if func is None:
            return {'success': False, 'error': f'Algorithm "{name}" not found or failed to load'}

        # Create synthetic 640x480 green-on-brown test image
        image = np.full((480, 640, 3), (60, 80, 120), dtype=np.uint8)  # brown background (BGR)
        # Add green rectangles (simulated weeds)
        for y, x in [(100, 150), (200, 350), (300, 100), (350, 450)]:
            cv2.rectangle(image, (x, y), (x + 40, y + 30), (30, 180, 40), -1)

        # Run algorithm and time it — pass params dict (same as GreenOnBrown.inference)
        test_params = {
            'exg_min': 30, 'exg_max': 250,
            'hue_min': 30, 'hue_max': 90,
            'brightness_min': 5, 'brightness_max': 200,
            'saturation_min': 30, 'saturation_max': 255,
            'min_detection_area': 1, 'invert_hue': False,
        }
        start = _time.perf_counter()
        try:
            output = func(image, test_params)
        except TypeError:
            output = func(image)
        elapsed_ms = (_time.perf_counter() - start) * 1000

        # Handle (image, bool) return for pre-thresholded algorithms
        threshed = False
        if isinstance(output, tuple):
            output, threshed = output[0], output[1]

        # Validate output
        if not isinstance(output, np.ndarray):
            return {'success': False, 'error': 'Algorithm must return a numpy array'}

        if output.ndim != 2:
            return {'success': False, 'error': f'Expected 2D output, got {output.ndim}D'}

        # Threshold if not already binary
        if not threshed:
            _, binary = cv2.threshold(output, 100, 255, cv2.THRESH_BINARY)
        else:
            binary = output

        # Count detections
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        detections = len([c for c in contours if cv2.contourArea(c) > 10])

        return {
            'success': True,
            'name': name,
            'timing_ms': round(elapsed_ms, 2),
            'output_shape': list(output.shape),
            'detection_count': detections,
            'pre_thresholded': threshed,
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}


@owl_tool(
    tier='apply',
    description='Deploy a custom algorithm to connected OWL devices via MQTT.',
    parameters={
        'name': {
            'type': 'string',
            'description': 'Algorithm name to deploy',
            'required': True,
        },
    },
)
def deploy_algorithm(name, **context):
    try:
        from custom_algorithms import get_algorithm_code
        code = get_algorithm_code(name)
        if code is None:
            return {'success': False, 'error': f'Algorithm "{name}" not found'}

        mqtt_client = context.get('mqtt_client')
        if mqtt_client is None:
            return {'error': 'No MQTT client available'}

        return mqtt_client._send_command('install_algorithm', name=name, code=code)
    except Exception as e:
        return {'success': False, 'error': str(e)}


@owl_tool(
    tier='apply',
    description='Delete a custom algorithm file',
    parameters={
        'name': {
            'type': 'string',
            'description': 'Algorithm name to delete',
            'required': True,
        },
    },
)
def delete_algorithm(name, **context):
    try:
        from custom_algorithms import delete_algorithm as _delete
        return _delete(name)
    except Exception as e:
        return {'success': False, 'error': str(e)}
