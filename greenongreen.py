import cv2
import os

def green_on_green(image, headless=True, algorithm='default.pb'):
    boxes = []
    weedCenters = []
    image = None


    # equivalent to green_on_brown, this returns the bounding boxes, centroids
    # and the image on which the boxes have been drawn
    return boxes, weedCenters, image