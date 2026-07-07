"""Pure geometry math for OWL crop / lane / actuation-band computation.

Deliberately dependency-free (no cv2, picamera2 or GPIO) so it is importable on
any platform and serves as the SINGLE source of truth shared by
``owl.Owl.recompute_geometry`` and the geometry tests — eliminating the drift risk
of a hand-maintained test replica.
"""


def compute_geometry(frame_w, frame_h, crop_left, crop_right, crop_top, crop_bottom,
                     relay_num, actuation_top, actuation_bottom):
    """Compute the crop slice, cropped dimensions, lane coordinates and actuation
    band pixel bounds from per-edge crop fractions, the actuation band and relay_num.

    Returns a dict of derived values, or ``None`` if frame dimensions are missing.

    All fractional inputs are clamped: crop edges to ``[0.0, 0.49]`` (so the crop can
    never collapse the frame to zero size) and the band to ``[0.0, 1.0]`` with a
    full-height fallback for a degenerate (top >= bottom) band.
    """
    if not (frame_w and frame_h):
        return None

    left = min(max(crop_left, 0.0), 0.49)
    right = min(max(crop_right, 0.0), 0.49)
    top = min(max(crop_top, 0.0), 0.49)
    bottom = min(max(crop_bottom, 0.0), 0.49)

    crop_l = int(frame_w * left)
    crop_r = int(frame_w * (1.0 - right))
    crop_t = int(frame_h * top)
    crop_b = int(frame_h * (1.0 - bottom))
    cropped_width = crop_r - crop_l
    cropped_height = crop_b - crop_t

    lane_width = cropped_width / relay_num
    lane_coords = {i: int(i * lane_width) for i in range(relay_num)}

    a_top = min(max(actuation_top, 0.0), 1.0)
    a_bottom = min(max(actuation_bottom, 0.0), 1.0)
    if a_top >= a_bottom:
        # Degenerate band — fall back to the full cropped height.
        a_top, a_bottom = 0.0, 1.0

    return {
        'crop_slice': (slice(crop_t, crop_b), slice(crop_l, crop_r)),
        'cropped_width': cropped_width,
        'cropped_height': cropped_height,
        'lane_width': lane_width,
        'lane_coords': lane_coords,
        'lane_coords_int': {k: int(v) for k, v in lane_coords.items()},
        'y_top': int(cropped_height * a_top),
        'y_bottom': int(cropped_height * a_bottom),
        'clamped_edges': (left, right, top, bottom),
    }
