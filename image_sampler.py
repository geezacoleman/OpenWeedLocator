import numpy as np
from time import strftime
import cv2
import os

def image_sample(image, centresList, saveDir, sideLength=200):
    """
    Generates and saves random image crop around a target centre
    :param image: input image to collect snapshot from
    :param centresList: list of target centres
    :param sideLength: dimensions of square
    """
    if sideLength > image.shape[0]:
        sideLength = image.shape[0]
    displayImage = image.copy()
    halfLength = int(sideLength / 2)

    # compute startX and StartY of the cropped area
    for ID, centre in enumerate(centresList):
        startX = centre[0] - np.random.randint(10, halfLength)
        if startX < 0:
            startX = 0
        startY = centre[1] - np.random.randint(10, halfLength)
        if startY < 0:
            startY = 0
        endX = startX + sideLength
        endY = startY + sideLength

        # check if box fits on image, if not compute from max edge
        if endX > image.shape[1]:
            endX = image.shape[1]
            startX = image.shape[1] - sideLength
        if endY > image.shape[0]:
            endY = image.shape[0]
            startY = image.shape[0] - sideLength

        # use numpy array slicing to crop image and save
        weedPosTrain = image[startY:endY, startX:endX]
        fname = strftime("%Y%m%d-%H%M%S_") + 'N' + str(ID) + ".png"
        cv2.imwrite(os.path.join(saveDir, fname), weedPosTrain)
        cv2.rectangle(displayImage, (int(startX), int(startY)), (endX, endY), (255, 100, 100), 3)