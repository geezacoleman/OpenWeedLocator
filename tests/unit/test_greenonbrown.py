"""GreenOnBrown.inference on frames with known plants in them.

Every test builds a frame (bare soil plus drawn blobs), runs the production
detector and checks what comes out: the boxes and centres owl.py turns into
relay lanes. Nothing here re-implements the pipeline.

hsv and lut threshold directly, so their boxes are the blob itself. exg and
exhsv go through an adaptive threshold, which on a flat synthetic blob marks
the ring of soil around it: the box is the blob plus up to HALO px a side and
the centre is unchanged.
"""

import cv2
import numpy as np
import pytest

from utils.greenonbrown import GreenOnBrown, MAX_DETECTIONS, draw_boxes
from utils.lut_manager import LUT_SIZE

SOIL = (40, 60, 90)       # BGR: hue 12, a brown the hue band rejects
GREEN = (60, 180, 60)     # BGR: hue 60, ExG 240
ORANGE = (0, 150, 200)    # BGR: hue 22, ExG 100 (green by ExG only)
HALO = 13                 # adaptive threshold block is 31 px: 15 px reach, less the closing

ALGORITHMS = ['exg', 'exhsv', 'hsv']


def frame(*blobs, width=416, height=320):
    """Bare soil with blobs (x, y, w, h[, colour]) drawn on it."""
    image = np.full((height, width, 3), SOIL, dtype=np.uint8)
    for x, y, w, h, *colour in blobs:
        cv2.rectangle(image, (x, y), (x + w - 1, y + h - 1), colour[0] if colour else GREEN, -1)
    return image


def detect(image, algorithm='hsv', detector=None, **params):
    _, boxes, centres, _ = (detector or GreenOnBrown()).inference(
        image, algorithm=algorithm, **params)
    return boxes, centres


PLANT = (100, 80, 80, 60)     # x, y, w, h -> centre (140, 110)
SPECK = (300, 200, 6, 6)      # centre (303, 203)


# --------------------------------------------------------------------------
# A plant is found, where it is
# --------------------------------------------------------------------------

@pytest.mark.parametrize('algorithm', ALGORITHMS)
def test_one_plant_gives_one_box_centred_on_it(algorithm):
    boxes, centres = detect(frame(PLANT), algorithm)

    assert len(boxes) == 1, boxes
    assert centres == [[140, 110]]
    x, y, w, h = boxes[0]
    halo = 0 if algorithm == 'hsv' else HALO
    assert 100 - halo <= x <= 100 and 80 - halo <= y <= 80
    assert 180 <= x + w <= 180 + halo and 140 <= y + h <= 140 + halo


def test_threshold_algorithms_box_the_plant_exactly():
    assert detect(frame(PLANT), 'hsv') == ([[100, 80, 80, 60]], [[140, 110]])


@pytest.mark.parametrize('algorithm', ALGORITHMS)
def test_bare_soil_gives_nothing(algorithm):
    assert detect(frame(), algorithm) == ([], [])


@pytest.mark.parametrize('algorithm', ALGORITHMS)
def test_two_plants_in_different_lanes_are_both_reported(algorithm):
    _, centres = detect(frame(PLANT, (300, 200, 60, 60)), algorithm)
    assert sorted(centres) == [[140, 110], [330, 230]]


# --------------------------------------------------------------------------
# Minimum weed size
# --------------------------------------------------------------------------

def test_min_area_drops_the_speck_and_keeps_the_plant():
    image = frame(PLANT, SPECK)
    assert sorted(detect(image, 'hsv', min_detection_area=1)[1]) == [[140, 110], [303, 203]]
    assert detect(image, 'hsv', min_detection_area=100)[1] == [[140, 110]]


@pytest.mark.parametrize('algorithm', ['exg', 'exhsv'])
def test_min_area_applies_on_the_adaptive_threshold_path(algorithm):
    image = frame(PLANT, SPECK)
    assert len(detect(image, algorithm, min_detection_area=1)[0]) == 2
    assert detect(image, algorithm, min_detection_area=2000)[1] == [[140, 110]]


def test_min_area_is_a_strict_greater_than():
    """cv2.contourArea of an 80x60 rectangle is 79*59: a plant exactly at the
    limit is not sprayed, one px^2 over is."""
    area = 79 * 59
    assert detect(frame(PLANT), 'hsv', min_detection_area=area) == ([], [])
    assert detect(frame(PLANT), 'hsv', min_detection_area=area - 1)[1] == [[140, 110]]


# --------------------------------------------------------------------------
# The detection cap
# --------------------------------------------------------------------------

def many_plants():
    """70 squares on a grid, sides 7..26 px. Returns (frame, sides)."""
    sides, blobs = [], []
    for n in range(70):
        row, col = divmod(n, 10)
        side = 7 + n % 20
        sides.append(side)
        blobs.append((20 + col * 80, 20 + row * 85, side, side))
    return frame(*blobs, width=832, height=640), sides


def test_no_more_than_the_cap_and_the_largest_are_kept():
    image, sides = many_plants()
    boxes, centres = detect(image, 'hsv')

    assert MAX_DETECTIONS == 50
    assert len(boxes) == len(centres) == MAX_DETECTIONS
    assert sorted(w for _, _, w, _ in boxes) == sorted(sides)[-MAX_DETECTIONS:]


def test_under_the_cap_everything_is_reported():
    image, sides = many_plants()
    image[320:] = SOIL                     # keep the first four rows: 40 plants
    assert len(detect(image, 'hsv')[0]) == 40


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------

@pytest.mark.parametrize('algorithm', ['hsv', 'exhsv'])
@pytest.mark.parametrize('limits', [
    {'hue_min': 100, 'hue_max': 120},      # plant hue is 60
    {'hue_min': 61},
    {'hue_max': 59},
    {'saturation_min': 171},               # plant saturation is 170
    {'saturation_max': 169},
    {'brightness_min': 181},               # plant brightness is 180
    {'brightness_max': 179},
], ids=lambda limits: ','.join(f'{k}={v}' for k, v in limits.items()))
def test_a_plant_outside_a_limit_is_not_detected(algorithm, limits):
    assert detect(frame(PLANT), algorithm, **limits) == ([], [])


@pytest.mark.parametrize('algorithm', ['hsv', 'exhsv'])
def test_a_plant_exactly_on_the_limits_is_detected(algorithm):
    limits = dict(hue_min=60, hue_max=60, saturation_min=170, saturation_max=170,
                  brightness_min=180, brightness_max=180)
    assert detect(frame(PLANT), algorithm, **limits)[1] == [[140, 110]]


def test_exg_min_above_the_plant_hides_it():
    """ExG of the plant is 2*180-60-60 = 240. Clipping the whole frame to
    [250, 255] leaves nothing standing out."""
    assert detect(frame(PLANT), 'exg', exg_min=250, exg_max=255) == ([], [])
    assert detect(frame(PLANT), 'exg', exg_min=30, exg_max=250)[1] == [[140, 110]]


def test_exg_max_below_the_plant_hides_it():
    assert detect(frame(PLANT), 'exg', exg_min=0, exg_max=0) == ([], [])


@pytest.mark.parametrize('algorithm', ['hsv', 'exhsv'])
def test_invert_hue_selects_what_is_outside_the_band(algorithm):
    """brightness_min=100 takes the soil (brightness 90) out of the picture so
    only the hue band decides."""
    image = frame(PLANT)
    bright = dict(brightness_min=100)
    assert detect(image, algorithm, **bright)[1] == [[140, 110]]
    assert detect(image, algorithm, invert_hue=True, **bright) == ([], [])
    assert detect(image, algorithm, invert_hue=True, hue_min=100, hue_max=120,
                  **bright)[1] == [[140, 110]]


# --------------------------------------------------------------------------
# Which algorithm runs
# --------------------------------------------------------------------------

def test_each_name_runs_its_own_algorithm():
    """An orange blob is green by ExG but outside the hue band: only exg
    finds it. hsv boxes the green plant exactly, exhsv with a halo."""
    orange = frame((100, 80, 80, 60, ORANGE))
    assert detect(orange, 'exg')[1] == [[140, 110]]
    assert detect(orange, 'exhsv') == ([], [])
    assert detect(orange, 'hsv') == ([], [])

    green = frame(PLANT)
    assert detect(green, 'hsv')[0] == [[100, 80, 80, 60]]
    assert detect(green, 'exhsv')[0] != [[100, 80, 80, 60]]


def test_unknown_algorithm_falls_back_to_exhsv():
    orange, green = frame((100, 80, 80, 60, ORANGE)), frame(PLANT)
    assert detect(orange, 'no-such-algorithm') == ([], [])          # not exg
    assert detect(green, 'no-such-algorithm') == detect(green, 'exhsv')


def lut_for(*colours):
    table = np.zeros(LUT_SIZE, dtype=np.uint8)
    for b, g, r in colours:
        table[(r >> 3) << 10 | (g >> 3) << 5 | (b >> 3)] = 255
    return table


def test_lut_detects_the_painted_colour_only():
    detector = GreenOnBrown(algorithm='lut', lut_table=lut_for(GREEN))
    image = frame(PLANT, (300, 200, 60, 60, ORANGE))
    assert detect(image, 'lut', detector) == ([[100, 80, 80, 60]], [[140, 110]])

    detector.set_lut(lut_for(ORANGE))      # profile switch while running
    assert detect(image, 'lut', detector) == ([[300, 200, 60, 60]], [[330, 230]])


def test_lut_without_a_profile_refuses_rather_than_detecting_nothing():
    with pytest.raises(RuntimeError, match='no profile is loaded'):
        GreenOnBrown().inference(frame(PLANT), algorithm='lut')


def test_lut_applies_min_area_and_survives_a_resolution_change():
    detector = GreenOnBrown(algorithm='lut', lut_table=lut_for(GREEN))
    assert detect(frame(PLANT, SPECK), 'lut', detector, min_detection_area=100)[1] == [[140, 110]]
    big = frame(PLANT, width=832, height=640)
    assert detect(big, 'lut', detector)[1] == [[140, 110]]


# --------------------------------------------------------------------------
# What comes back
# --------------------------------------------------------------------------

def test_the_frame_is_returned_untouched_unless_a_display_is_asked_for():
    image = frame(PLANT)
    before = image.copy()
    detector = GreenOnBrown()

    _, _, _, out = detector.inference(image, algorithm='hsv')
    assert out is image
    assert np.array_equal(image, before)

    _, boxes, _, annotated = detector.inference(image, algorithm='hsv', show_display=True)
    assert np.array_equal(image, before), 'the frame that gets recorded must not be drawn on'
    assert annotated is not image
    assert tuple(annotated[80, 100]) == (0, 0, 255), 'box outline drawn at the plant corner'


def test_draw_boxes_draws_on_a_copy():
    image = frame(PLANT)
    out = draw_boxes(image, [[100, 80, 80, 60]])
    assert tuple(image[80, 100]) == GREEN
    assert tuple(out[80, 100]) == (0, 0, 255)
