# Model directory for .tflite files

Once you have trained and exported your weed detection model (check out this notebook we have for [Weed-AI datasets](https://colab.research.google.com/github/Weed-AI/Weed-AI/blob/master/weed_ai_yolov5.ipynb)), 
you must export it using the command: 

#### YOLOv5
`!python export.py --weights path/to/your/weights/best.pt --include edgetpu`
#### YOLOv8
`!yolo export model=path/to/your/weights/best.pt format=edgetpu`

The full explanation for each method is available in the [Ultralytics YOLOv5](https://github.com/ultralytics/yolov5)
or [Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics) repositories

Currently, the `GreenOnGreen` class will simply either load the first (alphabetically) model in the directory if specified with
`algorithm='gog'` or will load the model specified if `algorithm=path/to/model.tflite`. Importantly, all your classes must
appear in the `labels.txt` file.

This is a very early version of the approach, so it is subject to change.

## References
These are some of the sources used in the development of this aspect of the project.

1. [PyImageSearch](https://pyimagesearch.com/2019/05/13/object-detection-and-image-classification-with-google-coral-usb-accelerator/)
2. [Google Coral Guides](https://coral.ai/docs/accelerator/get-started/)