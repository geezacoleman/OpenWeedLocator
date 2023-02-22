import cv2
import numpy as np
import pywt

# developed with the help of Chat-GPT!

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