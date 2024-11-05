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
    # using array slicing to split into channels
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)
    # cv2.imshow('blue', blue.astype('uint8'))
    # cv2.imshow('green', green.astype('uint8'))
    # cv2.imshow('red', red.astype('uint8'))

    image_out = 2 * green - red - blue
    image_out = np.clip(image_out, 0, 255)
    image_out = image_out.astype('uint8')

    # cv2.imshow('ExG', imgOut)
    return image_out

def maxg(image):
    '''
    Takes an input image in int8 format and calculates the 'maxg' algorithm based on the following publication:
    'Weed Identification Using Deep Learning and Image Processing in Vegetable Plantation', Jin et al. 2021
    :param image: image as a BGR array (i.e. opened with opencv not PIL)
    :return: grayscale image
    '''
    # using array slicing to split into channels with float32 for calculation
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)

    image_out = 24 * green - 19 * red - 2 * blue
    image_out = (image_out / np.amax(image_out)) * 255 # scale image between 0 - 255
    image_out = image_out.astype('uint8')

    return image_out

def exg_standardised(image):
    '''
    Takes an input image in int8 format and calculates the standardised ExG algorithm
    :param image: image as a BGR array (i.e. opened with opencv not PIL)
    :return: returns a grayscale image
    '''
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
    # cv2.imshow('ExG Standardised', imgOut)

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

    hsv_thresh, _ = hsv(image,
                       hue_min=hue_min, hue_max=hue_max,
                       brightness_min=brightness_min, brightness_max=brightness_max,
                       saturation_min=saturation_min, saturation_max=saturation_max,
                       invert_hue=invert_hue)
    image_out = hsv_thresh & image_out
    # cv2.imshow('exhu', imgOut)

    return image_out

def exgr(image):
    '''
    performs the ExGR algorithm on the input image
    :param image: image as a BGR array (i.e. opened with opencv not PIL)
    :return: returns a grayscale image
    '''
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)

    exg_image = exg(image)
    image_out = exg_image - (1.4 * red - green)

    image_out = np.clip(image_out, 0, 255)
    image_out = image_out.astype('uint8')

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

    image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue = image[:, :, 0]
    sat = image[:, :, 1]
    val = image[:, :, 2]

    hue_thresh = cv2.inRange(hue, hue_min, hue_max)
    sat_thresh = cv2.inRange(sat, saturation_min, saturation_max)
    val_thresh = cv2.inRange(val, brightness_min, brightness_max)

    # allow users to select purple/red colour ranges by excluding green
    if invert_hue:
        hue_thresh = cv2.bitwise_not(hue_thresh)

    out_thresh = sat_thresh & val_thresh & hue_thresh
    # cv2.imshow('HSV Out', outThresh)
    return out_thresh, True

# for NIR images only
def gndvi(image):
    """
    Takes an image and processes it using GNDVI. Returns a single channel grayscale scaled output.
    :return:
    """
    # using array slicing to split into channel
    green = image[:, :, 1].astype(np.float32)
    NIR = image[:, :, 2].astype(np.float32)

    image_out = (NIR - green) / (NIR + green)
    image_out = cv2.normalize(image_out, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
    image_out = image_out.astype('uint8')
    cv2.imshow('gndvi', image_out)
    return image_out


# Other vegetation indices are listed here, but have NOT been tested.
def veg(image):
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)

    image_out = green / ((red ** 0.667) * (blue ** 0.333))
    image_out = cv2.normalize(image_out, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
    image_out = np.clip(image_out, 0, 255)
    image_out = image_out.astype('uint8')

    return image_out

def cive(image):
    blue = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    red = image[:, :, 2].astype(np.float32)

    image_out = 0.441 * red - 0.881 * green + 0.385 * blue + 18.78745
    #image_out = cv2.normalize(imgOut, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
    image_out = np.clip(image_out, 0, 255)
    image_out = image_out.astype('uint8')

    return image_out

def clahe_sat_val(image):
    image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue = image[:, :, 0]
    sat = image[:, :, 1]
    val = image[:, :, 2]

    clahe = cv2.createCLAHE(clipLimit=20, tileGridSize=(64,64))
    satCL = clahe.apply(sat)
    valCL = clahe.apply(val)

    claheImage = cv2.merge([hue, satCL, valCL])
    claheImage = cv2.cvtColor(claheImage, cv2.COLOR_HSV2BGR)
    #cv2.imshow('CLAHE', claheImage)
    return claheImage

def dgci(image):
    image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    hue = image[:, :, 0].astype(np.float32)
    sat = image[:, :, 1].astype(np.float32)
    val = image[:, :, 2].astype(np.float32)

    np.seterr(divide='ignore', invalid='ignore')
    imgOut = ((hue - 60)/(60 + (1 - sat) + (1 - val)))/3

    imgOut = imgOut.astype('uint8')
    imgOut = cv2.normalize(imgOut, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)

    return imgOut

##### BLUR ALGORITHMS
# some algorithms developed with the help of Chat-GPT!
# used before passing image into blur algorithms
def normalize_brightness(image, intensity=0.8):
    img_yuv = cv2.cvtColor(image, cv2.COLOR_BGR2YUV)
    img_yuv[:, :, 0] = cv2.equalizeHist(img_yuv[:, :, 0])
    img_yuv[:, :, 0] = np.clip(intensity * img_yuv[:, :, 0], 0, 255)
    normalized = cv2.cvtColor(img_yuv, cv2.COLOR_YUV2BGR)

    # Return the normalized image
    #stacked = np.hstack((image, normalized))
    #cv2.imshow('normalised', stacked)
    #cv2.waitKey(0)

    return normalized

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

def laplacian_blur(image):
    grey = cv2.cvtColor(image.copy(), cv2.COLOR_BGR2GRAY)
    blurriness = cv2.Laplacian(grey, cv2.CV_64F).var()

    return blurriness


def variance_of_gradient_blur(image):
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sobelx = cv2.Sobel(grey, cv2.CV_64F, 1, 0, ksize=3)
    sobely = cv2.Sobel(grey, cv2.CV_64F, 0, 1, ksize=3)
    gradient_magnitude = np.sqrt(np.square(sobelx) + np.square(sobely))
    blurriness = np.var(gradient_magnitude)

    return blurriness


def tenengrad_blur(image):
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sobelx = cv2.Sobel(grey, cv2.CV_64F, 1, 0, ksize=5)
    sobely = cv2.Sobel(grey, cv2.CV_64F, 0, 1, ksize=5)
    gradient_magnitude = np.sqrt(np.square(sobelx) + np.square(sobely))
    blurriness = np.sum(np.square(gradient_magnitude)) / (grey.shape[0] * grey.shape[1])

    return blurriness


def entropy_blur(image):
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hist = cv2.calcHist([grey], [0], None, [256], [0, 256])
    hist_norm = hist / (grey.shape[0] * grey.shape[1])
    hist_norm = hist_norm[hist_norm != 0]
    blurriness = -np.sum(hist_norm * np.log2(hist_norm))

    return blurriness


def wavelet_blur(image):
    import pywt
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    coeffs = pywt.dwt2(grey, 'haar')
    LL, (LH, HL, HH) = coeffs
    blurriness = np.sum(np.square(LL)) / (grey.shape[0] * grey.shape[1])

    return blurriness


def gradient_blur(image):
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sobelx = cv2.Sobel(grey, cv2.CV_64F, 1, 0, ksize=3)
    sobely = cv2.Sobel(grey, cv2.CV_64F, 0, 1, ksize=3)
    gradient_magnitude = np.sqrt(np.square(sobelx) + np.square(sobely))
    blurriness = np.sum(gradient_magnitude) / (grey.shape[0] * grey.shape[1])

    return blurriness