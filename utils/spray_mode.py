"""
What the nozzles do, decided in one place.

Before v3.27 seven writers (dashboard state, the shared detection Value, the
hardware switch callbacks, first-boot, faults, and direct relay all_on/all_off
calls) each switched the nozzles their own way, so a Ute switch could not stop
a dashboard blanket and a request made during first-boot replayed when setup
finished. Now every input goes into resolve(), which returns one mode, and
RelayController.set_mode() is the only thing that applies it.

Pure: no I/O, no clock, no threads. owl.py builds SprayInputs and acts on the
result; tests/unit/test_spray_mode.py walks every combination.

Priority, highest first:
  1. first-boot setup             -> off   (inhibit)
  2. hardware switch off          -> off   (inhibit)
  3. any fault, on_error = off    -> off   (inhibit)
  4. link lost while blanket, on_error = off -> off (inhibit; spot continues)
  5. a fault that stops detection, on_error = on, spraying requested -> blanket
  6. the operator's request

An inhibit clears the stored request (owl.py does that), so spraying never
comes back by itself: someone has to press Start (or flip the switch) again.

THESE ARE PRODUCT DECISIONS (Guy, 2026-11-07), not defaults to tidy up.
"Fail-safe" here does not mean "fail off": on_error = on is the farmer's
choice to keep spraying, a phone leaving a standalone OWL changes nothing,
and a networked OWL follows its controller's screen. Turning nozzles off
"to be safe" anywhere else is a regression. The full rules, and why, are in
the public docs: readthedocs docs/software/spray-behaviour.md ("How the OWL
decides to spray"). Change a rule only on purpose, and update that page and
tests/unit/test_spray_mode.py with it.
"""

from dataclasses import dataclass, field

OFF = 'off'
SPOT = 'spot'
BLANKET = 'blanket'
MODES = (OFF, SPOT, BLANKET)

# Legacy detection_mode codes (MQTT set_detection_mode, Advanced switch)
MODE_TO_CODE = {SPOT: 0, OFF: 1, BLANKET: 2}
CODE_TO_MODE = {code: mode for mode, code in MODE_TO_CODE.items()}

# Reasons resolve() can return
FIRSTBOOT = 'firstboot'
SWITCH_OFF = 'switch_off'
LINK_LOST = 'link_lost'
FAULT_PREFIX = 'fault:'

# What a person reads. Sentence case, no codes.
REASON_TEXT = {
    FIRSTBOOT: 'Setup is running',
    SWITCH_OFF: 'Switch is off',
    LINK_LOST: 'Lost contact with the controller',
    'fault:camera_lost': 'Camera fault',
    'fault:control_lost': 'Control fault',
    'fault:relay_stuck': 'Relay fault',
    'setup_finished': 'Setup finished',
}


def reason_text(reason):
    if not reason:
        return ''
    code = fault_code(reason)
    if code:
        return REASON_TEXT.get(FAULT_PREFIX + code, code.replace('_', ' ').capitalize() + ' fault')
    return REASON_TEXT.get(reason, reason.replace('_', ' ').capitalize())


def fault_code(reason):
    """'fault:off:camera_lost' / 'fault:camera_lost' -> 'camera_lost'."""
    if not reason or not reason.startswith(FAULT_PREFIX):
        return None
    return reason[len(FAULT_PREFIX):].split(':')[-1]


def is_inhibit(reason):
    """Reasons that clear the stored request when they start."""
    return bool(reason) and (reason in (FIRSTBOOT, SWITCH_OFF, LINK_LOST)
                             or reason.startswith(FAULT_PREFIX + 'off:'))


@dataclass
class SprayInputs:
    request: str = OFF               # off | spot | blanket (the operator's choice)
    switch: object = None            # None (no switch) | 'off' | 'on'
    firstboot: bool = False
    # {code: detection_possible} for every active fault except link_lost
    faults: dict = field(default_factory=dict)
    on_error: str = 'off'            # off | on
    link_lost: bool = False          # networked OWL with no word from its controller


def resolve(inputs):
    """(mode, reason). reason is None when the operator's request is followed
    as asked; otherwise it names why not. Fault reasons are
    'fault:off:<code>' (an inhibit, on_error = off) or 'fault:<code>'
    (blanket, on_error = on)."""
    request = inputs.request if inputs.request in MODES else OFF
    on_error_on = str(inputs.on_error).strip().lower() == 'on'

    if inputs.firstboot:
        return OFF, FIRSTBOOT
    if inputs.switch == 'off':
        return OFF, SWITCH_OFF
    if inputs.faults and not on_error_on:
        return OFF, FAULT_PREFIX + 'off:' + _first(inputs.faults)
    if inputs.link_lost and request == BLANKET and not on_error_on:
        return OFF, LINK_LOST
    if on_error_on and request != OFF:
        blind = [code for code, possible in inputs.faults.items() if not possible]
        if blind:
            return BLANKET, FAULT_PREFIX + _first(dict.fromkeys(blind))
    return request, None


# Status LED priority: the most urgent active fault is the one that flashes
FAULT_PRIORITY = ('relay_stuck', 'camera_lost', 'control_lost', 'link_lost')


def _first(faults):
    for code in FAULT_PRIORITY:
        if code in faults:
            return code
    return sorted(faults)[0]


def top_fault(codes):
    """Highest-priority code of an iterable of fault codes, or None."""
    codes = list(codes)
    return _first(dict.fromkeys(codes)) if codes else None
