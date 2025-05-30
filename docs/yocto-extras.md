# Extra Features in the OWL Yocto Image

This document describes the components and configuration that the OWL Yocto image provides on top of a standard Yocto base image. It is intended for advanced users who want to understand exactly what is included and how it is configured.

## System Overview

The OWL Yocto image is built around the `core-image-base` recipe from the Yocto Project. Additional layers and recipes configure the system for Raspberry Pi hardware and install the OpenWeedLocator application. The image targets headless operation and fast boot times while retaining the ability to debug and update the system when required.

Key goals of the image are:

- Minimal root filesystem size
- Preinstalled OWL Python environment
- Automatic startup of the OWL service at boot
- Support for both legacy `picamera` and the newer `picamera2` libraries
- Simplified networking and logging configuration

## Included Layers

In addition to the standard `poky` layers, the build includes:

- `meta-openembedded` for many Python and system packages
- `meta-raspberrypi` for Raspberry Pi specific kernel and bootloader support
- `meta-owl` (custom layer) containing the OWL application and service files

These layers are added to `bblayers.conf` using paths relative to the build directory. See [Build Guide](yocto.md) for details on configuring the environment.

## System Packages

The final image contains only the packages needed to run OWL and manage the device. Notable packages include:

- `python3` and the Python modules listed in `requirements.txt`
- `systemd` as the init system
- Networking utilities: `wic`, `iproute2`, `wireless-tools`, and `wpa-supplicant`
- `openssh` for remote login and file transfer
- Optional `psplash` for a custom boot splash screen

Additional packages can be added by extending `IMAGE_INSTALL` in your image recipe.

## OWL Service

A `systemd` service file installed by the `meta-owl` layer launches `owl.py` on boot. The service ensures that OWL restarts automatically if it exits unexpectedly. Service logs are written to the standard `journald` database and can be viewed with `journalctl -u owl`.

## Filesystem Layout

The root filesystem is generated with a focus on reliability:

- `/home/root/owl` contains the OWL application code
- `/etc/owl/` stores configuration files copied from the repository's `config/` directory
- `/var/log/owl/` is created for storing runtime logs

The image can be configured as read-only by setting `IMAGE_FEATURES += "read-only-rootfs"` in `local.conf`. A separate writable data partition can be used for logs and configuration overrides if required.

## Networking

By default, the image enables DHCP on the primary Ethernet interface. Wi‑Fi support is included through `wpa-supplicant`. To preconfigure a wireless network, add a `wpa_supplicant.conf` file to `meta-owl/recipes-core/files/` and append the appropriate lines to `local.conf`:

```bash
WPA_CONF = "${sysconfdir}/wpa_supplicant/wpa_supplicant.conf"
```

## Camera Support

The Raspberry Pi kernel and firmware are configured to enable both the legacy camera stack and the modern `libcamera` pipeline. The OWL application can select either interface through its command line options. Device tree overlays for I2C and SPI are also enabled to support external triggers and drivers.

## Updating the Image

Because the image is built with Yocto, updates typically require rebuilding and reflashing. For small Python-only changes, you can copy updated files over SSH and restart the `owl` service. For larger changes such as kernel updates, rebuild the image with `bitbake` and reflash the SD card.

## Customisation Tips

- Use the `EXTRA_IMAGE_FEATURES` variable to add debugging utilities like `ssh-server-openssh` or `package-management`.
- Adjust `PREFERRED_VERSION_python3` and other package versions in `conf/local.conf` if specific versions are required.
- To speed up development, you can build an SDK with `bitbake core-image-base -c populate_sdk` and use it on your host to cross-compile additional tools.

## References

- [Yocto Project Documentation](https://docs.yoctoproject.org/)
- [Raspberry Pi BSP Layer](https://github.com/agherzan/meta-raspberrypi)

