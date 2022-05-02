#!/home/pi/.virtualenvs/owl/bin/python3
from algorithms import exg, exg_standardised, exg_standardised_hue, hsv, exgr, gndvi, maxg
import imutils
import numpy as np
import cv2


def green_on_brown(image, exgMin=30, exgMax=250, hueMin=30, hueMax=90, brightnessMin=5, brightnessMax=200, saturationMin=30,
                   saturationMax=255, minArea=1, headless=True, algorithm='exg'):
    '''
    Uses a provided algorithm and contour detection to determine green objects in the image. Min and Max
    thresholds are provided.
    :param image: input image to be analysed
    :param exgMin:
    :param exgMax:
    :param hueMin:
    :param hueMax:
    :param brightnessMin:
    :param brightnessMax:
    :param saturationMin:
    :param saturationMax:
    :param minArea: minimum area for the detection - used to filter out small detections
    :param headless: True: no windows display; False: watch what the algorithm does
    :param algorithm: the algorithm to use. Defaults to ExG if not correct
    :return: returns the contours, bounding boxes, centroids and the image on which the boxes have been drawn
    '''

    # different algorithm options, add in your algorithm here if you make a new one!
    threshedAlready = False
    if algorithm == 'exg':
        output = exg(image)

    elif algorithm == 'exgr':
        output = exgr(image)

    elif algorithm == 'maxg':
        output = maxg(image)

    elif algorithm == 'nexg':
        output = exg_standardised(image)

    elif algorithm == 'exhsv':
        output = exg_standardised_hue(image, hueMin=hueMin, hueMax=hueMax,
                                      brightnessMin=brightnessMin, brightnessMax=brightnessMax,
                                      saturationMin=saturationMin, saturationMax=saturationMax)

    elif algorithm == 'hsv':
        output, threshedAlready = hsv(image, hueMin=hueMin, hueMax=hueMax,
                                      brightnessMin=brightnessMin, brightnessMax=brightnessMax,
                                      saturationMin=saturationMin, saturationMax=saturationMax)

    elif algorithm == 'gndvi':
        output = gndvi(image)

    else:
        output = exg(image)
        print('[WARNING] DEFAULTED TO EXG')

    if not headless:
        cv2.imshow("Threshold", output)

    # run the thresholds provided
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    # if not a binary image, run an adaptive threshold on the area that fits within the thresholded bounds.
    if not threshedAlready:
        output = np.where(output > exgMin, output, 0)
        output = np.where(output > exgMax, 0, output)
        output = np.uint8(np.abs(output))
        if not headless:
            cv2.imshow("post", output)

        thresholdOut = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 2)
        thresholdOut = cv2.morphologyEx(thresholdOut, cv2.MORPH_CLOSE, kernel, iterations=1)

    # if already binary, run morphological operations to remove any noise
    if threshedAlready:
        thresholdOut = cv2.morphologyEx(output, cv2.MORPH_CLOSE, kernel, iterations=5)

    if not headless:
        cv2.imshow("Threshold", thresholdOut)

    # find all the contours on the binary images
    cnts = cv2.findContours(thresholdOut.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = imutils.grab_contours(cnts)
    weedCenters = []
    boxes = []
    # loop over all the detected contours and calculate the centres and bounding boxes
    for c in cnts:
        # filter based on total area of contour
        if cv2.contourArea(c) > minArea:
            # calculate the min bounding box
            startX, startY, boxW, boxH = cv2.boundingRect(c)
            endX = startX + boxW
            endY = startY + boxH
            cv2.rectangle(image, (int(startX), int(startY)), (endX, endY), (0, 0, 255), 2)
            # save the bounding box
            boxes.append([startX, startY, boxW, boxH])
            # compute box center
            centerX = int(startX + (boxW / 2))
            centerY = int(startY + (boxH / 2))
            weedCenters.append([centerX, centerY])

    # returns the bounding boxes, centroids and the image on which the boxes have been drawn
    return boxes, weedCenters, image

