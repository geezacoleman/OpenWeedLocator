# Model directory for .tflite files

## Google Coral Installation - Raspberry Pi
In addition to the other software installation to get the OpenWeedLocator running, you will also need to install the Google Coral supporting software onto the Raspberry Pi. Simply run `install_coral.sh` from the command line using the instructions below. Firstly, navigate to this directory with:

`pi@raspberrypi:~ $ cd ~/owl/models`

Then, run the installation file:

`pi@raspberrypi:~ $ chmod +x install_coral.sh && ./install_coral.sh`.

During the installation, you will be asked to confirm options and connect the Google Coral USB to the USB3.0 ports (blue). For full instructions on the installation process, check out the Google Coral [documentation](https://coral.ai/docs/accelerator/get-started/).

## Training/exporting detection models for inference with the Coral
Running weed recognition models on the Google Coral requires the generation of a .tflite model file. The .tflite files are specifically designed to be lightweight and efficient, making them well-suited for deployment on edge devices like the Coral USB TPU. One important thing to note is that .tflite files for the Google Coral are specifically optimized for it, so you cannot simply use any .tflite file. Using a generic .tflite file may result in much slower performance or even failure to run.

![image](https://user-images.githubusercontent.com/51358498/226113545-9b642d75-f611-4ff5-a613-5e684822e619.png)

To test if the installation has worked, the recommended option is to download a generic model file first from the [Coral model repository](https://coral.ai/models/object-detection/).

Once you have confirmed it is working, there are two main ways to generate optimized, weed recognition .tflite files for the Coral. 

### Train a model using Tensorflow
These instructions by EdjeElectronics provide a step-by-step to a working .tflite Edge TPU model file. 
* [Google Colab walkthrough](https://colab.research.google.com/github/EdjeElectronics/TensorFlow-Lite-Object-Detection-on-Android-and-Raspberry-Pi/blob/master/Train_TFLite2_Object_Detction_Model.ipynb)
* [Accompanying YouTube video](https://www.youtube.com/watch?v=XZ7FYAMCc4M&ab_channel=EdjeElectronics)

### Train a YOLO v5/v8 model and export as .tflite 
** NOTE ** it appears this method isn't currently working consistently. Once this resolves, this will be the recommended approach, given the ease of training for YOLO models and the relatively high performance. You can track one of the issues on the Ultralytics repository [here](https://github.com/ultralytics/ultralytics/issues/1185).

To train a YOLOv5 model from Weed-AI, check out this notebook we have for [Weed-AI datasets](https://colab.research.google.com/github/Weed-AI/Weed-AI/blob/master/weed_ai_yolov5.ipynb)). Once it is trained, you must export it using either of the following commands:

#### YOLOv5
`!python export.py --weights path/to/your/weights/best.pt --include edgetpu`
#### YOLOv8
`!yolo export model=path/to/your/weights/best.pt format=edgetpu`

The full explanation for each method is available in the [Ultralytics YOLOv5](https://github.com/ultralytics/yolov5)
or [Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics) repositories.

Currently, the `GreenOnGreen` class will simply either load the first (alphabetically) model in the directory if specified with
`algorithm='gog'` or will load the model specified if `algorithm=path/to/model.tflite`. Importantly, all your classes must
appear in the `labels.txt` file.

This is a very early version of the approach, so it is subject to change.

## References
These are some of the sources used in the development of this aspect of the project.

1. [PyImageSearch](https://pyimagesearch.com/2019/05/13/object-detection-and-image-classification-with-google-coral-usb-accelerator/)
2. [Google Coral Guides](https://coral.ai/docs/accelerator/get-started/)
