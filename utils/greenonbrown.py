#!/usr/bin/env python
from utils.algorithms import exg, exg_standardised, exg_standardised_hue, hsv, exgr, gndvi, maxg
import numpy as np
import cv2


class GreenOnBrown:
    def __init__(self, algorithm='exg', label_file='models/labels.txt'):
        self.algorithm = algorithm
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

        # Retrieve the function based on the algorithm name
        self.func = self.algorithms.get(algorithm, exg_standardised_hue)

    def inference(self, image,
                  exg_min=30,
                  exg_max=250,
                  hue_min=30,
                  hue_max=90,
                  brightness_min=5,
                  brightness_max=200,
                  saturation_min=30,
                  saturation_max=255,
                  min_detection_area=1,
                  show_display=False,
                  invert_hue=False,
                  label='WEED'):
        threshed_already = False

        # Handle special cases for functions with additional parameters
        if algorithm == 'exhsv':
            output = func(image, hue_min=hue_min, hue_max=hue_max, brightness_min=brightness_min,
                          brightness_max=brightness_max, saturation_min=saturation_min,
                          saturation_max=saturation_max, invert_hue=invert_hue)
        elif algorithm == 'hsv':
            output, threshed_already = func(image, hue_min=hue_min, hue_max=hue_max, brightness_min=brightness_min,
                                            brightness_max=brightness_max, saturation_min=saturation_min,
                                            saturation_max=saturation_max, invert_hue=invert_hue)
        else:
            output = func(image)

        weed_centres = []
        boxes = []

        if not threshed_already:
            output = np.clip(output, exg_min, exg_max)
            output = np.uint8(np.abs(output))
            if show_display:
                cv2.imshow("HSV Threshold on ExG", output)
            threshold_out = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
                                                  31, 2)
            # threshold_out = cv2.threshold(output, exg_min, exg_max, cv2.THRESH_BINARY)
            threshold_out = cv2.morphologyEx(threshold_out, cv2.MORPH_CLOSE, self.kernel, iterations=1)
        else:
            threshold_out = cv2.morphologyEx(output, cv2.MORPH_CLOSE, self.kernel, iterations=5)

        contours, _ = cv2.findContours(threshold_out, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for c in contours:
            if cv2.contourArea(c) > min_detection_area:
                x, y, w, h = cv2.boundingRect(c)
                boxes.append([x, y, w, h])
                weed_centres.append([x + w // 2, y + h // 2])

        if show_display:
            image_out = image.copy()
            for box in boxes:
                startX, startY, boxW, boxH = box
                endX = startX + boxW
                endY = startY + boxH
                cv2.putText(image_out, label, (startX, startY + 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 0, 0), 2)
                cv2.rectangle(image_out, (int(startX), int(startY)), (endX, endY), (0, 0, 255), 2)

            return contours, boxes, weed_centres, image_out

        return contours, boxes, weed_centres, None
