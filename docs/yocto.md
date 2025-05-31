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
4. If BitBake reports that user namespaces are not usable (for example on
   Ubuntu systems with AppArmor restrictions), enable them with:
   ```bash
   sudo sysctl -w kernel.unprivileged_userns_clone=1
   ```

5. Ensure the host packages providing `lz4c`, `pzstd`, `unzstd` and `zstd`
   are installed. On Debian/Ubuntu systems run:
   ```bash
   sudo apt-get install lz4 zstd
   ```

## 2. Configure layers

Edit `conf/bblayers.conf` in the build directory to include the new layers.
Use paths relative to the build directory so that BitBake can locate the
cloned layers:
```bash
BBLAYERS += "${TOPDIR}/../meta-openembedded/meta-oe"
BBLAYERS += "${TOPDIR}/../meta-openembedded/meta-python"
BBLAYERS += "${TOPDIR}/../meta-raspberrypi"
```

Configure the machine type in `conf/local.conf` for the desired Raspberry Pi model, for example:
```bash
MACHINE ?= "raspberrypi3"
```
To build images for different models, set `MACHINE` to `raspberrypi0`,
`raspberrypi2`, `raspberrypi3`, `raspberrypi4`, or `raspberrypi5` as needed.

If you require images for multiple boards, rerun the build with each desired
`MACHINE` setting:

```bash
for m in raspberrypi4 raspberrypi5; do
  sed -i "s/^MACHINE.*/MACHINE ?= \"$m\"/" conf/local.conf
  bitbake core-image-base
done
```

The provided GitHub workflow uses a similar approach with a build matrix to
automatically produce images for Raspberry Pi 3, 4 and 5 and attaches them to a
release when triggered from a tag. The workflow also compresses each `.wic`
image with `bzip2`, creates a corresponding `.bmap` file and generates a
`SHA256SUMS` file for verification.

## 3. Add OWL to the image

1. Add a custom layer (e.g. `meta-owl`) to package the OWL application. This layer should provide a recipe that installs OWL and its Python dependencies.
2. Include the OWL package in your image by adding it to `IMAGE_INSTALL` within your custom image recipe or `local.conf`:
```bash
IMAGE_INSTALL += "owl"
```

## 4. Add a custom splash screen (optional)

To show a branded splash screen on boot, enable the `psplash` package and point it at a custom PNG image:

```bash
IMAGE_INSTALL += "psplash"
SPLASH = "file://path/to/my_splash.png"
```

Place your splash image in your layer under `recipes-core/psplash/files/` and update your recipe accordingly. This will display the image while the system boots into the simple framebuffer console.

## 5. Build the image

Run `bitbake` to build the desired image, such as `core-image-base` or a custom recipe:
```bash
bitbake core-image-base
```
The resulting SD card image will be available under `tmp/deploy/images/<machine>/`.

## 6. Flash and run

Write the generated `.wic` or `.sdimg` file to an SD card and boot your Raspberry Pi. The OWL application can then be launched according to the normal usage instructions.

For further details on Yocto usage and customization, consult the [Yocto Project documentation](https://docs.yoctoproject.org/).
