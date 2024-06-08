#!/usr/bin/env python
from utils.algorithms import exg, exg_standardised, exg_standardised_hue, hsv, exgr, gndvi, maxg
import numpy as np
import cv2


class GreenOnBrown:
    def __init__(self, algorithm='exg', label_file='models/labels.txt'):
        self.algorithm = algorithm
        self.weed_centres = None
        self.boxes = None
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

        # Dictionary mapping algorithm names to functions
        self.algorithms = {
            'exg': exg,
            'exgr': exgr,
            'maxg': maxg,
            'nexg': exg_standardised,
            'exhsv': exg_standardised_hue,
            'hsv': hsv,
            'gndvi': gndvi
        }

    def inference(self, image, exgMin=30, exgMax=250, hueMin=30, hueMax=90, brightnessMin=5, brightnessMax=200,
                  saturationMin=30, saturationMax=255, min_detection_area=1, show_display=False, algorithm='exg',
                  invert_hue=False, label='WEED'):
        threshed_already = False

        # Retrieve the function based on the algorithm name
        func = self.algorithms.get(algorithm, exg_standardised_hue)

        # Handle special cases for functions with additional parameters
        if algorithm == 'exhsv':
            output = func(image, hueMin=hueMin, hueMax=hueMax, brightnessMin=brightnessMin,
                          brightnessMax=brightnessMax, saturationMin=saturationMin,
                          saturationMax=saturationMax, invert_hue=invert_hue)
        elif algorithm == 'hsv':
            output, threshed_already = func(image, hueMin=hueMin, hueMax=hueMax, brightnessMin=brightnessMin,
                                            brightnessMax=brightnessMax, saturationMin=saturationMin,
                                            saturationMax=saturationMax, invert_hue=invert_hue)
        else:
            output = func(image)

        self.weed_centres = []
        self.boxes = []

        if not threshed_already:
            output = np.clip(output, exgMin, exgMax)
            output = np.uint8(np.abs(output))
            if show_display:
                cv2.imshow("HSV Threshold on ExG", output)
            threshold_out = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
                                                  31, 2)
            # threshold_out = cv2.threshold(output, exgMin, exgMax, cv2.THRESH_BINARY)
            threshold_out = cv2.morphologyEx(threshold_out, cv2.MORPH_CLOSE, self.kernel, iterations=1)
        else:
            threshold_out = cv2.morphologyEx(output, cv2.MORPH_CLOSE, self.kernel, iterations=5)

        contours, _ = cv2.findContours(threshold_out, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for c in contours:
            if cv2.contourArea(c) > min_detection_area:
                startX, startY, boxW, boxH = cv2.boundingRect(c)
                self.boxes.append([startX, startY, boxW, boxH])
                centerX = int(startX + (boxW / 2))
                centerY = int(startY + (boxH / 2))
                self.weed_centres.append([centerX, centerY])

        if show_display:
            for box in self.boxes:
                startX, startY, boxW, boxH = box
                endX = startX + boxW
                endY = startY + boxH
                cv2.putText(image, label, (startX, startY + 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 0, 0), 2)
                cv2.rectangle(image, (int(startX), int(startY)), (endX, endY), (0, 0, 255), 2)

        return contours, self.boxes, self.weed_centres, image
