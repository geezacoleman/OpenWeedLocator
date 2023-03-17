# Model directory for .tflite files

## Google Coral Installation - Raspberry Pi
In addition to the other software installation to get the OpenWeedLocator running, you will also need to install the Google Coral supporting software onto the Raspberry Pi. Simply run `install_coral.sh` from the command line using the instructions below. Firstly, navigate to this directory with:

`pi@raspberrypi:~ $ cd ~/owl/models`

Then, run the installation file:

`pi@raspberrypi:~ $ chmod +x install_coral.sh && ./install_coral.sh`.

During the installation, you will be asked to confirm options and connect the Google Coral USB to the USB3.0 ports (blue). For full instructions on the installation process, check out the Google Coral [documentation](https://coral.ai/docs/accelerator/get-started/).

## Training/exporting detection models for inference with the Coral
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
