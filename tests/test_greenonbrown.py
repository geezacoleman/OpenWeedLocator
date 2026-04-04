"""
CCA vs findContours equivalence tests for GreenOnBrown blob extraction.

Validates that cv2.connectedComponentsWithStats produces identical detection
results (boxes, centres, relay assignments) to the current findContours pipeline,
before any production code changes.

Run: pytest tests/test_greenonbrown.py -v
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.greenonbrown import MAX_DETECTIONS

# ---------------------------------------------------------------------------
# Helper functions: two blob-extraction pipelines operating on binary images
# ---------------------------------------------------------------------------

def contour_inference_from_binary(threshold_out, min_detection_area=1, max_detections=MAX_DETECTIONS):
    """Reference pipeline extracted verbatim from GreenOnBrown.inference() lines 68-84."""
    contours, _ = cv2.findContours(threshold_out, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    valid = []
    for c in contours:
        area = cv2.contourArea(c)
        if area > min_detection_area:
            valid.append((area, c))

    if len(valid) > max_detections:
        valid.sort(key=lambda x: x[0], reverse=True)
        valid = valid[:max_detections]

    boxes = []
    weed_centres = []
    for area, c in valid:
        x, y, w, h = cv2.boundingRect(c)
        boxes.append([x, y, w, h])
        weed_centres.append([x + w // 2, y + h // 2])

    return boxes, weed_centres


def cca_inference_from_binary(threshold_out, min_detection_area=1, max_detections=MAX_DETECTIONS,
                              use_bbox_centre=True):
    """CCA pipeline: connectedComponentsWithStats returns boxes, areas, centroids in one call."""
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        threshold_out, connectivity=8
    )

    # Skip label 0 (background), filter by area
    valid = []
    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area > min_detection_area:
            valid.append((area, i))

    # Sort by area descending, cap at max_detections
    if len(valid) > max_detections:
        valid.sort(key=lambda x: x[0], reverse=True)
        valid = valid[:max_detections]

    boxes = []
    weed_centres = []
    for area, i in valid:
        x = stats[i, cv2.CC_STAT_LEFT]
        y = stats[i, cv2.CC_STAT_TOP]
        w = stats[i, cv2.CC_STAT_WIDTH]
        h = stats[i, cv2.CC_STAT_HEIGHT]
        boxes.append([x, y, w, h])

        if use_bbox_centre:
            weed_centres.append([x + w // 2, y + h // 2])
        else:
            # Centre-of-mass from CCA centroids
            cx, cy = centroids[i]
            weed_centres.append([int(round(cx)), int(round(cy))])

    return boxes, weed_centres


def _sort_by_xy(boxes, centres):
    """Sort boxes and centres by (x, y) for order-independent comparison."""
    if not boxes:
        return [], []
    paired = sorted(zip(boxes, centres), key=lambda p: (p[0][0], p[0][1]))
    sorted_boxes = [p[0] for p in paired]
    sorted_centres = [p[1] for p in paired]
    return sorted_boxes, sorted_centres


# ---------------------------------------------------------------------------
# TestCCAEquivalence: 9 tests on hand-crafted binary images
# ---------------------------------------------------------------------------

class TestCCAEquivalence:
    """Compare CCA vs findContours on synthetic binary images."""

    def test_single_large_blob(self):
        """One filled circle — exact match on box and centre."""
        img = np.zeros((480, 640), dtype=np.uint8)
        cv2.circle(img, (320, 240), 50, 255, -1)

        cnt_boxes, cnt_centres = contour_inference_from_binary(img)
        cca_boxes, cca_centres = cca_inference_from_binary(img)

        assert len(cnt_boxes) == 1
        assert len(cca_boxes) == 1
        assert cnt_boxes == cca_boxes
        assert cnt_centres == cca_centres

    def test_many_small_blobs(self):
        """20 non-overlapping circles — same count and boxes after sorting."""
        img = np.zeros((480, 640), dtype=np.uint8)
        rng = np.random.RandomState(42)
        # Place circles on a grid to avoid overlap
        positions = []
        for row in range(4):
            for col in range(5):
                x = 50 + col * 120
                y = 50 + row * 110
                r = rng.randint(10, 25)
                positions.append((x, y, r))
                cv2.circle(img, (x, y), r, 255, -1)

        cnt_boxes, cnt_centres = contour_inference_from_binary(img)
        cca_boxes, cca_centres = cca_inference_from_binary(img)

        assert len(cnt_boxes) == 20
        assert len(cca_boxes) == 20

        cnt_s, cnt_c = _sort_by_xy(cnt_boxes, cnt_centres)
        cca_s, cca_c = _sort_by_xy(cca_boxes, cca_centres)

        assert cnt_s == cca_s
        assert cnt_c == cca_c

    def test_no_blobs(self):
        """All-black image — both return empty lists."""
        img = np.zeros((480, 640), dtype=np.uint8)

        cnt_boxes, cnt_centres = contour_inference_from_binary(img)
        cca_boxes, cca_centres = cca_inference_from_binary(img)

        assert cnt_boxes == []
        assert cca_boxes == []
        assert cnt_centres == []
        assert cca_centres == []

    def test_blobs_at_edges(self):
        """Half-circles at 4 edges — all detected by both."""
        img = np.zeros((480, 640), dtype=np.uint8)
        # Top edge
        cv2.circle(img, (320, 0), 30, 255, -1)
        # Bottom edge
        cv2.circle(img, (320, 479), 30, 255, -1)
        # Left edge
        cv2.circle(img, (0, 240), 30, 255, -1)
        # Right edge
        cv2.circle(img, (639, 240), 30, 255, -1)

        cnt_boxes, _ = contour_inference_from_binary(img)
        cca_boxes, _ = cca_inference_from_binary(img)

        assert len(cnt_boxes) == 4
        assert len(cca_boxes) == 4

        cnt_s, _ = _sort_by_xy(cnt_boxes, [c for c in range(4)])
        cca_s, _ = _sort_by_xy(cca_boxes, [c for c in range(4)])
        assert cnt_s == cca_s

    def test_blob_at_min_area_threshold(self):
        """Blob near min_area threshold — each method internally consistent.

        contourArea uses Green's theorem (boundary approximation) ~81 for 10x10 rect.
        CCA CC_STAT_AREA counts actual pixels = 100 for 10x10 rect.
        This is a known difference — CCA is more accurate (matches ground truth).
        """
        img = np.zeros((200, 200), dtype=np.uint8)
        # 10x10 filled rectangle: 100 actual pixels
        cv2.rectangle(img, (50, 50), (59, 59), 255, -1)

        # CCA: area=100, so threshold=99 includes, threshold=100 excludes (strict >)
        cca_boxes_100, _ = cca_inference_from_binary(img, min_detection_area=100)
        cca_boxes_99, _ = cca_inference_from_binary(img, min_detection_area=99)
        assert len(cca_boxes_100) == 0  # 100 > 100 is False
        assert len(cca_boxes_99) == 1   # 100 > 99 is True

        # Contours: area~=81, so threshold=80 includes, threshold=81 excludes
        cnt_boxes_81, _ = contour_inference_from_binary(img, min_detection_area=81)
        cnt_boxes_80, _ = contour_inference_from_binary(img, min_detection_area=80)
        assert len(cnt_boxes_81) == 0  # 81 > 81 is False
        assert len(cnt_boxes_80) == 1  # 81 > 80 is True

        # Both detect the same box when threshold is low enough for both
        cnt_boxes_low, _ = contour_inference_from_binary(img, min_detection_area=10)
        cca_boxes_low, _ = cca_inference_from_binary(img, min_detection_area=10)
        assert cnt_boxes_low == cca_boxes_low

    def test_max_detections_cap(self):
        """80 circles — both return only 50 largest."""
        img = np.zeros((800, 1000), dtype=np.uint8)
        rng = np.random.RandomState(99)
        # Place 80 circles on a grid, varying radius
        placed = 0
        for row in range(8):
            for col in range(10):
                x = 40 + col * 95
                y = 40 + row * 95
                r = 5 + placed % 30  # Radius varies from 5 to 34
                cv2.circle(img, (x, y), r, 255, -1)
                placed += 1

        cnt_boxes, _ = contour_inference_from_binary(img, min_detection_area=1, max_detections=50)
        cca_boxes, _ = cca_inference_from_binary(img, min_detection_area=1, max_detections=50)

        assert len(cnt_boxes) == 50
        assert len(cca_boxes) == 50

        # Both should have picked the 50 largest — sort and compare
        cnt_s, _ = _sort_by_xy(cnt_boxes, cnt_boxes)
        cca_s, _ = _sort_by_xy(cca_boxes, cca_boxes)
        assert cnt_s == cca_s

    def test_hsv_path_morphology(self):
        """Binary image + MORPH_CLOSE iterations=5 (HSV path) — same results."""
        img = np.zeros((480, 640), dtype=np.uint8)
        # Draw several small blobs with gaps (morphology will close them)
        cv2.circle(img, (200, 200), 20, 255, -1)
        cv2.circle(img, (210, 200), 20, 255, -1)  # Overlapping
        cv2.circle(img, (400, 300), 15, 255, -1)

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        morphed = cv2.morphologyEx(img, cv2.MORPH_CLOSE, kernel, iterations=5)

        cnt_boxes, cnt_centres = contour_inference_from_binary(morphed)
        cca_boxes, cca_centres = cca_inference_from_binary(morphed)

        cnt_s, cnt_c = _sort_by_xy(cnt_boxes, cnt_centres)
        cca_s, cca_c = _sort_by_xy(cca_boxes, cca_centres)

        assert cnt_s == cca_s
        assert cnt_c == cca_c

    def test_adjacent_touching_blobs(self):
        """Two rectangles sharing an edge — merged by 8-connectivity in both."""
        img = np.zeros((200, 200), dtype=np.uint8)
        # Two adjacent rectangles sharing the column at x=100
        cv2.rectangle(img, (50, 50), (99, 150), 255, -1)
        cv2.rectangle(img, (100, 50), (150, 150), 255, -1)

        cnt_boxes, _ = contour_inference_from_binary(img)
        cca_boxes, _ = cca_inference_from_binary(img)

        # findContours with RETR_EXTERNAL sees one contour for adjacent shapes
        # CCA with 8-connectivity also sees one component
        assert len(cnt_boxes) == len(cca_boxes)
        assert cnt_boxes == cca_boxes

    def test_asymmetric_blob_centre_tolerance(self):
        """L-shaped blob: bbox centres match exactly; centre-of-mass differs."""
        img = np.zeros((300, 300), dtype=np.uint8)
        # L-shape: vertical bar + horizontal bar
        cv2.rectangle(img, (50, 50), (80, 200), 255, -1)   # Vertical
        cv2.rectangle(img, (50, 170), (180, 200), 255, -1)  # Horizontal

        cnt_boxes, cnt_centres = contour_inference_from_binary(img)
        cca_boxes_bbox, cca_centres_bbox = cca_inference_from_binary(img, use_bbox_centre=True)
        _, cca_centres_com = cca_inference_from_binary(img, use_bbox_centre=False)

        # Bbox centres match exactly
        assert len(cnt_boxes) == 1
        assert cnt_boxes == cca_boxes_bbox
        assert cnt_centres == cca_centres_bbox

        # Centre-of-mass differs from bbox centre for asymmetric shapes
        bbox_cx, bbox_cy = cca_centres_bbox[0]
        com_cx, com_cy = cca_centres_com[0]

        # Quantify the difference — it should be meaningful for an L-shape
        dx = abs(bbox_cx - com_cx)
        dy = abs(bbox_cy - com_cy)

        # The L-shape has more mass in the vertical bar, so COM should be
        # shifted left and up from bbox centre. Difference should be noticeable.
        assert dx > 0 or dy > 0, "Expected COM to differ from bbox centre for L-shape"

        # But within relay-lane tolerance (~160px lane at 640px, 4 relays)
        assert dx < 80, f"COM x-offset {dx}px exceeds relay-lane tolerance"
        assert dy < 80, f"COM y-offset {dy}px exceeds relay-lane tolerance"


# ---------------------------------------------------------------------------
# TestCCAFullPipelineEquivalence: 3 tests on realistic algorithm output
# ---------------------------------------------------------------------------

class TestCCAFullPipelineEquivalence:
    """Run real detection algorithms, then compare CCA vs findContours on the threshold output."""

    @staticmethod
    def _make_test_image(width=640, height=480):
        """Synthetic image with green patches on brown background."""
        image = np.full((height, width, 3), [60, 80, 120], dtype=np.uint8)
        rng = np.random.RandomState(42)
        for _ in range(8):
            cx = rng.randint(50, width - 50)
            cy = rng.randint(50, height - 50)
            rw = rng.randint(15, 40)
            rh = rng.randint(15, 40)
            image[max(0, cy - rh):cy + rh, max(0, cx - rw):cx + rw] = [30, 160, 50]
        return image

    def _run_algorithm_to_threshold(self, image, algorithm='exhsv'):
        """Run an algorithm and return the binary threshold_out (pre-contour step)."""
        from utils.algorithms import exg, exg_standardised_hue, hsv as hsv_algo

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

        if algorithm == 'exhsv':
            output = exg_standardised_hue(image, hue_min=39, hue_max=83,
                                          saturation_min=50, saturation_max=220,
                                          brightness_min=60, brightness_max=190)
            np.clip(output, 25, 200, out=output)
            output = output.astype(np.uint8)
            threshold_out = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                  cv2.THRESH_BINARY_INV, 31, 2)
            threshold_out = cv2.morphologyEx(threshold_out, cv2.MORPH_CLOSE, kernel, iterations=1)
        elif algorithm == 'hsv':
            output, _ = hsv_algo(image, hue_min=39, hue_max=83,
                                 saturation_min=50, saturation_max=220,
                                 brightness_min=60, brightness_max=190)
            threshold_out = cv2.morphologyEx(output, cv2.MORPH_CLOSE, kernel, iterations=5)
        elif algorithm == 'exg':
            output = exg(image)
            np.clip(output, 25, 200, out=output)
            output = output.astype(np.uint8)
            threshold_out = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                  cv2.THRESH_BINARY_INV, 31, 2)
            threshold_out = cv2.morphologyEx(threshold_out, cv2.MORPH_CLOSE, kernel, iterations=1)
        else:
            raise ValueError(f"Unknown algorithm: {algorithm}")

        return threshold_out

    def test_exhsv_pipeline(self):
        """ExHSV algorithm path: both extraction methods match."""
        image = self._make_test_image()
        threshold_out = self._run_algorithm_to_threshold(image, 'exhsv')

        cnt_boxes, cnt_centres = contour_inference_from_binary(threshold_out, min_detection_area=10)
        cca_boxes, cca_centres = cca_inference_from_binary(threshold_out, min_detection_area=10)

        cnt_s, cnt_c = _sort_by_xy(cnt_boxes, cnt_centres)
        cca_s, cca_c = _sort_by_xy(cca_boxes, cca_centres)

        assert cnt_s == cca_s
        np.testing.assert_allclose(cnt_c, cca_c, atol=1)

    def test_hsv_pipeline(self):
        """HSV path (threshed_already=True, 5 morphology iterations): both match."""
        image = self._make_test_image()
        threshold_out = self._run_algorithm_to_threshold(image, 'hsv')

        cnt_boxes, cnt_centres = contour_inference_from_binary(threshold_out, min_detection_area=10)
        cca_boxes, cca_centres = cca_inference_from_binary(threshold_out, min_detection_area=10)

        cnt_s, cnt_c = _sort_by_xy(cnt_boxes, cnt_centres)
        cca_s, cca_c = _sort_by_xy(cca_boxes, cca_centres)

        assert cnt_s == cca_s
        np.testing.assert_allclose(cnt_c, cca_c, atol=1)

    def test_exg_pipeline(self):
        """Plain ExG algorithm: both extraction methods match."""
        image = self._make_test_image()
        threshold_out = self._run_algorithm_to_threshold(image, 'exg')

        cnt_boxes, cnt_centres = contour_inference_from_binary(threshold_out, min_detection_area=10)
        cca_boxes, cca_centres = cca_inference_from_binary(threshold_out, min_detection_area=10)

        cnt_s, cnt_c = _sort_by_xy(cnt_boxes, cnt_centres)
        cca_s, cca_c = _sort_by_xy(cca_boxes, cca_centres)

        assert cnt_s == cca_s
        np.testing.assert_allclose(cnt_c, cca_c, atol=1)


# ---------------------------------------------------------------------------
# TestCCARelayAssignment: 2 tests verifying same relay outputs
# ---------------------------------------------------------------------------

class TestCCARelayAssignment:
    """Verify that CCA and findContours produce the same relay firing pattern."""

    IMAGE_WIDTH = 640
    RELAY_NUM = 4

    def setup_method(self):
        lane_width = self.IMAGE_WIDTH / self.RELAY_NUM
        self.lane_width = lane_width
        self.lane_coords_int = {i: int(i * lane_width) for i in range(self.RELAY_NUM)}

    @staticmethod
    def _run_actuation_logic(weed_centres, lane_width, relay_num, actuation_y_thresh=0):
        """Simulate actuation logic from owl.py."""
        if not weed_centres:
            return []
        fired = set()
        for centre in weed_centres:
            if centre[1] >= actuation_y_thresh:
                relay_id = min(int(centre[0] / lane_width), relay_num - 1)
                fired.add(relay_id)
        return sorted(fired)

    def test_same_relays_fired(self):
        """Both pipelines trigger the same relay set on a realistic scene."""
        image = np.full((480, 640, 3), [60, 80, 120], dtype=np.uint8)
        rng = np.random.RandomState(42)
        for _ in range(12):
            cx = rng.randint(30, 610)
            cy = rng.randint(30, 450)
            r = rng.randint(10, 30)
            cv2.circle(image, (cx, cy), r, (30, 160, 50), -1)

        from utils.algorithms import exg_standardised_hue
        output = exg_standardised_hue(image, hue_min=39, hue_max=83,
                                      saturation_min=50, saturation_max=220,
                                      brightness_min=60, brightness_max=190)
        np.clip(output, 25, 200, out=output)
        output = output.astype(np.uint8)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        threshold_out = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                              cv2.THRESH_BINARY_INV, 31, 2)
        threshold_out = cv2.morphologyEx(threshold_out, cv2.MORPH_CLOSE, kernel, iterations=1)

        _, cnt_centres = contour_inference_from_binary(threshold_out, min_detection_area=10)
        _, cca_centres = cca_inference_from_binary(threshold_out, min_detection_area=10)

        cnt_relays = self._run_actuation_logic(cnt_centres, self.lane_width, self.RELAY_NUM)
        cca_relays = self._run_actuation_logic(cca_centres, self.lane_width, self.RELAY_NUM)

        assert cnt_relays == cca_relays

    def test_actuation_zone_filtering(self):
        """With Y-threshold filtering, same relays fire."""
        img = np.zeros((480, 640), dtype=np.uint8)
        # Blobs in bottom 25% (actuation zone: y >= 360)
        cv2.circle(img, (100, 400), 20, 255, -1)
        cv2.circle(img, (400, 420), 25, 255, -1)
        # Blobs above zone (should be filtered)
        cv2.circle(img, (300, 100), 20, 255, -1)

        actuation_y_thresh = int(480 * 0.75)  # 360

        _, cnt_centres = contour_inference_from_binary(img, min_detection_area=1)
        _, cca_centres = cca_inference_from_binary(img, min_detection_area=1)

        cnt_relays = self._run_actuation_logic(cnt_centres, self.lane_width, self.RELAY_NUM,
                                               actuation_y_thresh)
        cca_relays = self._run_actuation_logic(cca_centres, self.lane_width, self.RELAY_NUM,
                                               actuation_y_thresh)

        assert cnt_relays == cca_relays
        # Verify the blob above zone was filtered out
        assert len(cnt_relays) == 2
