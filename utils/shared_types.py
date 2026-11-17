from enum import IntEnum

_SWITCH_ON = {'true', '1', 'on', 'yes'}
_SWITCH_OFF = {'false', '0', 'off', 'no'}


def parse_switch(value):
    """An on/off value from a command, JSON body or tool call as a bool.

    bool('false') is True: a client that sent the string "false" to
    set_detection_enable switched spot spraying ON. Accepts real booleans,
    0/1 and the usual words in any case; anything else (None, 2, 'banana')
    raises ValueError, so the caller refuses the command and nothing changes.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _SWITCH_ON:
            return True
        if word in _SWITCH_OFF:
            return False
    raise ValueError(f'not an on/off value: {value!r}')

class Sensitivity(IntEnum):
    LOW = 0
    MEDIUM = 5
    HIGH = 10