"""Farmer-entered session metadata and data-collection keys shared by the
OWL (utils/mqtt_manager.py) and both controllers.

Kept free of heavy imports so the networked controller can use it without
pulling in the OWL's MQTT manager.

Metadata is written into every image's EXIF ImageDescription and into
session_metadata.json. Noktura maps it onto existing dataset fields:
crop -> crop_type, lighting -> datasets.lighting, notes -> description, and
crop_season / weather / purpose into agcontext
(Noktura docs/owl/NOKTURA_INTEGRATION_SPEC.md). Values are slugs
(e.g. crop 'wheat', lighting 'natural_sun'), never translated labels.
"""

SESSION_METADATA_FIELDS = ('field_name', 'crop', 'crop_season', 'weather', 'lighting',
                           'purpose', 'notes', 'vehicle', 'run_id')
SESSION_METADATA_MAX_LEN = 500

# Capture keys the controller may set live (MQTT set_capture_params).
# Applied live only: the controller keeps them in its own [Collect] and
# re-sends them on every reconnect, so collect settings never reach spray mode.
CAPTURE_PARAM_KEYS = ('sample_mode', 'sample_distance_m', 'sample_interval_s', 'jpeg_quality',
                      'record_mode', 'video_max_s', 'shutter_cap', 'smear_limit_mm', 'max_gain')


def clean_session_metadata(source):
    """Session metadata dict with every known field as a trimmed string."""
    source = source or {}
    return {key: str(source.get(key, '') or '').strip()[:SESSION_METADATA_MAX_LEN]
            for key in SESSION_METADATA_FIELDS}
