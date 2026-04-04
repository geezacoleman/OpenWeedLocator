#!/usr/bin/env python
"""
Benchmark: Three eras of OWL — FULL FRAME LOOP comparison (exhsv).

  v1 (Aug 2021):  cam.read -> green_on_brown -> lane assignment -> controller.receive -> fps.update
  v2 (main):      cam.read -> green_on_brown (class) -> lane assignment -> controller.receive
  v3 (current):   cam.read -> inference -> dedup actuation -> stream copy -> loop timing

Each iteration uses a synthetic image with a random number of weeds (0-50).
All three versions run end-to-end on the same image: detection output
(contours, weed centres) feeds directly into actuation. This means
findContours scaling and per-contour processing are fully exercised.

v1 code inlined verbatim from `git show 4e513b2`.
v2 code inlined from `git show main:utils/algorithms.py` and `git show main:utils/greenonbrown.py`.
v3 code uses the live current codebase.
"""
import sys
import os
import time
import json
import argparse
import statistics
import threading
from collections import deque

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.greenonbrown import GreenOnBrown


# =============================================================================
# ORIGINAL ALGORITHMS (commit 4e513b2, 23 Aug 2021)
# =============================================================================

def orig_exg(image):
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)
    imgOut = 2 * green - red - blue
    imgOut = np.clip(imgOut, 0, 255)
    imgOut = imgOut.astype('uint8')
    return imgOut


def orig_exg_standardised(image):
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)
    chanSum = red + green + blue
    chanSum = np.where(chanSum == 0, 1, chanSum)
    b = blue / chanSum
    g = green / chanSum
    r = red / chanSum
    imgOut = 255 * (2 * g - r - b)
    imgOut = np.where(imgOut < 0, 0, imgOut)
    imgOut = np.where(imgOut > 255, 255, imgOut)
    imgOut = imgOut.astype('uint8')
    return imgOut


def orig_hsv(image, hueMin=30, hueMax=90, brightnessMin=10, brightnessMax=220,
             saturationMin=30, saturationMax=255):
    image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue = image[:, :, 0]
    sat = image[:, :, 1]
    val = image[:, :, 2]
    hueThresh = cv2.inRange(hue, hueMin, hueMax)
    satThresh = cv2.inRange(sat, saturationMin, saturationMax)
    valThresh = cv2.inRange(val, brightnessMin, brightnessMax)
    outThresh = satThresh & valThresh & hueThresh
    return outThresh, True


def orig_exg_standardised_hue(image, hueMin=30, hueMax=90, brightnessMin=10,
                               brightnessMax=220, saturationMin=30, saturationMax=255):
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)
    chanSum = red + green + blue
    chanSum = np.where(chanSum == 0, 1, chanSum)
    b = blue / chanSum
    g = green / chanSum
    r = red / chanSum
    imgOut = 255 * (2 * g - r - b)
    imgOut = np.where(imgOut < 0, 0, imgOut)
    imgOut = np.where(imgOut > 255, 255, imgOut)
    imgOut = imgOut.astype('uint8')
    hsvThresh, _ = orig_hsv(image, hueMin=hueMin, hueMax=hueMax,
                             brightnessMin=brightnessMin, brightnessMax=brightnessMax,
                             saturationMin=saturationMin, saturationMax=saturationMax)
    imgOut = hsvThresh & imgOut
    return imgOut


# =============================================================================
# v2 (MAIN BRANCH) ALGORITHMS — array slicing, np.where, 3 separate HSV ops
# =============================================================================

def main_exg(image):
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)
    image_out = 2 * green - red - blue
    image_out = np.clip(image_out, 0, 255)
    image_out = image_out.astype('uint8')
    return image_out


def main_exg_standardised(image):
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)
    channel_sum = red + green + blue
    channel_sum = np.where(channel_sum == 0, 1, channel_sum)
    b = blue / channel_sum
    g = green / channel_sum
    r = red / channel_sum
    image_out = 255 * (2 * g - r - b)
    image_out = np.where(image_out < 0, 0, image_out)
    image_out = np.where(image_out > 255, 255, image_out)
    image_out = image_out.astype('uint8')
    return image_out


def main_hsv(image, hue_min=30, hue_max=90, brightness_min=10, brightness_max=220,
             saturation_min=30, saturation_max=255, invert_hue=False):
    image_hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue = image_hsv[:, :, 0]
    sat = image_hsv[:, :, 1]
    val = image_hsv[:, :, 2]
    hueThresh = cv2.inRange(hue, hue_min, hue_max)
    satThresh = cv2.inRange(sat, saturation_min, saturation_max)
    valThresh = cv2.inRange(val, brightness_min, brightness_max)
    outThresh = satThresh & valThresh & hueThresh
    return outThresh, True


def main_exg_standardised_hue(image, hue_min=30, hue_max=90, brightness_min=10,
                               brightness_max=220, saturation_min=30, saturation_max=255,
                               invert_hue=False):
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)
    channel_sum = red + green + blue
    channel_sum = np.where(channel_sum == 0, 1, channel_sum)
    b = blue / channel_sum
    g = green / channel_sum
    r = red / channel_sum
    image_out = 255 * (2 * g - r - b)
    image_out = np.where(image_out < 0, 0, image_out)
    image_out = np.where(image_out > 255, 255, image_out)
    image_out = image_out.astype('uint8')
    hsv_thresh, _ = main_hsv(image, hue_min=hue_min, hue_max=hue_max,
                              brightness_min=brightness_min, brightness_max=brightness_max,
                              saturation_min=saturation_min, saturation_max=saturation_max)
    image_out = hsv_thresh & image_out
    return image_out


# =============================================================================
# v2 (MAIN BRANCH) green_on_brown() — class-based, no MAX_DETECTIONS, kernel in __init__
# =============================================================================

class MainGreenOnBrown:
    """GreenOnBrown as it exists on main branch (v2.3.1)."""
    def __init__(self, algorithm='exg'):
        self.algorithm = algorithm
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.algorithms = {
            'exg': main_exg,
            'nexg': main_exg_standardised,
            'exhsv': main_exg_standardised_hue,
            'hsv': main_hsv,
        }

    def inference(self, image, exg_min=30, exg_max=250, hue_min=30, hue_max=90,
                  brightness_min=5, brightness_max=200, saturation_min=30,
                  saturation_max=255, min_detection_area=1, show_display=False,
                  algorithm='exg', invert_hue=False, label='WEED'):
        threshed_already = False
        func = self.algorithms.get(algorithm, main_exg_standardised_hue)

        if algorithm == 'exhsv':
            output = func(image, hue_min=hue_min, hue_max=hue_max,
                          brightness_min=brightness_min, brightness_max=brightness_max,
                          saturation_min=saturation_min, saturation_max=saturation_max)
        elif algorithm == 'hsv':
            output, threshed_already = func(image, hue_min=hue_min, hue_max=hue_max,
                                            brightness_min=brightness_min, brightness_max=brightness_max,
                                            saturation_min=saturation_min, saturation_max=saturation_max)
        else:
            output = func(image)

        weed_centres = []
        boxes = []

        if not threshed_already:
            output = np.clip(output, exg_min, exg_max)
            output = np.uint8(np.abs(output))
            threshold_out = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                  cv2.THRESH_BINARY_INV, 31, 2)
            threshold_out = cv2.morphologyEx(threshold_out, cv2.MORPH_CLOSE, self.kernel, iterations=1)
        else:
            threshold_out = cv2.morphologyEx(output, cv2.MORPH_CLOSE, self.kernel, iterations=5)

        contours, _ = cv2.findContours(threshold_out, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for c in contours:
            if cv2.contourArea(c) > min_detection_area:
                x, y, w, h = cv2.boundingRect(c)
                boxes.append([x, y, w, h])
                weed_centres.append([x + w // 2, y + h // 2])

        return contours, boxes, weed_centres, image


# =============================================================================
# ORIGINAL green_on_brown() (commit 4e513b2)
# =============================================================================

def _grab_contours(cnts):
    if len(cnts) == 2:
        return cnts[0]
    elif len(cnts) == 3:
        return cnts[1]
    raise Exception("Contours tuple has unexpected length")


def orig_green_on_brown(image, exgMin=30, exgMax=250, hueMin=30, hueMax=90,
                         brightnessMin=5, brightnessMax=200, saturationMin=30,
                         saturationMax=255, minArea=1, headless=True, algorithm='exg'):
    threshedAlready = False
    if algorithm == 'exg':
        output = orig_exg(image)
    elif algorithm == 'nexg':
        output = orig_exg_standardised(image)
    elif algorithm == 'exhsv':
        output = orig_exg_standardised_hue(image, hueMin=hueMin, hueMax=hueMax,
                                            brightnessMin=brightnessMin, brightnessMax=brightnessMax,
                                            saturationMin=saturationMin, saturationMax=saturationMax)
    elif algorithm == 'hsv':
        output, threshedAlready = orig_hsv(image, hueMin=hueMin, hueMax=hueMax,
                                            brightnessMin=brightnessMin, brightnessMax=brightnessMax,
                                            saturationMin=saturationMin, saturationMax=saturationMax)
    else:
        output = orig_exg(image)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    if not threshedAlready:
        output = np.where(output > exgMin, output, 0)
        output = np.where(output > exgMax, 0, output)
        output = np.uint8(np.abs(output))
        thresholdOut = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                              cv2.THRESH_BINARY_INV, 31, 2)
        thresholdOut = cv2.morphologyEx(thresholdOut, cv2.MORPH_CLOSE, kernel, iterations=1)
    if threshedAlready:
        thresholdOut = cv2.morphologyEx(output, cv2.MORPH_CLOSE, kernel, iterations=5)

    cnts = cv2.findContours(thresholdOut.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = _grab_contours(cnts)
    weedCenters = []
    boxes = []
    for c in cnts:
        if cv2.contourArea(c) > minArea:
            startX, startY, boxW, boxH = cv2.boundingRect(c)
            endX = startX + boxW
            endY = startY + boxH
            cv2.rectangle(image, (int(startX), int(startY)), (endX, endY), (0, 0, 255), 2)
            boxes.append([startX, startY, boxW, boxH])
            centerX = int(startX + (boxW / 2))
            centerY = int(startY + (boxH / 2))
            weedCenters.append([centerX, centerY])

    return cnts, boxes, weedCenters, image


# =============================================================================
# ORIGINAL RELAY CONTROLLER (commit 4e513b2) — queue + condition + logger
# =============================================================================

class OrigLogger:
    """Original logger wrote to file on every receive() call."""
    def log_line(self, line):
        # Simulate the string format + file I/O cost
        _ = line.encode('utf-8')


class OrigController:
    """Simplified from commit 4e513b2 relay_control.py Controller class."""
    def __init__(self, nozzle_dict):
        self.nozzle_dict = nozzle_dict
        self.nozzle_queue_dict = {}
        self.nozzle_condition_dict = {}
        self.logger = OrigLogger()

        for nozzle in range(len(nozzle_dict)):
            self.nozzle_queue_dict[nozzle] = deque(maxlen=5)
            self.nozzle_condition_dict[nozzle] = threading.Condition()

    def receive(self, nozzle, timeStamp, location=0, delay=0, duration=1):
        """Original receive: format log string + append to deque + notify."""
        input_q_message = [nozzle, timeStamp, delay, duration]
        input_q = self.nozzle_queue_dict[nozzle]
        input_condition = self.nozzle_condition_dict[nozzle]
        with input_condition:
            input_q.append(input_q_message)
            input_condition.notify()

        line = "nozzle: {} | time: {} | location {} | delay: {} | duration: {}".format(
            nozzle, timeStamp, location, delay, duration)
        self.logger.log_line(line)


# =============================================================================
# CURRENT RELAY CONTROLLER — just the receive() overhead (no consumer threads)
# =============================================================================

class CurrentRelayController:
    """Current RelayController.receive() — no logger call, just deque + condition."""
    def __init__(self, relay_dict):
        self.relay_queue_dict = {}
        self.relay_condition_dict = {}
        for relay in range(len(relay_dict)):
            self.relay_queue_dict[relay] = deque(maxlen=5)
            self.relay_condition_dict[relay] = threading.Condition()

    def receive(self, relay, time_stamp, delay=0, duration=1):
        input_queue_message = [relay, time_stamp, delay, duration]
        input_queue = self.relay_queue_dict[relay]
        input_condition = self.relay_condition_dict[relay]
        with input_condition:
            input_queue.append(input_queue_message)
            input_condition.notify()


# =============================================================================
# SYNTHETIC IMAGE
# =============================================================================

def make_test_image(width, height, num_weeds=8, seed=42):
    rng = np.random.RandomState(seed)
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :, 0] = 60
    img[:, :, 1] = 80
    img[:, :, 2] = 120
    noise = rng.randint(-15, 15, (height, width, 3), dtype=np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    for _ in range(num_weeds):
        cx = rng.randint(10, width - 10)
        cy = rng.randint(10, height - 10)
        ax1 = rng.randint(4, 15)
        ax2 = rng.randint(4, 15)
        angle = rng.randint(0, 180)
        # Moderate green — ExG ~80-160, survives v1's np.where exgMax=200 band-pass
        color = (45 + rng.randint(0, 15), 100 + rng.randint(0, 40), 40 + rng.randint(0, 15))
        cv2.ellipse(img, (cx, cy), (ax1, ax2), angle, 0, 360, color, -1)

    # Small noise detections — tiny green spots mimicking leaf edges and
    # background texture that produce real contours in field images
    num_noise = 3
    for _ in range(num_noise):
        cx = rng.randint(10, width - 10)
        cy = rng.randint(10, height - 10)
        color = (45 + rng.randint(0, 15), 100 + rng.randint(0, 40), 40 + rng.randint(0, 15))
        cv2.ellipse(img, (cx, cy), (2, 3), rng.randint(0, 180), 0, 360, color, -1)

    return img


# =============================================================================
# FULL FRAME LOOP SIMULATORS
# =============================================================================

def simulate_original_frame(frame, algorithm, params, nozzle_num, lane_coords,
                            lane_width, y_act, controller):
    """
    Simulates one iteration of the original hoot() loop from commit 4e513b2.

    What happens per frame in original:
    1. cam.read() [simulated: frame already provided]
    2. frame.copy() before passing to green_on_brown
    3. green_on_brown() — algorithm + threshold + contour + rectangle drawing
    4. Lane assignment loop over detected weed centres
    5. controller.receive() per weed (with logging)
    6. fps.update() [imutils FPS counter]
    """
    # Step 2: Copy frame (original always did frame.copy())
    frame_copy = frame.copy()

    # Step 3: Detection — full pipeline including findContours + rectangle drawing
    cnts, boxes, weed_centres, image_out = orig_green_on_brown(
        frame_copy, algorithm=algorithm, headless=True, **params)

    # Step 4-5: Lane assignment + actuation (per weed, per lane)
    for ID, centre in enumerate(weed_centres):
        if centre[1] > y_act:
            spray_time = time.time()
            for i in range(nozzle_num):
                if int(lane_coords[i]) <= centre[0] < int(lane_coords[i] + lane_width):
                    controller.receive(nozzle=i, timeStamp=spray_time, duration=0.15)

    # Step 6: FPS counter overhead (original used imutils FPS which calls time.time())
    _ = time.time()

    return weed_centres, image_out


def simulate_main_frame(frame, algorithm, detector, params, nozzle_num,
                        lane_coords, lane_width, y_act, controller):
    """
    Simulates one iteration of the main branch (v2.3.1) hoot() loop.

    Same structure as original but uses class-based GreenOnBrown,
    kernel in __init__, and np.clip instead of np.where for thresholding.
    Still does per-weed lane assignment and per-fire logging.
    """
    # Step 2: Copy frame
    frame_copy = frame.copy()

    # Step 3: Detection (class-based, kernel reused)
    cnts, boxes, weed_centres, image_out = detector.inference(
        frame_copy, algorithm=algorithm, show_display=False,
        exg_min=params['exgMin'], exg_max=params['exgMax'],
        hue_min=params['hueMin'], hue_max=params['hueMax'],
        saturation_min=params['saturationMin'], saturation_max=params['saturationMax'],
        brightness_min=params['brightnessMin'], brightness_max=params['brightnessMax'],
        min_detection_area=params['minArea'])

    # Step 4-5: Lane assignment + actuation (per weed, per lane)
    for ID, centre in enumerate(weed_centres):
        if centre[1] > y_act:
            spray_time = time.time()
            for i in range(nozzle_num):
                if int(lane_coords[i]) <= centre[0] < int(lane_coords[i] + lane_width):
                    controller.receive(nozzle=i, timeStamp=spray_time, duration=0.15)

    # Step 6: FPS counter overhead
    _ = time.time()

    return weed_centres, image_out


def simulate_current_frame(frame, algorithm, detector, params,
                            relay_num, lane_coords_int, lane_width,
                            y_act, relay_controller, stream_lock,
                            loop_times, frame_count,
                            actuation_duration, delay):
    """
    Simulates one iteration of the current hoot() loop.

    What happens per frame in current:
    1. loop_start = time.time()
    2. cam.read() [simulated: frame already provided]
    3. detector.inference() — class-based, no rectangle drawing when headless
    4. Deduplicated relay actuation (fired set) with y-threshold
    5. Stream frame copy (every 5th frame, thread-safe)
    6. Loop time tracking (deque rolling average)
    """
    # Step 1: Loop timing
    loop_start = time.time()

    # Step 3: Detection — full pipeline including findContours
    cnts, boxes, weed_centres, image_out = detector.inference(
        frame, algorithm=algorithm, show_display=False,
        exg_min=params['exgMin'], exg_max=params['exgMax'],
        hue_min=params['hueMin'], hue_max=params['hueMax'],
        saturation_min=params['saturationMin'], saturation_max=params['saturationMax'],
        brightness_min=params['brightnessMin'], brightness_max=params['brightnessMax'],
        min_detection_area=params['minArea'])

    # Step 4: Deduplicated actuation with y-threshold
    if weed_centres:
        actuation_time = time.time()
        fired = set()
        for centre in weed_centres:
            if centre[1] >= y_act:
                relay_id = min(int(centre[0] / lane_width), relay_num - 1)
                fired.add(relay_id)
        for relay_id in fired:
            relay_controller.receive(relay=relay_id, delay=delay,
                                      time_stamp=actuation_time, duration=actuation_duration)

    # Step 5: Stream copy (simulate every-5th-frame behaviour)
    if frame_count % 5 == 0:
        with stream_lock:
            _ = frame.copy()

    # Step 6: Loop timing
    loop_time_ms = (time.time() - loop_start) * 1000
    loop_times.append(loop_time_ms)

    return weed_centres, frame


# =============================================================================
# BENCHMARK HARNESS
# =============================================================================

def stats(times):
    n = len(times)
    std = statistics.stdev(times) if n > 1 else 0
    return {
        'mean': round(statistics.mean(times), 3),
        'median': round(statistics.median(times), 3),
        'min': round(min(times), 3),
        'max': round(max(times), 3),
        'std': round(std, 3),
        'sem': round(std / (n ** 0.5), 3) if n > 1 else 0,
        'p95': round(sorted(times)[int(n * 0.95)], 3),
    }


def run_full_loop_benchmark(algorithm, width, height, iterations=1000,
                            max_detections=50):
    """Benchmark the complete per-frame pipeline for all three versions.

    Each iteration uses a synthetic image with a random number of weeds
    (0 to max_detections). Detection output (findContours, contour
    processing, weed centres) feeds directly into actuation — no
    synthetic centres. This exercises the full pipeline end-to-end.
    """
    nozzle_num = 4
    params = dict(exgMin=25, exgMax=200, hueMin=39, hueMax=83,
                  saturationMin=50, saturationMax=220,
                  brightnessMin=60, brightnessMax=190, minArea=10)

    # Pre-generate weed counts: random 0-max_detections per iteration
    rng = np.random.RandomState(42)
    weed_counts = [int(rng.randint(0, max_detections + 1)) for _ in range(iterations)]
    avg_weeds = statistics.mean(weed_counts)

    y_act = int(0.2 * height)

    # --- v1 (Original 2021) setup ---
    nozzle_dict = {0: 13, 1: 15, 2: 16, 3: 18}
    orig_controller = OrigController(nozzle_dict)
    orig_lane_width = width / nozzle_num
    orig_lane_coords = {i: int(i * orig_lane_width) for i in range(nozzle_num)}

    # --- v2 (Main branch) setup ---
    main_detector = MainGreenOnBrown(algorithm=algorithm)
    main_controller = OrigController(nozzle_dict)
    main_lane_width = width / nozzle_num
    main_lane_coords = {i: int(i * main_lane_width) for i in range(nozzle_num)}

    # --- v3 (Current) setup ---
    curr_lane_width = width / nozzle_num
    curr_lane_coords_int = {i: int(i * curr_lane_width) for i in range(nozzle_num)}
    curr_detector = GreenOnBrown(algorithm=algorithm)
    curr_relay_controller = CurrentRelayController({0: 13, 1: 15, 2: 16, 3: 18})
    curr_stream_lock = threading.Lock()
    curr_loop_times = deque(maxlen=30)

    print(f"\n{'='*80}")
    print(f"  FULL LOOP: {algorithm.upper()} @ {width}x{height}  ({iterations} iterations)")
    print(f"  Weeds per image: 0-{max_detections} (avg {avg_weeds:.1f})")
    print(f"{'='*80}")

    # Verify detection counts on a test image
    test_img = make_test_image(width, height, num_weeds=25, seed=999)
    wc_v1, _ = simulate_original_frame(
        test_img.copy(), algorithm, params, nozzle_num,
        orig_lane_coords, orig_lane_width, y_act, orig_controller)
    wc_v2, _ = simulate_main_frame(
        test_img.copy(), algorithm, main_detector, params, nozzle_num,
        main_lane_coords, main_lane_width, y_act, main_controller)
    wc_v3, _ = simulate_current_frame(
        test_img.copy(), algorithm, curr_detector, params,
        nozzle_num, curr_lane_coords_int, curr_lane_width,
        y_act, curr_relay_controller, curr_stream_lock, curr_loop_times,
        0, 0.15, 0)
    print(f"  Detection check (25 weeds): v1={len(wc_v1)}, v2={len(wc_v2)}, v3={len(wc_v3)}")

    # Warmup
    for i in range(20):
        warmup_img = make_test_image(width, height, num_weeds=25, seed=i)
        simulate_original_frame(warmup_img, algorithm, params, nozzle_num,
                                 orig_lane_coords, orig_lane_width, y_act,
                                 orig_controller)
        simulate_main_frame(warmup_img.copy(), algorithm, main_detector, params,
                            nozzle_num, main_lane_coords, main_lane_width,
                            y_act, main_controller)
        simulate_current_frame(warmup_img.copy(), algorithm, curr_detector, params,
                                nozzle_num, curr_lane_coords_int, curr_lane_width,
                                y_act, curr_relay_controller, curr_stream_lock,
                                curr_loop_times, i, 0.15, 0)

    # Benchmark v1 (original 2021)
    orig_times = []
    orig_detected = []
    for i in range(iterations):
        img = make_test_image(width, height, num_weeds=weed_counts[i], seed=i)
        t0 = time.perf_counter()
        wc, _ = simulate_original_frame(img, algorithm, params, nozzle_num,
                                         orig_lane_coords, orig_lane_width,
                                         y_act, orig_controller)
        t1 = time.perf_counter()
        orig_times.append((t1 - t0) * 1000)
        orig_detected.append(len(wc))

    # Benchmark v2 (main branch)
    main_times = []
    main_detected = []
    for i in range(iterations):
        img = make_test_image(width, height, num_weeds=weed_counts[i], seed=i)
        t0 = time.perf_counter()
        wc, _ = simulate_main_frame(img, algorithm, main_detector, params,
                                     nozzle_num, main_lane_coords, main_lane_width,
                                     y_act, main_controller)
        t1 = time.perf_counter()
        main_times.append((t1 - t0) * 1000)
        main_detected.append(len(wc))

    # Benchmark v3 (current)
    curr_times = []
    curr_detected = []
    for i in range(iterations):
        img = make_test_image(width, height, num_weeds=weed_counts[i], seed=i)
        t0 = time.perf_counter()
        wc, _ = simulate_current_frame(img, algorithm, curr_detector, params,
                                        nozzle_num, curr_lane_coords_int, curr_lane_width,
                                        y_act, curr_relay_controller, curr_stream_lock,
                                        curr_loop_times, i, 0.15, 0)
        t1 = time.perf_counter()
        curr_times.append((t1 - t0) * 1000)
        curr_detected.append(len(wc))

    print(f"  Avg detections: v1={statistics.mean(orig_detected):.1f}, "
          f"v2={statistics.mean(main_detected):.1f}, v3={statistics.mean(curr_detected):.1f}")

    orig_s = stats(orig_times)
    main_s = stats(main_times)
    curr_s = stats(curr_times)
    v1_v3_speedup = orig_s['mean'] / curr_s['mean'] if curr_s['mean'] > 0 else 0
    v2_v3_speedup = main_s['mean'] / curr_s['mean'] if curr_s['mean'] > 0 else 0

    print(f"\n  {'Metric':<10} {'v1 (2021)':>14} {'v2 (main)':>14} {'v3 (current)':>14} {'v1->v3':>9} {'v2->v3':>9}")
    print(f"  {'-'*72}")
    for key in ['mean', 'median', 'p95', 'min']:
        o = orig_s[key]
        m = main_s[key]
        c = curr_s[key]
        s13 = f"{o/c:.2f}x" if c > 0 else "-"
        s23 = f"{m/c:.2f}x" if c > 0 else "-"
        print(f"  {key:<10} {o:>11.3f} ms {m:>11.3f} ms {c:>11.3f} ms {s13:>9} {s23:>9}")

    # FPS equivalent
    orig_fps = 1000.0 / orig_s['mean'] if orig_s['mean'] > 0 else 0
    main_fps = 1000.0 / main_s['mean'] if main_s['mean'] > 0 else 0
    curr_fps = 1000.0 / curr_s['mean'] if curr_s['mean'] > 0 else 0
    print(f"\n  FPS:  v1 {orig_fps:.0f}  |  v2 {main_fps:.0f}  |  v3 {curr_fps:.0f}")
    print(f"  >> v3 is {v1_v3_speedup:.2f}x faster than v1, {v2_v3_speedup:.2f}x faster than v2 (mean)")

    return {
        'algorithm': algorithm,
        'resolution': f"{width}x{height}",
        'iterations': iterations,
        'max_weeds_per_image': max_detections,
        'avg_weeds_per_image': round(avg_weeds, 1),
        'avg_detected_v1': round(statistics.mean(orig_detected), 1),
        'avg_detected_v2': round(statistics.mean(main_detected), 1),
        'avg_detected_v3': round(statistics.mean(curr_detected), 1),
        'v1_2021': orig_s,
        'v2_main': main_s,
        'v3_current': curr_s,
        'v1_fps': round(orig_fps, 1),
        'v2_fps': round(main_fps, 1),
        'v3_fps': round(curr_fps, 1),
        'speedup_v1_v3': round(v1_v3_speedup, 2),
        'speedup_v2_v3': round(v2_v3_speedup, 2),
        'per_iteration': {
            'weed_counts': weed_counts,
            'v1_detected': orig_detected,
            'v2_detected': main_detected,
            'v3_detected': curr_detected,
            'v1_times_ms': [round(t, 3) for t in orig_times],
            'v2_times_ms': [round(t, 3) for t in main_times],
            'v3_times_ms': [round(t, 3) for t in curr_times],
        },
    }


# =============================================================================
# COMPONENT BREAKDOWN — time each piece separately
# =============================================================================

def run_component_breakdown(algorithm, width, height, iterations=500):
    """Break down where time is spent in each version."""
    nozzle_num = 4
    params = dict(exgMin=25, exgMax=200, hueMin=39, hueMax=83,
                  saturationMin=50, saturationMax=220,
                  brightnessMin=60, brightnessMax=190, minArea=10)
    img = make_test_image(width, height, num_weeds=8)

    # Setup
    nozzle_dict = {0: 13, 1: 15, 2: 16, 3: 18}
    orig_ctrl = OrigController(nozzle_dict)
    curr_detector = GreenOnBrown(algorithm=algorithm)
    curr_ctrl = CurrentRelayController(nozzle_dict)
    stream_lock = threading.Lock()

    lane_width = width / nozzle_num
    lane_coords = {i: int(i * lane_width) for i in range(nozzle_num)}

    components = {}

    # 1. frame.copy() — original always copies before detection
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        _ = img.copy()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    components['frame_copy'] = stats(times)

    # 2. Original detection (includes rectangle drawing)
    times = []
    for _ in range(iterations):
        frame = img.copy()
        t0 = time.perf_counter()
        orig_green_on_brown(frame, algorithm=algorithm, headless=True, **params)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    components['orig_detection'] = stats(times)

    # 3. Current detection (no drawing when show_display=False)
    times = []
    for _ in range(iterations):
        frame = img.copy()
        t0 = time.perf_counter()
        curr_detector.inference(frame, algorithm=algorithm, show_display=False,
                                 exg_min=params['exgMin'], exg_max=params['exgMax'],
                                 hue_min=params['hueMin'], hue_max=params['hueMax'],
                                 saturation_min=params['saturationMin'],
                                 saturation_max=params['saturationMax'],
                                 brightness_min=params['brightnessMin'],
                                 brightness_max=params['brightnessMax'],
                                 min_detection_area=params['minArea'])
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    components['curr_detection'] = stats(times)

    # 4. Original actuation (per-weed, per-lane, with logger)
    fake_centres = [[100, 200], [300, 200], [500, 200], [200, 100]]
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        for ID, centre in enumerate(fake_centres):
            spray_time = time.time()
            for i in range(nozzle_num):
                if int(lane_coords[i]) <= centre[0] < int(lane_coords[i] + lane_width):
                    orig_ctrl.receive(nozzle=i, timeStamp=spray_time, duration=0.15)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    components['orig_actuation'] = stats(times)

    # 5. Current actuation (deduplicated, no logger per-call)
    curr_lane_width = width / nozzle_num
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        if fake_centres:
            actuation_time = time.time()
            fired = set()
            for centre in fake_centres:
                relay_id = min(int(centre[0] / curr_lane_width), nozzle_num - 1)
                fired.add(relay_id)
            for relay_id in fired:
                curr_ctrl.receive(relay=relay_id, time_stamp=actuation_time, duration=0.15)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    components['curr_actuation'] = stats(times)

    # 6. Stream frame copy (current only, every 5th frame cost)
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        with stream_lock:
            _ = img.copy()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    components['stream_copy'] = stats(times)

    # 7. Kernel creation overhead (original creates per-frame, current once in __init__)
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        _ = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    components['kernel_creation'] = stats(times)

    return components


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='OWL three-era benchmark')
    parser.add_argument('--rpi', action='store_true',
                        help='Use RPi-native resolutions (IMX296 global shutter aspect ratio)')
    parser.add_argument('--iterations', type=int, default=1000,
                        help='Iterations per benchmark (default: 1000)')
    args = parser.parse_args()

    iterations = args.iterations

    algorithm = 'exhsv'

    if args.rpi:
        # IMX296 global shutter native: 1456x1088 (4:3-ish, actually 91:68)
        # Scale down at same aspect ratio, plus common OWL field resolutions
        resolutions = [
            (416, 320),     # Minimum viable — Pi 3B+ safe
            (512, 384),     # Quarter-HD equivalent
            (640, 480),     # Classic OWL default
            (728, 544),     # Mid-range
            (800, 608),     # ~800x640 equivalent at native AR
            (1024, 768),    # XGA
            (1456, 1088),   # IMX296 native resolution
        ]
        breakdown_res = (800, 608)
    else:
        resolutions = [
            (416, 320),
            (640, 480),
            (832, 624),
            (1280, 720),
            (1920, 1080),
        ]
        breakdown_res = (1280, 720)

    mode = "RPi (IMX296 native)" if args.rpi else "Desktop"
    print("=" * 68)
    print(f"  OWL Three-Era Benchmark — {mode}")
    print(f"  Python {sys.version.split()[0]}, OpenCV {cv2.__version__}, NumPy {np.__version__}")
    print(f"  Iterations: {iterations}")
    print("=" * 68)

    # --- Part 1: Full loop comparison ---
    all_results = []
    for w, h in resolutions:
        result = run_full_loop_benchmark(algorithm, w, h, iterations=iterations)
        all_results.append(result)

    # Summary
    print(f"\n\n{'='*90}")
    print("  FULL LOOP SUMMARY — Three Eras of OWL")
    print(f"{'='*90}")
    print(f"  {'Algorithm':<8} {'Resolution':<12} {'v1 ms':>8} {'v2 ms':>8} {'v3 ms':>8} {'v1 FPS':>8} {'v2 FPS':>8} {'v3 FPS':>8} {'v1->v3':>8} {'v2->v3':>8}")
    print(f"  {'-'*86}")
    for r in all_results:
        print(f"  {r['algorithm']:<8} {r['resolution']:<12} "
              f"{r['v1_2021']['mean']:>5.2f} ms "
              f"{r['v2_main']['mean']:>5.2f} ms "
              f"{r['v3_current']['mean']:>5.2f} ms "
              f"{r['v1_fps']:>6.0f}   "
              f"{r['v2_fps']:>6.0f}   "
              f"{r['v3_fps']:>6.0f}   "
              f"{r['speedup_v1_v3']:>6.2f}x "
              f"{r['speedup_v2_v3']:>6.2f}x")

    # --- Part 2: Component breakdown ---
    bw, bh = breakdown_res
    print(f"\n\n{'='*72}")
    print(f"  COMPONENT BREAKDOWN @ {bw}x{bh} (exhsv)")
    print(f"{'='*72}")
    components = run_component_breakdown('exhsv', bw, bh, iterations=iterations)

    print(f"\n  {'Component':<25} {'Mean ms':>10} {'P95 ms':>10}  Notes")
    print(f"  {'-'*72}")
    notes = {
        'frame_copy':      'Both versions (original copies before detection)',
        'orig_detection':  'Original green_on_brown() + rectangle drawing',
        'curr_detection':  'Current GreenOnBrown.inference() (no drawing)',
        'orig_actuation':  'Per-weed per-lane + logger.log_line() per fire',
        'curr_actuation':  'Deduped (fired set) + no per-call logging',
        'stream_copy':     'Current only (MJPEG stream, every 5th frame)',
        'kernel_creation': 'Original: per-frame | Current: once in __init__',
    }
    for comp_name, comp_stats in components.items():
        note = notes.get(comp_name, '')
        print(f"  {comp_name:<25} {comp_stats['mean']:>7.3f} ms {comp_stats['p95']:>7.3f} ms  {note}")

    # Totals
    orig_total = (components['frame_copy']['mean'] +
                  components['orig_detection']['mean'] +
                  components['orig_actuation']['mean'] +
                  components['kernel_creation']['mean'])
    curr_total = (components['curr_detection']['mean'] +
                  components['curr_actuation']['mean'] +
                  components['stream_copy']['mean'] * 0.2)  # 1 in 5 frames
    print(f"\n  Estimated total per frame:")
    print(f"    Original: {orig_total:.3f} ms  (detection + copy + actuation + kernel)")
    print(f"    Current:  {curr_total:.3f} ms  (detection + actuation + stream/5)")
    if curr_total > 0:
        print(f"    Speedup:  {orig_total / curr_total:.2f}x")

    # What's new in current that didn't exist in original
    print(f"\n  ADDITIONAL WORK in current loop (not in original):")
    print(f"    - MJPEG stream copy (every 5th frame)")
    print(f"    - Loop time tracking (deque rolling avg)")
    print(f"    - MQTT state sync (update_state thread)")
    print(f"    - System stats collection (every 90 frames)")
    print(f"    - Image recording (every Nth frame)")
    print(f"    - Live algorithm/model/class hot-switching checks")
    print(f"    - Detection deduplication (fired set vs per-weed)")
    print(f"    - ConfigValidator, LogManager, multiprocessing.Value")

    print(f"\n  REMOVED from original:")
    print(f"    - imutils dependency (VideoStream, FPS, grab_contours, resize)")
    print(f"    - Per-frame kernel creation (now __init__ once)")
    print(f"    - Per-weed logger.log_line() in actuation")
    print(f"    - cv2.rectangle drawing on every detection (now only when display=True)")

    # Save
    output = {
        'date': time.strftime('%Y-%m-%d'),
        'description': 'Full frame loop: v1 (4e513b2, 2021) vs v2 (main) vs v3 (current)',
        'mode': 'rpi' if args.rpi else 'desktop',
        'iterations': iterations,
        'platform': f"Python {sys.version.split()[0]}, OpenCV {cv2.__version__}, NumPy {np.__version__}",
        'full_loop_results': all_results,
        'component_breakdown': {k: v for k, v in components.items()},
    }
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"{time.strftime('%Y-%m-%d')}_v1_vs_current.json")
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results saved to {out_path}")


if __name__ == '__main__':
    main()
