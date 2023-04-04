from time import strftime
import numpy as np
import cv2
import os


def whole_image_save(image, save_directory, frame_id):
    fname = f"{strftime('%Y%m%d-%H%M%S_')}_frame_{frame_id}.png"
    cv2.imwrite(os.path.join(save_directory, fname), image)


def bounding_box_image_sample(image, bounding_boxes, save_directory, frame_id):
    '''
    Generates and saves a cropped section of whole image based on bbox coordinates
    :param image: input image array
    :param bounding_boxes: bounding box coordinates in list of form [[startX, startY, boxW, boxH], [...]]
    :param saveDir: save directory
    '''
    for contour_id, box in enumerate(bounding_boxes):
        startX = box[0]
        startY = box[1]
        endX = startX + box[2]
        endY = startY + box[3]

        cropped_image = image[startY:endY, startX:endX]
        fname = f"{strftime('%Y%m%d-%H%M%S_')}_frame_{frame_id}_n_{str(contour_id)}.png"
        cv2.imwrite(os.path.join(save_directory, fname), cropped_image)


def square_image_sample(image, centres_list, save_directory, frame_id, side_length=200):
    """
    Generates and saves random square image crop around a target centre
    :param image: input image to collect snapshot from
    :param centresList: list of target centres
    :param sideLength: dimensions of square
    """
    if side_length > image.shape[0]:
        side_length = image.shape[0]
    halfLength = int(side_length / 2)

    # compute startX and StartY of the cropped area
    for contour_id, centre in enumerate(centres_list):
        startX = centre[0] - np.random.randint(10, halfLength)
        if startX < 0:
            startX = 0
        startY = centre[1] - np.random.randint(10, halfLength)
        if startY < 0:
            startY = 0
        endX = startX + side_length
        endY = startY + side_length

        # check if box fits on image, if not compute from max edge
        if endX > image.shape[1]:
            endX = image.shape[1]
            startX = image.shape[1] - side_length
        if endY > image.shape[0]:
            endY = image.shape[0]
            startY = image.shape[0] - side_length

        # use numpy array slicing to crop image and save
        square_image = image[startY:endY, startX:endX]
        fname = f"{strftime('%Y%m%d-%H%M%S_')}_frame_{frame_id}_n_{str(contour_id)}.png"
        cv2.imwrite(os.path.join(save_directory, fname), square_image)
