# Adding Green-on-Green to the OWL (beta)
Welcome to the first iteration of Green-on-Green or in-crop weed detection with the OWL. This is still an early beta version, so it may require additional troubleshooting. It has been tested and works on both a Raspberry Pi 4, LibreComputer and a Windows desktop computer.

## Stage 1| Hardware/Software - Google Coral Installation
In addition to the other software installation to get the OpenWeedLocator running, you will also need to install the Google Coral supporting software onto the Raspberry Pi. Simply run `install_coral.sh` from the command line using the instructions below. 

### Step 1
Assuming you have cloned the OpenWeedLocator repository and renamed it to `owl`, navigate to the `models` directory on the Raspberry Pi with:

`owl@raspberrypi:~ $ cd ~/owl/models`

### Step 2
Now run the installation file. This will install the `pycoral` library and other important packages to run the Coral. For full instructions on the installation process, we recommend reading  the Google Coral [documentation](https://coral.ai/docs/accelerator/get-started/).

During the installation, you will be asked to confirm performance options and connect the Google Coral USB to the USB3.0 ports (blue). 

`owl@raspberrypi:~ $ chmod +x install_coral.sh && ./install_coral.sh`.

If you run into errors during the `pycoral` library installation, try running 

```
owl@raspberrypi:~ $ workon owl
(owl) owl@raspberrypi:~/owl/models$ pip install pycoral
```

### Step 3
The final step is to test the installation.

Open up a Python terminal by running:
```
(owl) owl@raspberrypi:~/owl/models$ python
```

Now try running:
```
>>> import pycoral
```

If this runs successfully then you're ready to move on to the next step and running object detection models with the OWL.

## Stage 2 | Model Training/Deployment - Inference with the Coral
Running weed recognition models on the Google Coral requires the generation of a .tflite model file. The .tflite files are specifically designed to be lightweight and efficient, making them well-suited for deployment on edge devices like the Coral USB TPU. One important thing to note is that .tflite files for the Google Coral are specifically optimized for it, so you cannot simply use any .tflite file. Using a generic .tflite file may result in much slower performance or even failure to run.

This is an overview of the process from the official Google Coral documentation:
![image](https://user-images.githubusercontent.com/51358498/226113545-9b642d75-f611-4ff5-a613-5e684822e619.png)

### Step 1
To test if the installation has worked, the recommended option is to download a generic model file first from the [Coral model repository](https://coral.ai/models/object-detection/). This will isolate any issues with it running to the OWL or the Google Coral installation, rather than the model training. 

While still in the `models` directory, run this command to download the appropriate model:
```
(owl) owl@raspberrypi:~/owl/models$ wget https://raw.githubusercontent.com/google-coral/test_data/master/ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite
```

Now change back to the `owl` directory and try running `owl.py` and specifying `gog` for the algorithm. If you don't specify a path to the `.tflite` model file, it will automatically select the first model in the directory when sorted alphabetically.

**NOTE** If you are testing this inside, the camera settings will likely be too dark (and the image will appear entirely black) so you may also need to specify the `--exp-compensation 4` and `--exp-mode auto`. 

```
(owl) owl@raspberrypi:~/owl/models$ cd ..
(owl) owl@raspberrypi:~/owl$python owl.py --show-display --algorithm gog
```

If this runs correctly, a video feed just like the previous green-on-brown approach should appear with a red box around an 'object', which in this case has been filtered to only detect 'potted plants'. If you would like to detect any of the other COCO categories, simply change the `filter_id=63` to a different category. The full list is [available here](https://tech.amikelive.com/node-718/what-object-categories-labels-are-in-coco-dataset/).

Once you have confirmed it is working, you will need to start training and deploying your own weed recognition models.

There are two main ways to generate optimized, weed recognition .tflite files for the Coral. These are detailed below.

### Option 1 | Train a model using Tensorflow
These instructions by EdjeElectronics provide a step-by-step to a working .tflite Edge TPU model file. 
* [Google Colab walkthrough](https://colab.research.google.com/github/EdjeElectronics/TensorFlow-Lite-Object-Detection-on-Android-and-Raspberry-Pi/blob/master/Train_TFLite2_Object_Detction_Model.ipynb)
* [Accompanying YouTube video](https://www.youtube.com/watch?v=XZ7FYAMCc4M&ab_channel=EdjeElectronics)

There is also the [official Google Colab tutorial](https://colab.research.google.com/github/google-coral/tutorials/blob/master/retrain_ssdlite_mobiledet_qat_tf1.ipynb) from the Coral documentation, that walks you through the entire training process for custom datasets.

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
