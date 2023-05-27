import cv2
import numpy as np
import pywt

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