#!/usr/bin/env python
from pycoral.adapters.common import input_size
from pycoral.adapters.detect import get_objects
from pycoral.utils.dataset import read_label_file
from pycoral.utils.edgetpu import make_interpreter
from pycoral.utils.edgetpu import run_inference
from pathlib import Path

import cv2


class GreenOnGreen:
    def __init__(self, model_path='models', label_file='models/labels.txt'):
        if model_path is None:
            print('[WARNING] No model directory or path provided with --model-path flag. '
                  'Attempting to load from default...')
            model_path = 'models'
        self.model_path = Path(model_path)

        if self.model_path.is_dir():
            model_files = list(self.model_path.glob('*.tflite'))
            if not model_files:
                raise FileNotFoundError('No .tflite model files found. Please provide a directory or .tflite file.')

            else:
                self.model_path = model_files[0]
                print(f'[INFO] Using {self.model_path.stem} model...')

        elif self.model_path.suffix == '.tflite':
            print(f'[INFO] Using {self.model_path.stem} model...')

        else:
            print(f'[WARNING] Specified model path {model_path} is unsupported, attempting to use default...')

            model_files = Path('models').glob('*.tflite')
            try:
                self.model_path = next(model_files)
                print(f'[INFO] Using {self.model_path.stem} model...')

            except StopIteration:
                print('[ERROR] No model files found.')

        self.labels = read_label_file(label_file)
        self.interpreter = make_interpreter(self.model_path.as_posix())
        self.interpreter.allocate_tensors()
        self.inference_size = input_size(self.interpreter)
        self.objects = None

    def inference(self, image, confidence=0.5, filter_id=0):
        cv2_im_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        cv2_im_rgb = cv2.resize(cv2_im_rgb, self.inference_size)
        run_inference(self.interpreter, cv2_im_rgb.tobytes())
        self.objects = get_objects(self.interpreter, confidence)
        self.filter_id = filter_id

        height, width, channels = image.shape
        scale_x, scale_y = width / self.inference_size[0], height / self.inference_size[1]
        self.weed_centers = []
        self.boxes = []

        for det_object in self.objects:
            if det_object.id == self.filter_id:
                bbox = det_object.bbox.scale(scale_x, scale_y)

                startX, startY = int(bbox.xmin), int(bbox.ymin)
                endX, endY = int(bbox.xmax), int(bbox.ymax)
                boxW = endX - startX
                boxH = endY - startY

                # save the bounding box
                self.boxes.append([startX, startY, boxW, boxH])
                # compute box center
                centerX = int(startX + (boxW / 2))
                centerY = int(startY + (boxH / 2))
                self.weed_centers.append([centerX, centerY])

                percent = int(100 * det_object.score)
                label = f'{percent}% {self.labels.get(det_object.id, det_object.id)}'
                cv2.rectangle(image, (startX, startY), (endX, endY), (0, 0, 255), 2)
                cv2.putText(image, label, (startX, startY + 30),
                                     cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 0, 0), 2)
            else:
                pass
        # print(self.weedCenters)
        return None, self.boxes, self.weed_centers, image







