# Building an OWL Yocto Image

This guide outlines the basic steps for creating a custom Yocto image for running OpenWeedLocator (OWL) on Raspberry Pi models. These instructions assume familiarity with Yocto Project concepts and a Linux build host with the required dependencies installed.

## 1. Set up the Yocto environment

1. Create a working directory and clone the Yocto Project `poky` repository:
   ```bash
   git clone --branch kirkstone git://git.yoctoproject.org/poky poky
   ```
2. Clone the `meta-openembedded` collection and the `meta-raspberrypi` layer:
   ```bash
   cd poky
   git clone --branch kirkstone git://git.openembedded.org/meta-openembedded
   git clone --branch kirkstone https://github.com/agherzan/meta-raspberrypi
   ```
3. Source the build environment:
   ```bash
   source oe-init-build-env
   ```

## 2. Configure layers

Edit `conf/bblayers.conf` in the build directory to include the new layers:
```bash
BBLAYERS += "${BSPDIR}/meta-openembedded/meta-oe"
BBLAYERS += "${BSPDIR}/meta-openembedded/meta-python"
BBLAYERS += "${BSPDIR}/meta-raspberrypi"
```

Configure the machine type in `conf/local.conf` for the desired Raspberry Pi model, for example:
```bash
MACHINE ?= "raspberrypi3"
```
To build images for different models, set `MACHINE` to `raspberrypi0`, `raspberrypi2`, `raspberrypi3`, or `raspberrypi4` as needed.

## 3. Add OWL to the image

1. Add a custom layer (e.g. `meta-owl`) to package the OWL application. This layer should provide a recipe that installs OWL and its Python dependencies.
2. Include the OWL package in your image by adding it to `IMAGE_INSTALL` within your custom image recipe or `local.conf`:
```bash
IMAGE_INSTALL += "owl"
```

## 4. Build the image

Run `bitbake` to build the desired image, such as `core-image-base` or a custom recipe:
```bash
bitbake core-image-base
```
The resulting SD card image will be available under `tmp/deploy/images/<machine>/`.

## 5. Flash and run

Write the generated `.wic` or `.sdimg` file to an SD card and boot your Raspberry Pi. The OWL application can then be launched according to the normal usage instructions.

For further details on Yocto usage and customization, consult the [Yocto Project documentation](https://docs.yoctoproject.org/).
