import time

import os
import cv2
from imutils.video import FileVideoStream

class FrameReader:
    def __init__(self, path, resolution=(640, 480), loop_time=5):
        '''
        FrameReader allows users to provide a directory of images, video or a single image to OWL for testing
        and visualisation purposes.
        :param path: path to the media (single image, directory of images or video)
        :param loop_time: the delay between image display if using a directory)
        '''

        self.loop_time = loop_time
        self.loop_start_time = time.time()
        self.resolution = resolution
        self.curr_image = None
        self.files = None

        if os.path.isdir(path):
            self.files = iter(os.listdir(path))
            self.path = path
            self.cam = None
            self.input_type = "directory"
            self.single_image = False

        elif os.path.isfile(path):
            if path.endswith(('.png', '.jpg', '.jpeg')):
                self.cam = cv2.resize(cv2.imread(path), self.resolution, interpolation=cv2.INTER_AREA)
                self.input_type = "image"
                self.single_image = True

            else:
                self.cam = FileVideoStream(path).start()
                self.input_type = "video"
                self.single_image = False
        else:
            raise ValueError(f'[ERROR] Invalid path to image/s: {path}')

    def read(self):
        if self.single_image:
            return self.cam

        elif self.files:
            if self.curr_image is None or (time.time() - self.loop_start_time) > self.loop_time:
                try:
                    image = next(self.files)
                    self.curr_image = cv2.imread(os.path.join(self.path, image))
                    self.curr_image = cv2.resize(self.curr_image, self.resolution, interpolation=cv2.INTER_AREA)

                    self.loop_start_time = time.time()

                except StopIteration:
                    self.files = iter(os.listdir(self.path))  # restart from first image
                    return self.read()

            return self.curr_image

        else:
            frame = self.cam.read()
            frame = cv2.resize(frame, self.resolution, interpolation=cv2.INTER_AREA)

            return frame

    def reset(self):
        if self.input_type == "directory":
            # reset the iterator to the beginning of the directory
            self.files = iter(os.listdir(self.path))
            self.curr_image = None

        elif self.input_type == "video":
            # stop the current video stream and start a new one
            self.cam.stop()
            self.cam = FileVideoStream(self.path).start()

        self.loop_start_time = time.time()  # reset the loop timer

    def stop(self):
        if not self.single_image and self.cam:
            self.cam.stop()



# # this enables any input of images to use - directory of images, single image, video
# class FrameReader:
#     def __init__(self, path, delay=10):
#         '''
#         FrameReader allows users to provide a directory of images, video or a single image to OWL for testing
#         and visualisation purposes.
#         :param path: path to the media (single image, directory of images or video)
#         :param delay: the delay between image display if using a directory)
#         '''
#         self.delay = delay
#         self.input_type = None
#
#         if os.path.isdir(path):
#             self.files = iter(os.listdir(path))
#             self.path = path
#             self.cam = None
#             self.single_image = False
#             self.input_type = "image directory"
#
#         elif os.path.isfile(path):
#             if path.endswith(('.png', '.jpg', '.jpeg')):
#                 self.cam = cv2.imread(path)
#                 self.single_image = True
#                 self.input_type = "single image"
#
#             else:
#                 self.cam = FileVideoStream(path).start()
#                 self.single_image = False
#                 self.input_type = "video"
#
#         else:
#             raise ValueError(f'[ERROR] Invalid path to image/s: {path}')
#
#         if self.cam is not None and not self.single_image:
#             self.frame_width = self.cam.stream.get(cv2.CAP_PROP_FRAME_WIDTH)
#             self.frame_height = self.cam.stream.get(cv2.CAP_PROP_FRAME_HEIGHT)
#
#     def read(self):
#         if self.single_image:
#             return self.cam
#
#         elif self.files:
#             try:
#                 image = next(self.files)
#                 frame = cv2.imread(os.path.join(self.path, image))
#                 return frame
#
#             except StopIteration:
#                 self.files = iter(os.listdir(self.path))  # restart from first image
#                 return self.read()
#
#         else:
#             return self.cam.read()
#
#     def stop(self):
#         if not self.single_image and self.cam:
#             self.cam.stop()
#
#     def reset(self):
#         if self.input_type == "directory":
#             # reset the iterator to the beginning of the directory
#             self.files = iter(os.listdir(self.path))
#         elif self.input_type == "video":
#             # stop the current video stream and start a new one
#             self.cam.stop()
#             self.cam = FileVideoStream(self.path).start()
#
