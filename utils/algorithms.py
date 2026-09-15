import numpy as np
import cv2

### Adding a new algorithm ###
"""
To add a new algorithm the only requirement is that it accepts a BGR (opencv) image and returns a grayscale
image as an output. If it returns a binary image (like hsv) then it must return a boolean True in addition to the image
as it has already been thresholded.
"""


##############################

def exg(image):
    """
    Takes an image and processes it using ExG. Returns a single channel exG output.
    Developed by Woebbecke et al. 1995.
    :return: grayscale image
    """
    # OPTIMIZED: cv2.split is faster than array slicing when we need copies
    blue, green, red = cv2.split(image)
    blue = blue.astype(np.float32)
    green = green.astype(np.float32)
    red = red.astype(np.float32)

    image_out = 2 * green - red - blue
    # Clip to 0-255 range then convert (convertScaleAbs takes absolute value)
    np.clip(image_out, 0, 255, out=image_out)
    image_out = image_out.astype('uint8')

    return image_out


def maxg(image):
    '''
    Takes an input image in int8 format and calculates the 'maxg' algorithm based on the following publication:
    'Weed Identification Using Deep Learning and Image Processing in Vegetable Plantation', Jin et al. 2021
    :param image: image as a BGR array (i.e. opened with opencv not PIL)
    :return: grayscale image
    '''
    # OPTIMIZED: cv2.split for channel extraction
    blue, green, red = cv2.split(image)
    blue = blue.astype(np.float32)
    green = green.astype(np.float32)
    red = red.astype(np.float32)

    image_out = 24 * green - 19 * red - 2 * blue
    image_out = (image_out / np.amax(image_out)) * 255  # scale image between 0 - 255
    image_out = image_out.astype('uint8')

    return image_out


def exg_standardised(image):
    '''
    Takes an input image in int8 format and calculates the standardised ExG algorithm
    :param image: image as a BGR array (i.e. opened with opencv not PIL)
    :return: returns a grayscale image
    '''
    blue, green, red = cv2.split(image)
    blue = blue.astype(np.float32)
    green = green.astype(np.float32)
    red = red.astype(np.float32)

    channel_sum = red + green + blue
    np.maximum(channel_sum, 1.0, out=channel_sum)

    # Single division: (2g - r - b) / sum (reuse green buffer in-place)
    np.multiply(green, 2.0, out=green)
    np.subtract(green, red, out=green)
    np.subtract(green, blue, out=green)
    np.divide(green, channel_sum, out=green)
    np.multiply(green, 255.0, out=green)
    np.clip(green, 0, 255, out=green)
    image_out = green.astype('uint8')

    return image_out


def exg_standardised_hue(image,
                         hue_min=30,
                         hue_max=90,
                         brightness_min=10,
                         brightness_max=220,
                         saturation_min=30,
                         saturation_max=255,
                         invert_hue=False):
    '''
    Takes an image and performs a combined ExG + HSV algorithm
    :param image: image as a BGR array (i.e. opened with opencv not PIL)
    :param hue_min: minimum hue value
    :param hue_max: maximum hue value
    :param brightness_min: minimum 'value' or brightness value
    :param brightness_max: maximum 'value' or brightness value
    :param saturation_min: minimum saturation
    :param saturation_max: maximum saturation
    :param invert_hue: inverts the hue threshold to exclude anything within the thresholds
    :return: returns a grayscale image
    '''
    blue, green, red = cv2.split(image)
    blue = blue.astype(np.float32)
    green = green.astype(np.float32)
    red = red.astype(np.float32)

    channel_sum = red + green + blue
    np.maximum(channel_sum, 1.0, out=channel_sum)

    # Single division: (2g - r - b) / sum (reuse green buffer in-place)
    np.multiply(green, 2.0, out=green)
    np.subtract(green, red, out=green)
    np.subtract(green, blue, out=green)
    np.divide(green, channel_sum, out=green)
    np.multiply(green, 255.0, out=green)
    np.clip(green, 0, 255, out=green)
    image_out = green.astype('uint8')

    # OPTIMIZED: single inRange on 3-channel HSV (biggest speedup ~1.5x)
    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    if not invert_hue:
        lower = np.array([hue_min, saturation_min, brightness_min], dtype=np.uint8)
        upper = np.array([hue_max, saturation_max, brightness_max], dtype=np.uint8)
        hsv_thresh = cv2.inRange(hsv_image, lower, upper)
    else:
        # For inverted hue, select pixels outside the hue range
        lower1 = np.array([0, saturation_min, brightness_min], dtype=np.uint8)
        upper1 = np.array([hue_min, saturation_max, brightness_max], dtype=np.uint8)
        lower2 = np.array([hue_max, saturation_min, brightness_min], dtype=np.uint8)
        upper2 = np.array([180, saturation_max, brightness_max], dtype=np.uint8)
        mask1 = cv2.inRange(hsv_image, lower1, upper1)
        mask2 = cv2.inRange(hsv_image, lower2, upper2)
        hsv_thresh = cv2.bitwise_or(mask1, mask2)

    image_out = hsv_thresh & image_out

    return image_out


def hsv(image,
        hue_min=30,
        hue_max=90,
        brightness_min=10,
        brightness_max=220,
        saturation_min=30,
        saturation_max=255,
        invert_hue=False):
    """
    Performs an HSV thresholding operation on the input image
    :param image: image as a BGR array (i.e. opened with opencv not PIL)
    :param hue_min: minimum hue threshold
    :param hue_max: maximum hue threshold
    :param brightness_min: minimum 'brightness' or 'value' threshold
    :param brightness_max: maximum 'brightness' or 'value' threshold
    :param saturation_min: minimum saturation threshold
    :param saturation_max: maximum saturation threshold
    :param invert_hue: inverts the hue threshold to exclude anything within the thresholds
    :return: returns a binary image and boolean thresholded or not
    """
    # OPTIMIZED: single inRange on 3-channel HSV instead of 3 separate calls
    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    if not invert_hue:
        lower = np.array([hue_min, saturation_min, brightness_min], dtype=np.uint8)
        upper = np.array([hue_max, saturation_max, brightness_max], dtype=np.uint8)
        out_thresh = cv2.inRange(hsv_image, lower, upper)
    else:
        # For inverted hue, select pixels outside the hue range
        lower1 = np.array([0, saturation_min, brightness_min], dtype=np.uint8)
        upper1 = np.array([hue_min, saturation_max, brightness_max], dtype=np.uint8)
        lower2 = np.array([hue_max, saturation_min, brightness_min], dtype=np.uint8)
        upper2 = np.array([180, saturation_max, brightness_max], dtype=np.uint8)
        mask1 = cv2.inRange(hsv_image, lower1, upper1)
        mask2 = cv2.inRange(hsv_image, lower2, upper2)
        out_thresh = cv2.bitwise_or(mask1, mask2)

    return out_thresh, True


def fft_blur(image, size=60):
    """
    Adapted from:
    https://pyimagesearch.com/2020/06/15/opencv-fast-fourier-transform-fft-for-blur-detection-in-images-and-video-streams/
    """
    (h, w) = image.shape
    (cX, cY) = (int(w / 2.0), int(h / 2.0))
    fft = np.fft.fft2(image)
    fftShift = np.fft.fftshift(fft)

    fftShift[cY - size:cY + size, cX - size:cX + size] = 0
    fftShift = np.fft.ifftshift(fftShift)
    recon = np.fft.ifft2(fftShift)

    magnitude = 20 * np.log(np.abs(recon))
    mean = np.mean(magnitude)

    return mean
