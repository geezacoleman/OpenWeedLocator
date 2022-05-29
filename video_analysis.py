from greenonbrown import green_on_brown
from imutils.video import count_frames, FileVideoStream
import pandas as pd
import numpy as np
import imutils
import glob
import cv2
import csv
import os


def four_frame_analysis(exgFile: str, exgsFile: str, hueFile: str, exhuFile: str, HDFile: str):
    baseName = os.path.splitext(os.path.basename(exhuFile))[0]

    exgVideo = cv2.VideoCapture(exgFile)
    print("[INFO] Loaded {}".format(exgFile))
    lenexg = count_frames(exgFile, override=True) - 1

    exgsVideo = cv2.VideoCapture(exgsFile)
    print("[INFO] Loaded {}".format(exgsFile))
    lenexgs = count_frames(exgsFile, override=True) - 1

    hueVideo = cv2.VideoCapture(hueFile)
    print("[INFO] Loaded {}".format(hueFile))
    lenhue = count_frames(hueFile, override=True) - 1

    exhuVideo = cv2.VideoCapture(exhuFile)
    print("[INFO] Loaded {}".format(exhuFile))
    lenexhu = count_frames(exhuFile, override=True) - 1

    videoHD = cv2.VideoCapture(HDFile)
    print("[INFO] Loaded {}".format(HDFile))
    lenHD = count_frames(HDFile, override=True) - 1

    hdFrame = None
    exgFrame = None
    exgsFrame = None
    hueFrame = None
    exhuFrame = None

    hdframecount = 0
    exgframecount = 0
    exgsframecount = 0
    hueframecount = 0
    exhuframecount = 0

    hdFramesAll = []
    exgFramesAll = []
    exgsFramesAll = []
    hueFramesAll = []
    exhuFramesAll = []

    while True:
        k = cv2.waitKey(1) & 0xFF
        if k == ord('v') or hdFrame is None:
            if hdframecount >= len(hdFramesAll):
                hdFrame = next(frame_processor(videoHD, 'hd'))
                hdFrame = imutils.resize(hdFrame, height=640)
                hdFrame = imutils.rotate(hdFrame, angle=180)
                hdframecount += 1
                hdFramesAll.append(hdFrame)
            else:
                hdFrame = hdFramesAll[hdframecount]
                hdframecount += 1

        if k == ord('q') or exgFrame is None:
            if exgframecount >= len(exgFramesAll):
                exgFrame = next(frame_processor(exgVideo, 'exg'))
                exgframecount += 1
                exgFramesAll.append(exgFrame)
            else:
                exgFrame = exgFramesAll[exgframecount]
                exgframecount += 1

        if k == ord('w') or exgsFrame is None:
            if exgsframecount >= len(exgsFramesAll):
                exgsFrame = next(frame_processor(exgsVideo, 'exgs'))
                exgsframecount += 1
                exgsFramesAll.append(exgsFrame)
            else:
                exgsFrame = exgsFramesAll[exgsframecount]
                exgsframecount += 1

        if k == ord('e') or hueFrame is None:
            if hueframecount >= len(hueFramesAll):
                hueFrame = next(frame_processor(hueVideo, 'hsv'))
                hueframecount += 1
                hueFramesAll.append(hueFrame)
            else:
                hueFrame = hueFramesAll[hueframecount]
                hueframecount += 1

        if k == ord('r') or exhuFrame is None:
            if exhuframecount >= len(exhuFramesAll):
                exhuFrame = next(frame_processor(exhuVideo, 'exhu'))
                exhuframecount += 1
                exhuFramesAll.append(exhuFrame)
            else:
                exhuFrame = exhuFramesAll[exhuframecount]
                exhuframecount += 1

        if k == ord('b'):
            if hdframecount > 0:
                hdframecount -= 1
                hdFrame = hdFramesAll[hdframecount]
            else:
                hdFrame = hdFramesAll[hdframecount]

        if k == ord('a'):
            if exgframecount > 0:
                exgframecount -= 1
                exgFrame = exgFramesAll[exgframecount]
            else:
                exgFrame = exgFramesAll[exgframecount]

        if k == ord('s'):
            if exgsframecount > 0:
                exgsframecount -= 1
                exgsFrame = exgsFramesAll[exgsframecount]
            else:
                exgsFrame = exgsFramesAll[exgsframecount]

        if k == ord('d'):
            if hueframecount > 0:
                hueframecount -= 1
                hueFrame = hueFramesAll[hueframecount]
            else:
                hueFrame = hueFramesAll[hueframecount]

        if k == ord('f'):
            if exhuframecount > 0:
                exhuframecount -= 1
                exhuFrame = exhuFramesAll[exhuframecount]
            else:
                exhuFrame = exhuFramesAll[exhuframecount]

        # save current frames for the video comparison
        if k == ord('y'):
            cv2.imwrite('images/frameGrabs/{}_frame{}_exg.png'.format(baseName, exgframecount), exgFrame)
            cv2.imwrite('images/frameGrabs/{}_frame{}_exgs.png'.format(baseName, exgsframecount), exgsFrame)
            cv2.imwrite('images/frameGrabs/{}_frame{}_hue.png'.format(baseName, hueframecount), hueFrame)
            cv2.imwrite('images/frameGrabs/{}_frame{}_exhu.png'.format(baseName, exhuframecount), exhuFrame)
            print('[INFO] All frames written.')

        # write text on each video frame
        exgVis = exgFrame.copy()
        exgsVis = exgsFrame.copy()
        hueVis = hueFrame.copy()
        exhuVis = exhuFrame.copy()

        cv2.putText(exhuVis, 'exhu: {} / {}'.format(exhuframecount, lenexhu), (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 0), 2)
        cv2.putText(hueVis, 'hue: {} / {}'.format(hueframecount, lenhue), (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 0), 2)
        cv2.putText(exgsVis, 'exgs: {} / {}'.format(exgsframecount, lenexgs), (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 0), 2)
        cv2.putText(exgVis, 'exg: {} / {}'.format(exgframecount, lenexg), (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 0), 2)
        cv2.putText(hdFrame, 'HD: {} / {}'.format(hdframecount, lenHD), (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 0), 2)

        # stack the video frames
        topRow = np.hstack((exgVis, exgsVis))
        bottomRow = np.hstack((hueVis, exhuVis))
        combined = np.vstack((topRow, bottomRow))
        combined = np.hstack((combined, hdFrame))

        cv2.imshow('Output', combined)

        if k == 27:
            break


def single_frame_analysis(videoFile: str, HDFile: str, algorithm):
    baseName = os.path.splitext(os.path.basename(videoFile))[0]

    video = cv2.VideoCapture(videoFile)
    print("[INFO] Loaded {}".format(video))
    lenVideo = count_frames(videoFile, override=True) - 1

    videoHD = cv2.VideoCapture(HDFile)
    print("[INFO] Loaded {}".format(HDFile))
    lenHD = count_frames(HDFile, override=True) - 1

    hdFrame = None
    videoFrame = None

    hdframecount = 0
    videoframecount = 0

    hdFramesAll = []
    videoFramesAll = []

    while True:
        k = cv2.waitKey(1) & 0xFF
        if k == ord('d') or hdFrame is None:
            if hdframecount >= len(hdFramesAll):
                hdFrame = next(frame_processor(videoHD, 'hd'))
                hdFrame = imutils.resize(hdFrame, height=640)
                # hdFrame = imutils.rotate(hdFrame, angle=180)
                hdframecount += 1
                hdFramesAll.append(hdFrame)
            else:
                hdFrame = hdFramesAll[hdframecount]
                hdframecount += 1

        if k == ord('s') or videoFrame is None:
            if videoframecount >= len(videoFramesAll):
                videoFrame = next(frame_processor(video, algorithm))
                videoFrame = imutils.resize(videoFrame, height=640)
                videoframecount += 1
                videoFramesAll.append(videoFrame)
            else:
                videoFrame = videoFramesAll[videoframecount]
                videoframecount += 1

        if k == ord('e'):
            if hdframecount > 0:
                hdframecount -= 1
                hdFrame = hdFramesAll[hdframecount]
            else:
                hdFrame = hdFramesAll[hdframecount]

        if k == ord('w'):
            if videoframecount > 0:
                videoframecount -= 1
                videoFrame = videoFramesAll[videoframecount]
            else:
                videoFrame = videoFramesAll[videoframecount]

        # save current frames for the video comparison
        if k == ord('y'):
            cv2.imwrite('images/frameGrabs/{}_frame{}_{}.png'.format(baseName, videoframecount, algorithm), videoFrame)
            print('[INFO] All frames written.')

        # write text on each video frame
        videoVis = videoFrame.copy()

        cv2.putText(videoVis, '!CHECK! -> {}: {} / {}'.format(algorithm, videoframecount, lenVideo), (50, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1,  (0, 255, 0), 2)
        cv2.putText(hdFrame, 'HD: {} / {}'.format(hdframecount, lenHD), (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1,
                    (0, 255, 0), 2)

        # stack the video frames
        combined = np.hstack((videoVis, hdFrame))
        cv2.putText(combined, 'Controls: "d/e" - HD fwd/back | "s/w" - vid fwd/back | ESC - quit | "y" - save frame'.format(algorithm, videoframecount, lenVideo), (450, 620),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        cv2.imshow('Output', combined)

        if k == 27:
            break


def frame_processor(videoFeed, videoName):
    frameShape = None
    while True:
        k = cv2.waitKey(1) & 0xFF
        ret, frame = videoFeed.read()

        if ret == False:
            frame = np.zeros(frameShape, dtype='uint8')

        if frameShape is None:
            frameShape = frame.shape

        if videoName == "hd":
            yield frame

        else:
            cnts, boxes, weedCentres, imageOut = green_on_brown(frame, exgMin=29,
                                                                exgMax=200,
                                                                hueMin=30,
                                                                hueMax=92,
                                                                saturationMin=10,
                                                                saturationMax=250,
                                                                brightnessMin=60,
                                                                brightnessMax=250,
                                                                show_display=False,
                                                                algorithm=videoName, minArea=10)

            yield imageOut
        if k == 27:
            videoFeed.stop()
            break


def blur_analysis(directory):
    blurDict = {}
    df = pd.DataFrame(columns=['field', 'algorithm', 'blur'])
    for videoPath in glob.iglob(directory + '\*.mp4'):
        allframeBlur = []
        sampledframeBlur = []
        video = FileVideoStream(videoPath).start()
        frameCount = 0
        while True:
            frame = video.read()
            if video.stopped:
                meanBlur = np.mean(allframeBlur)
                stdBlur = np.std(allframeBlur)
                vidName = os.path.basename(videoPath)
                fieldNameList = [vidName.split("-")[0] for i in range(100)]
                print(fieldNameList)
                algorithmNameList = [os.path.splitext(vidName.split("-")[2])[0] for i in range(100)]

                for i in range(100):
                    randint = np.random.randint(0, len(allframeBlur))
                    sampledframeBlur.append(allframeBlur[randint])
                df2 = pd.DataFrame(list(zip(fieldNameList, algorithmNameList, sampledframeBlur)),
                                   columns=['field', 'algorithm', 'blur'])
                print(df2)
                df = df.append(df2)
                print(df)
                df.to_csv(r"videos\blur\blurriness.csv")
                blurDict[vidName] = [meanBlur, stdBlur]
                break

            greyscale = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blurriness = cv2.Laplacian(greyscale, cv2.CV_64F).var()
            allframeBlur.append(blurriness)
            frameCount += 1

        print(vidName, ',', np.round(meanBlur, 2), ',', np.round(stdBlur, 2), ',', frameCount)

    print(blurDict)


if __name__ == "__main__":
    videoFile = r"videos/ard-1-30.avi"
    hdFile = r"videos/ard-5-15.avi"

    single_frame_analysis(videoFile=videoFile,
                          HDFile=hdFile,
                          algorithm='exg')

    # # blur analysis
    # directory = r"videos/blur"
    # blur_analysis(directory=directory)
