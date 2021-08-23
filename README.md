<p align="center">
<img src="https://media.github.sydney.edu.au/user/3859/files/ec0ebd80-cc49-11eb-9d5a-77b93a48e97a" width="400">
</p>

Welcome to the OpenWeedLocator (OWL) project, an opensource hardware and software green-on-brown weed detector that uses entirely off-the-shelf componentry, very simple green-detection algorithms and entirely 3D printable parts. OWL integrates weed detection on a Raspberry Pi with a relay control board in a custom designed case so you can attach any 12V solenoid, relay, lightbulb or device for low-cost, simple and opensource site-specific weed control. Projects to date have seen OWL mounted on robots and vehicles for spot spraying! 

On the weed detection front, a range of algorithms have been provided, each with advantages and disadvantages for your use case. They include ExG (excess green 2g - r - b, developed by Woebbecke et al. 1995), a hue, saturation and value (HSV) threshold and a combined ExG + HSV algorithm. These algorithms have all been tested in a wide range of conditions, a preprint of the article is available here: [LINK TO ARTICLE](https://doi.org/10.31220/agriRxiv.2021.00074).

Internal electronics       |  Fitted module - vehicle | Fitted module - robot 
:-------------------------:|:-------------------------: |:-------------------------:
![Internal view](https://media.github.sydney.edu.au/user/3859/files/caf58200-cb0e-11eb-8b36-e5567c2a28e1)  |  ![Fitted module - spot spraying vehicle](https://media.github.sydney.edu.au/user/3859/files/6e469700-cb0f-11eb-84e4-4a17c5e03502) | ![Fitted module - robot](https://media.github.sydney.edu.au/user/3859/files/02fdc480-cb11-11eb-9778-f3e156ac7e25)

# Overview
* [OWL Use Cases](#owl-use-cases)
* [Hardware Requirements](#hardware-requirements)
  - [Hardware Assembly](#hardware-assembly)
* [Software Installation](#software)
  - [Quick Method](#quick-method)
  - [Detailed Method](#detailed-method)
  - [Changing Detection Settings](#changing-detection-settings)
* [3D Printing](#3d-printing)
* [Updating OWL](#updating-owl)
  - [Version History](#version-history)
* [Troubleshooting](#troubleshooting)
* [Citing OWL](#Citing OWL)
* [Acknowledgements](#acknowledgements)
* [References](#references)

# OWL Use Cases
## Vehicle-mounted spot spraying
The first, and most clear use case for the OWL is for the site-specific application of herbicide in fallow. As part of the development and testing of the unit, the OWL team designed and assembled a 2 m spot spraying boom, using two OWLs to control four 12 V solenoids each. The boom was mounted on the back of a ute/utility vehicle with the spray tank located in the tray and powered by a 12V car battery. Indicator lights for each nozzle were used to highlight more clearly when each solenoid had been activated for demonstration and testing purposes.

<p align="center">
<img src="https://media.github.sydney.edu.au/user/3859/files/71bb9e00-e480-11eb-9392-81826a88c680" width="600">
</p>

## Robot-mounted spot spraying
A second system, identical to the first, was developed for the University of Sydney's Digifarm robot, the Agerris Digital Farm Hand. The system is in frequent use for the site-specific control of weeds in trial areas. It is powered by the 24V system on the robot, using a 24 - 12V DC/DC converter.

<p align="center">
<img src="https://media.github.sydney.edu.au/user/3859/files/3cfd1600-e483-11eb-8adc-243534daac81" width="600">
</p>

## Community development
As more OWLs are built and fallow weed control systems developed, we would love to share the end results here. Please get in contact and we can upload images of the finished systems on this page.

# Hardware Requirements
A complete list of components is provided below. Further details on 3D models and hardware assembly are provided in subsequent sections. The quantities of each item below are for one OWL detection unit. 

*Please note links are provided to an example online retailer of each component for convenience only. There are certainly many other retailers that may be better suited and priced to your purposes and we encourage you to find local suppliers. Other types of connector, layout and design are also possible, which may change the parts required.*

| **Component**  | **Quantity** | **Link** |
| ------------- | ------------- | ------------- |
| **Enclosure**  |  |  |
| Main Case  | 1 | [STL File]() |
| Main Cover  | 1 | [STL File]() |
| Raspberry Pi Mount  | 1 | [STL File]() |
| Relay Control Board Mount  | 1 | [STL File]() |
| Voltage Regulator Mount  | 1 | [STL File]() |
| HQ Camera Mount  | 1 | [STL File]() |
| *V2 Camera Mount*  | 1 | [STL File]() |
| Rear Plug  | 1 | [STL File]() |
| **Computing**  |  |  |
| Raspberry Pi 4 8GB  | 1  | [Link](https://core-electronics.com.au/raspberry-pi-4-model-b-8gb.html) |
| 64GB SD Card  | 1  | [Link](https://core-electronics.com.au/extreme-sd-microsd-memory-card-64gb-class-10-adapter-included.html) |
| **Camera**  |  |  |
| Raspberry Pi HQ Camera  | 1  | [Link](https://core-electronics.com.au/raspberry-pi-hq-camera.html) |
| CCTV 6mm Wide Angle Lens  | 1  | [Link](https://core-electronics.com.au/raspberry-pi-6mm-wide-angle-lens.html) |
| **Power**  |  |  |
| 5V 5A Step Down Voltage Regulator  | 1  | [Link](https://core-electronics.com.au/pololu-5v-5a-step-down-voltage-regulator-d24v50f5.html) |
| 4 Channel, 12V Relay Control Board  | 1  | [Link](https://www.jaycar.com.au/arduino-compatible-4-channel-12v-relay-module/p/XC4440?gclid=Cj0KCQjwvYSEBhDjARIsAJMn0ljQf_l5tRY0D4UyDRlaNBFV6-XAj_UGQzC029d-wiwoCyD6Rzy7x2MaAinhEALw_wcB) |
| M205 Panel Mount Fuse Holder  | 1  | [Link](https://www.jaycar.com.au/round-10a-240v-m205-panel-mount-fuse-holder/p/SZ2028?pos=17&queryId=11c21fd77c75a11725bd0f093a0fc862&sort=relevance) |
| Jumper Wire  | 1  | [Link](https://core-electronics.com.au/solderless-breadboard-jumper-cable-wires-female-female-40-pieces.html) |
| WAGO 2-way Terminal Block  | 2  | [Link](https://au.rs-online.com/web/p/splice-connectors/8837544/) |
| Bulgin Connector - Panel Mount | 1  | [Link](https://au.rs-online.com/web/p/industrial-circular-connectors/8068625/) |
| Bulgin Connector - Plug  | 1  | [Link](https://au.rs-online.com/web/p/industrial-circular-connectors/8068565/) |
| Micro USB to USB-C adaptor | 1  | [Link]() |
| Micro USB Cable | 1  | [Link]() |
| **Miscellaneous**  |  |  |
| 12V Chrome LED | 2  | [Link](https://www.jaycar.com.au/12v-mini-chrome-bezel-red/p/SL2644) |
| 3 - 16V Piezo Buzzer  | 1  | [Link](https://www.jaycar.com.au/mini-piezo-buzzer-3-16v/p/AB3462?pos=8&queryId=404751ef55b1d6b8adef8b031d16576f&sort=relevance) |
| Brass Standoffs - M2/3/4  | Kit  | [Link](https://www.amazon.com/Hilitchi-360pcs-Female-Standoff-Assortment/dp/B013ZWM1F6/ref=sr_1_5?dchild=1&keywords=standoff+kit&qid=1623697572&sr=8-5) |
| M3 Bolts/Nuts  | 4 each or Kit | [Link](https://www.amazon.com/DYWISHKEY-Pieces-Stainless-Socket-Assortment/dp/B07VNDFYNQ/ref=sr_1_4?crid=2X7QROKBF9F4D&dchild=1&keywords=m3+hex+bolt&qid=1623697718&sprefix=M3+hex%2Caps%2C193&sr=8-4) |
| Wire - 20AWG (red/black/green/blue/yellow/white) | 1 roll each  | [Link](https://www.amazon.com/Electronics-different-Insulated-Temperature-Resistance/dp/B07G2GLKMP/ref=sr_1_1_sspa?dchild=1&keywords=20+awg+wire&qid=1623697639&sr=8-1-spons&psc=1&spLa=ZW5jcnlwdGVkUXVhbGlmaWVyPUEyMUNVM1BBQUNKSFNBJmVuY3J5cHRlZElkPUEwNjQ4MTQ5M0dRTE9ZR0MzUFE5VyZlbmNyeXB0ZWRBZElkPUExMDMwNTIwODM5OVVBOTFNRjdSJndpZGdldE5hbWU9c3BfYXRmJmFjdGlvbj1jbGlja1JlZGlyZWN0JmRvTm90TG9nQ2xpY2s9dHJ1ZQ==) |

## Hardware Assembly
All components listed above are relatively "plug and play" with minimal soldering or complex electronics required. Follow these instructions carefully and triple check your connections before powering anything on to avoid losing the magic smoke and potentially a few hundred dollars. Never make changes to the wiring on the detection unit while it is connected to 12V and always remain within the safe operating voltages of any component.

Before starting, have a look at the complete wiring diagram below to see how everything fits together. The LEDs, fuse and Bulgin connector are all mounted on the rear of the OWL unit, rather than where they are located in the diagram. If you prefer not to use or can't access a Bulgin connector, there is a separate 3D model design that uses cable glands instead.

### Required tools
* Wire strippers
* Wire cutters
* Soldering iron/solder

![OWL - wiring diagram](https://media.github.sydney.edu.au/user/3859/files/e004fc00-cb74-11eb-8938-cd571b3ab787)

### Step 1 - enclosure and mounts
Assembling the components for an OWL unit requires the enclosure and mounts as a minimum. These can be 3D printed on your own printer or printed and delivered from one of the many online stores that offer a 3D printing service. Alternatively, you could create your own enclosure using a plastic electrical box and cutting holes in it, if that's easier. We'll be assuming you have printed out the enclosure and associated parts for the rest of the guide, but please share your finished designs however they turn out!

The first few steps don't require the enclosure so you can make a start right away, but while you're working on getting that assembled, make sure you have the pieces printing, they'll be used from Step 4. For a complete device, you'll need: 1 x base, 1 x cover, 1 x RPi mount, 1 x relay mount, 1 x regulator mount, 1 x camera mount and 1 x plug.

### Step 2 - soldering
There are only a few components that need soldering, including the fuse and voltage regulator:
* Soldering of voltage regulator pins
* Soldering of 12V input wires to voltage regulator pins
* Soldering of 5V output wires to voltage regulator pins
* Soldering of red wire to both fuse terminals

**NOTE**: Soldering can burn you and generates potentially hazardous smoke! Use appropriate care, fume extractors and PPE to avoid any injury. If you're new to soldering, read through [this guide](https://www.makerspaces.com/how-to-solder/), which explains in more detail how to perfect your skills and solder safely.

**NOTE**: When soldering, it's best to cover the exposed terminals with glue lined heat shrink to reduce the risk of electrical short circuits.

Voltage regulator | Voltage regulator pins | Fuse
:-------------: | :-------------: | :-------------: 
![Vreg](https://media.github.sydney.edu.au/user/5402/files/12c53200-ce9e-11eb-812b-52c51a0c6263) | ![Vregpins](https://media.github.sydney.edu.au/user/5402/files/85abe580-d023-11eb-87ee-c3cad42406fe)| ![Fuse](https://media.github.sydney.edu.au/user/5402/files/240e3e80-ce9e-11eb-8c0f-25e296720072)

Once the two red wires are soldered to the fuse, the fuse can be mounted on the rear panel of the OWL base. One wire will be connected to the Bulgin plug (next step) and the other to the Wago 2-way block.

For neater wiring you can also solder jumpers between all the normally open (NO) pins on the base of the relay board, but this is optional. If you don't solder these connections, make sure you connect wire using the screw terminals instead. Photos of both are provided below.

Soldered | Screw terminals
:-------------: | :-------------: 
![Relayboardunderside](https://media.github.sydney.edu.au/user/5402/files/e5938500-cf6c-11eb-91a2-75685a6d948d) | ![Relayboardalternative](https://media.github.sydney.edu.au/user/5402/files/e88e7580-cf6c-11eb-8e26-7bbce2fb3f71)

The other wires requiring soldering are joins between the buzzer and jumper wires for easy connection to the GPIO pins and from the LEDs to the power in/jumper wires.

### Step 3 - wiring up Bulgin connector 
Next we'll need to wire the output relay control and input 12V wires to the Bulgin panel mount connector. Fortunately all pins are labelled, so follow the wire number table below. This will need to be repeated for the Bulgin plug as well, which will connect your solenoids or other devices to the relay control board.

The process is:
1. Connect all wires to Bulgin connector using the screw terminals
2. Mount the connector to the rear panel
3. Leave at least 10cm of wire so it can be connected to the relay board and other connections later. 

Bulgin terminal number | Wire connection
:-------------: | :-------------: 
1 | Blue wire - connects to centre terminal (common) on relay 1
2 | Green wire - connects to centre terminal (common) on relay 2
3 | Orange wire - connects to centre terminal (common) on relay 3
4 | White wire - connects to centre terminal (common) on relay 4
5 | Red 12VDC - connects to fuse wire already soldered. Make sure wire is the right length when mounted. 
6 | Black GND - connects to Wago 2-way terminal

**NOTE**: Skip this step if you're using cable glands.

Once all the wires have been connected you can now mount the Bulgin connector to the OWL base.

### Step 4 - mounting the relay control board and voltage regulator
Attach the relay control board to the 3D printed relay control board mount using 2.5 mm standoffs. Attach the voltage regulator to the 3D printed voltage regulator mount with 2 mm standoffs. The mounted voltage regulator can then be mounted to one corner of the relay control board. The relay board and voltage regulator can then be installed in the raised slots in the OWL base.

**NOTE**: Use **2.5 mm** standoffs for mounting the relay control board to its base. Use **2 mm** standoffs to mount the voltage regulator to its base.

![Relaymount](https://media.github.sydney.edu.au/user/5402/files/964c5500-cf6a-11eb-8d2f-e1282b6411c3)

### Step 5 - wiring the relay control board, voltage regulator, Wago 2-way blocks and Bulgin connector
Connect the relay control board to the Bulgin connector using the table in step 3 as a guide. Next, connect red and black jumper wires to the VCC and GND header pins on the relay control board. Now choose one Wago block to be a 12V positive block and the second to be the negative or ground. To the positive block, connect the 12 V wire from the fuse (12V input from source), the 12 V input to the voltage regulator, the 12 V solenoid line from the relay board and the VCC line from the relay board to one of the two WAGO terminal blocks, twisting the wires together if necessary. Repeat with the second, negative WAGO terminal block, connecting the input ground line from the Bulgin connector, ground line from the voltage regulator and the GND black wire from the relay board.

Installed relay board | Relay board wiring diagram | Relay board wiring
:-------------: | :-------------: | :-------------: 
![Relayboardinstalled](https://media.github.sydney.edu.au/user/5402/files/54c7a400-cf82-11eb-9fb3-250199227384) | ![OWL - relay board diagram](https://media.github.sydney.edu.au/user/3859/files/431ed600-cf5b-11eb-94df-87f01e0a41a4) | ![relayinputs](https://media.github.sydney.edu.au/user/5402/files/cad01780-d023-11eb-98e0-bfcc1c2c03e0) |

### Step 6 - mounting Raspberry Pi and connecting power
Attach the Raspberry Pi to the 3D printed mount using 2.5 mm standoffs. Install in the raised slots in the OWL base. Connect to micro USB power from the voltage regulator, using a micro USB to USB-C adaptor. Alternatively, the Raspberry Pi can be powered over the GPIO, however, this has not yet been implemented.

Raspberry Pi mount | Raspberry Pi in OWL base
:-------------: | :-------------: 
![RPimount](https://media.github.sydney.edu.au/user/5402/files/263dcf00-cf6a-11eb-9781-1c2d79c9b96d) | ![RPiin base](https://media.github.sydney.edu.au/user/5402/files/c2b99e80-cf75-11eb-9420-b14853929b90)

### Step 7 - connecting GPIO pins
Connect the Raspberry Pi GPIO to the relay control board header pins, using the table below and the wiring diagram above as a guide:

RPi GPIO pin | Relay header pin
:-------------: | :-------------: 
13 | IN1
14 | COM
15 | IN2
16 | IN3
18 | IN4

Raspberry Pi GPIO pins | Relay control board header pins
:-------------: | :-------------: 
![GPIOtorelay](https://media.github.sydney.edu.au/user/5402/files/6adb7000-d027-11eb-81b9-5fecfe0c2c4e) | ![relayheaderpins](https://media.github.sydney.edu.au/user/5402/files/2bf8ea80-d026-11eb-8c5c-685563db7691)

![GPIOconnected](https://media.github.sydney.edu.au/user/5402/files/25ab3580-cf76-11eb-9edf-f19f833a7a00)

### Step 8 - mounting and connecting HQ camera
Connect one end of the CSI ribbon cable to the camera. Attach the HQ camera to the 3D printed mount using 2.5 mm standoffs. Ensuring that the CSI cable port on the camera is directed towards the Raspberry Pi, mount the camera inside the OWL case using M3 bolts and nuts. Connect the other end of the CSI cable to the Raspberry Pi CSI camera port.

**NOTE** the lens comes with a C-CS mount adapter which needs to be removed before fitting to the camera sensor base. The image won't focus unless the adapter is removed. 

HQ camera mount | Raspberry Pi camera port | Camera mounted in case
:-------------: | :-------------: | :-------------:
![Cameramount](https://media.github.sydney.edu.au/user/5402/files/5db87580-cf82-11eb-8ba3-6df5352908dd) | ![Cameracable](https://media.github.sydney.edu.au/user/5402/files/7ed2a200-d026-11eb-93cb-26a91d727094) | ![Camerainbase](https://media.github.sydney.edu.au/user/5402/files/65781a00-cf82-11eb-860b-1173f6e98037)

The lens will need to be focused, details below, once the software is correctly set up.

### Step 9 - adding buzzer and LEDs
Mount the buzzer inside the OWL base using double sided mounting tape and connect the 5 V and ground wires to Raspberry Pi GPIO pins 7 and 9, respectively. Install the 5 V LED inside the OWL base and connect the 5V and ground wire to GPIO pins 8 and 20, respectively. Install the 12 V LED inside the OWL base and connect the 12 V and ground wires to their respective WAGO terminal blocks.

Buzzer location | LEDs in OWL base | GPIO pins
:-------------: | :-------------: | :-------------:
![Buzzer](https://media.github.sydney.edu.au/user/5402/files/72950900-cf82-11eb-8afc-2a84a742d97c) | ![LEDs](https://media.github.sydney.edu.au/user/5402/files/79bc1700-cf82-11eb-80b2-646f2c74fcc9) | ![GPIOpins](https://media.github.sydney.edu.au/user/5402/files/e0474080-d027-11eb-93fd-8c7b7c783eea)

### OPTIONAL STEP - adding real time clock module
Although optional, we recommend that you use a real time clock (RTC) module with the OWL system. This will enable the Raspberry Pi to hold the correct time when disconnected from power and the internet, and will be useful for debugging errors if they arise. The RTC uses a CR1220 button cell battery and sits on top of the Raspberry Pi using GPIO pins 1-6.

PiRTC module | RTC installed on Raspberry Pi
:-------------: | :-------------: 
![RTC](https://media.github.sydney.edu.au/user/5402/files/a59bd300-d03c-11eb-847a-d0813f44fcb2) | ![RTConPi](https://media.github.sydney.edu.au/user/5402/files/a6cd0000-d03c-11eb-9742-595b12c3693e)

### Step 10 - connecting mounting hardware and OWL cover
There are four 6.5 mm holes on the OWL base for mounting to a boom. Prior to installing the OWL cover, decide on a mounting solution suitable to your needs. In the photo below, we used 4 x M6 bolts. The cover of the OWL unit is secured with 4 x M3 nuts and bolts. Place M3 nuts into the slots in the OWL base. This can be fiddly and we suggest using tweezers, as shown below. Place the cover onto the base and secure using M3 bolts.

Mounting hardware | Cover nuts | Completed OWL unit
:-------------: | :-------------: | :-------------:
![Mountinghardware](https://media.github.sydney.edu.au/user/5402/files/a8d28880-cf82-11eb-8bbe-da740b2f7aa4) | ![covernut](https://media.github.sydney.edu.au/user/5402/files/b12ac380-cf82-11eb-9b5e-ad9b8c1c334e) | ![Cover](https://media.github.sydney.edu.au/user/5402/files/bd168580-cf82-11eb-83a8-41df69e970a4)

### Step 11 - connecting 12V solenoids
Once you have completed the setup, you now have the opportunity to wire up your own solenoids for spot spraying, targeted tillage, spot flaming or any other targeted weed control you can dream up. To do this, wire the GND wire of your device (it can be any wire if it's a solenoid) to the ground pin on the Bulgin plug (the same wire used for the GND from the 12V power source) and wire the other to one of the blue, green, orange or white wires on pins 1 - 4. A wiring diagram is provided below. The easiest way to wire them together to the same GND wire is to create a six-way harness, where one end is connected to the plug, one of the five other wires to the source power GND and the remaining four to the solenoids or whatever devices you are driving.

![Solenoid wiring diagram](https://media.github.sydney.edu.au/user/3859/files/f55d5180-cc6c-11eb-805a-80616648355d)


Bulgin plug | Ground wiring harness
:-------------: | :-------------:
![Bulginplug](https://media.github.sydney.edu.au/user/5402/files/7f753380-d03a-11eb-8d9b-658db73d3408) | ![Groundharness](https://media.github.sydney.edu.au/user/5402/files/7e440680-d03a-11eb-9af1-67132f4cc36f)

# Software
The project will eventually support the use of the two major embedded computing devices, the Raspberry Pi (models 3B+ and 4) and the Jetson Nano/Jetson Xavier NX for possible green-on-green detection with deep learning algorithms. At present, just the details on setting up the Raspberry Pi 3B+/4 are provided below. There are two options for installation. For the first, all you'll need to do is download the disk image file (vX.X.X-owl.img) and flash it to an SD card. The second method is more in depth, but takes you through the entire process from beginning to end. If you're looking to learn about how everything works, take some time to work through this process.

## Quick Method
For this method you'll need access to:
* Desktop/laptop computer
* Micro SD card reader
* Internet with large data capacity and high speed (WARNING: the image file is large and downloading will take time and use up a substantial quantity of your data allowance if you have are on a limited plan)

### Step 1 - download the disk image file
Download the entire disk image file (v0.1.0-owl.img) here: [OWL disk image](https://www.dropbox.com/s/ad6uieyk3awav9k/owl.img.zip?dl=0)

The latest, stable version will be linked above, however, all other older versions or versions with features being tested are available [here](#version-history).

### Step 2 - flash owl.img to SD card
The easiest way to flash (add the vX.X.X-owl.img file to the SD card so it can boot) the SD card is to use Balena Etcher or any other card flashing software. Instructions for Balena Etcher are provided here. Navigate to the [website](https://www.balena.io/etcher/) and download the relevant version/operating system. Install Balena Etcher and fire it up.

![OWL - etcher](https://media.github.sydney.edu.au/user/3859/files/e184ea00-d5a0-11eb-9560-4842758686d0)

* Insert the SD card using your SD card reader.
* Select `Flash from file` on the Balena Etcher window and navigate to where you downloaded the vXX-XX-XX-owl.dmg file. This can be a zip file (compressed) too.
* Select the target, the SD card you just inserted.
* Click `Flash`

If this completes successfully, you're ready to move to the next step. If it fails, use Balena Etcher documentation to diagnose the issue.

### Step 3 - power up
Once the SD card is inserted into the slot of the Raspberry Pi, power everything up and wait for the beep. If you hear the beep, you're ready to go and start focusing the camera.

### Step 4 - focusing the camera
For this final step, you'll need to connect a computer screen, keyboard and mouse to the Raspberry Pi.

The final step in the process is to make sure the camera and lens are correctly focused for the mounting height. To view the live camera feed, navigate to the Home > owl directory and open the greenonbrown.py file. Choose to 'open' the file (rather than 'execute' or 'execute in terminal') when a window pops up. Scroll down to the bottom of the code and on line 385, under `owl = Owl(....)` change `headless=True` to `headless=False`. Save and exit the file.

Now reboot the Raspberry Pi. Once the Raspberry Pi has rebooted, you'll need to launch greenonbrown.py manually. To do this open up a Terminal window (Ctrl + Alt + T) and type the following command:
```
(owl) pi@owl :-$ ~/owl/./greenonbrown.py
```

This will bring up a video feed you can use to visualise the OWL detector and also use it to focus the camera. Once you're happy with the focus, press Esc to exit. Navigate back to the greenonbrown.py file. You'll now need to change `headless` back to `True`. Double click on greenonbrown.py, choose to 'open' the file (rather than 'execute' or 'execute in terminal') when a window pops up. Scroll down to the bottom of the code and on line 385, under `owl = Owl(....)` change `headless=False` back to `headless=True`. Save and exit the file. Shutdown the Raspberry Pi. Unplug the screen, keyboard and mouse and reboot.

You're now ready to run!

## Detailed Method
This setup approach may take a little longer (about 1 hour total) than the quick method, but you'll be much better trained in the ways of OWL and more prepared for any problem solving, upgrades or changes in the future. In the process you'll learn about Python environments, install Python packages and set it all up to run on startup. To get this working you'll need access to:
* Raspberry Pi
* Empty SD Card (SanDisk 32GB SDXC ideally)
* Your own computer with SD card reader
* Power supply (if not using the OWL unit)
* Screen and keyboard
* WiFi/Ethernet cable

### Step 1 - Raspberry Pi setup
Before powering up the Raspberry Pi, you'll need to install the Raspian operating system (just like Windows/MacOSX for laptops) on the new SD card. This is done using the same process as the quick method used to flash the premade owl.img file, except you'll be doing it with a completely new and untouched version of Raspbian. To get the Raspberry Pi to the stage at which we can start installing OWL software, follow [these instructions](https://www.pyimagesearch.com/2019/09/16/install-opencv-4-on-raspberry-pi-4-and-raspbian-buster/) from Adrian Rosebrock at PyImageSearch. They are very well written, detailed and if you're interested in computer vision, the rest of the PyImageSearch blog posts are very useful. 

**NOTE 1**:
At **PyImageSearch Step 3** make sure to create a virtual environment `owl` (it *must* be named `owl` otherwise the software will not load) instead of `cv` as written in the guide.
```
$ mkvirtualenv owl -p python3
```

**NOTE 2**:
At **PyImageSearch Step 4** you do not need to compile OpenCV from scratch, the pip install method (**Step 4a**) will be a LOT faster and perfectly functional for this project. Make sure you're in the owl virtual environment for this step by looking for (owl) at the start of the line, if it's not there type: `workon owl`
```
(owl) pi@owl :-$ pip install opencv-contrib-python==4.1.0.25
```

### Step 2 - enable camera
We now need to enable the connection to the Raspberry Pi camera. This can be enabled in raspi-config:
```
(owl) pi@owl :-$ sudo raspi-config
```
Select **3 Interface Options**, then select **P1 Camera**. Select **Yes** to enable the camera. You can now exit raspi-config and reboot.

### Step 3 - downloading the 'owl' repository
Now you should have:
* A virtual environment called 'owl'
* A working version of OpenCV installed into that environment
* a Terminal window open with the 'owl' environment activated. If it is active (owl) will appear at the start of a new line in the terminal window. If you're unsure, run: `workon owl`  

The next step is to download the entire OpenWeedLocator repository into your *home* directory on the Raspberry Pi.
```
(owl) pi@owl :-$ cd ~
(owl) pi@owl :-$ git clone https://github.com/geezacoleman/OpenWeedLocator
(owl) pi@owl :-$ mv /home/pi/OpenWeedLocator /home/pi/owl
```
Double check it is there by typing `(owl) pi@owl :-$ ls` and reading through the results, alternatively open up the Home folder using a mousee. If that was sucessful, you can now move on to Step 4.

### Step 4 - installing the OWL Python dependencies
Dependencies are Python packages on which the code relies to function correctly. With a range of versions and possible comptibility issues, this is the step where issues might come up. There aren't too many packages, but please make sure each and every module in the requirements.txt file has been installed correctly. These include:
* OpenCV (should already be in 'owl' virtual environment from Step 1)
* numpy
* imutils
* gpiozero
* pandas (for data collection only)
* glob (for data collection only)
* threading, collections, queue, time, os (though these are included as standard Python modules).

To install all the requirements.txt, simply run:
```
(owl) pi@owl :-$ cd ~/owl
(owl) pi@owl :-$ pip install -r requirements.txt
```
It's very important that you're in the owl virtual environment for this, so double check that **(owl)** appears on the far left of the command line when you type the command in. Check these have been installed correctly by importing them in Python in the command prompt and check the package version. To do this:
```
(owl) pi@owl :-$ python
```
Python should start up an interactive session; type each of these in and make sure you don't get any errors.
```
>>> import cv2
>>> import numpy
>>> import gpiozero
>>> import pandas
```
Version numbers can be checked with:
```
>>> print(package_name_here.__version__) ## this is a generic example - add the package where it says package_name_here
>>> print(cv2.__version__)
```

If any errors appear, you'll need to go back and check that the modules above have (1) been installed into the owl virtual environment, (2) that Python was started in the owl environment, and/or (3) they all installed correctly. Once that is complete, exit Python and continue with the installation process.
```
>>> exit()
```

### Step 5 - starting OWL on boot
Now that these dependencies have been installed into the owl virtual environment, it's time to make sure it runs on startup! The first step is to make the Python file `greenonbrown.py` executable using the Terminal window.
```
(owl) pi@owl :-$ chmod a+x ~/owl/greenonbrown.py
```
After it's been made executable, the file needs to be launched on startup so each time the Raspberry Pi is powered on, the detection systems starts. The easiest way to do this
by using cron, a scheduler for starting code. So you'll need to add the `owl_boot.sh` file to the schedule so that it launches on boot. The `owl_boot.sh` file is fairly straightforward. It's what's known as a [bash script](https://ryanstutorials.net/bash-scripting-tutorial/bash-script.php) which is just a text file that contains commands we would normally enter on the command line in Terminal. 
```
#!/bin/bash

source /home/pi/.bashrc
workon owl
lxterminal
cd /home/pi/owl
./greenonbrown.py
```
In the file, the first two commands launch our `owl` virtual environment, then `lxterminal` creates a virtual terminal environment so outputs are logged. Finally we change directory `cd` into the owl folder and run the python program. 

To add this to the list of cron jobs, you'll need to edit it as a root user:
```
pi@owl :-$ sudo crontab -e
```
Select `1. /bin/nano editor`, which should bring up the crontab file. At the base of the file add:
```
@reboot /home/pi/owl/owl_boot.sh
```
Once you've added that line, you'll just need to save the file and exit. In the nano editor just press Ctrl + X, then Y and finally press Enter to agree to save and exit.

Finally you just need to make `owl_boot.sh` executable so it can be run on startup:
```
(owl) pi@owl :-$ chmod a+x ~/owl/owl_boot.sh
```

If you get stuck, [this guide](https://www.makeuseof.com/how-to-run-a-raspberry-pi-program-script-at-startup/) or [this guide](https://www.tomshardware.com/how-to/run-script-at-boot-raspberry-pi) both have a bit more detail on cron and some other methods too. 

### Step 6 - focusing the camera
The final step in the process is to make sure the camera and lens are correctly focused for the mounting height. The camera will need to be connected for this step. To view the live camera feed, navigate to the Home > owl directory and open the greenonbrown.py file. Choose to 'open' the file (rather than 'execute' or 'execute in terminal') when a window pops up. Scroll down to the bottom of the code and on line 385, under `owl = Owl(....)` change `headless=True` to `headless=False`. Save and exit the file.

Using a Terminal window, type the following command:
```
(owl) pi@owl :-$ ~/owl/./greenonbrown.py
```
This will bring up a video feed you can use to visualise the OWL detector and also use it to focus the camera. Once you're happy with the focus, press Esc to exit. Navigate back to the greenonbrown.py file. You'll now need to change `headless` back to `True`. Double click on greenonbrown.py, choose to 'open' the file (rather than 'execute' or 'execute in terminal') when a window pops up. Scroll down to the bottom of the code and on line 385, under `owl = Owl(....)` change `headless=False` back to `headless=True`. Save and exit the file.

You're now almost ready to run!

### Step 7 - reboot
The moment of truth. Shut the Raspberry Pi down and unplug the power. This is where you'll need to reconnect the camera and all the GPIO pins/power in the OWL unit if they have been disconnected. Once everything is connected again (double check the camera cable is inserted or this won't work), reconnect the power and wait for a beep!

If you hear a beep, grab something green and move it under the camera. If the relays start clicking and lights come on, congratulations, you've successfully set the OWL up! If not, check the troubleshooting chart below and see if you can get it fixed.

**NOTE** The unit does not perform well under office/artificial lighting. The thresholds have been set for outdoor conditions.

### OPTIONAL STEP - installing RTC and setting the time
The optional real time clock module can be set up by following the [detailed instructions](https://learn.adafruit.com/adding-a-real-time-clock-to-raspberry-pi/set-up-and-test-i2c) provided by Adafruit. This is a quick process that should take less than 10 minutes. Note that an internet connection is required to set the time initially, however after this the time will be held on the clock module.

## Changing detection settings
If you're interested in changing settings on the detector, such as selecting the weed detection algorithm, modifying sensitivity settings, viewing results and a whole raft of other options, connect a screen, keyboard and mouse and boot up the OWL. Navigate to the owl directory and open up `greenonbrown.py` in an editor. You'll need to right click, select open with and then choose an  integrated development environment (IDE). Once it's open, scroll down to the very bottom and you should come across:
```
if __name__ == "__main__":
    sprayer = Sprayer(video=False,
                      videoFile=r'xyz',
                      headless=True,
                      recording=True,
                      exgMin=13,
                      exgMax=200,
                      hueMin=30,
                      hueMax=92,
                      saturationMin=10,
                      saturationMax=250,
                      brightnessMin=15,
                      brightnessMax=250,
                      resolution=(416, 320))
    sprayer.start(sprayDur=0.15,
                  sample=False,
                  sampleDim=1000,
                  saveDir='/home/pi',
                  algorithm='hsv',
                  selectorEnabled=True,
                  camera_name='hsv',
                  minArea=10)
```
Here's a summary table of what each parameter does. If you change `headless` to `False`, you'll be able to see a real time feed of what the algorithm is doing and where the detections are occurring. Just make sure to switch it back to `headless=True` if you decide to run it without the screen connected. Note that the owl program will not run on startup if `headless=False`.

**Parameter**  | **Options** | **Description** 
:-------------: | :-------------: | :-------------: 
**Sprayer()** | | All options when the sprayer class is instantiated
`video`|`True` or `False`| Toggles whether or not to use an existing video. Useful if you have a recording and you want to see how it performs on a laptop at home.
`videoFile`|Any path string| If 'video' is True, the program will try to read from this path here. Replace xyz with the path to your video.
`headless`|`True` or `False`| Toggles whether or not to display a video output. IMPORTANT: OWL will not run on boot if set to `True` and no screen connected. Set to `True` to see how the algorithm is doing.
`recording`|`True` or `False`| Toggles whether or not to record a video. If a switch is connected, it will only record when switch pressed/connected.
`exgMin`|Any integer between 0 and 255| Provides the minimum threshold value for the exg algorithm. Usually leave between 10 (very sensitive) and 25 (not sensitive)
`exgMax`|Any integer between 0 and 255| Provides a maximum threshold for the exg algorithm. Leave above 180. 
`hueMin`|Any integer between 0 and 128| Provides a minimum threshold for the hue channel when using hsv or exhsv algorithms. Typically between 28 and 45. Increase to reduce sensitivity.
`hueMax`|Any integer between 0 and 128| Provides a maximum threshold for the hue (colour hue) channel when using hsv or exhsv algorithms. Typically between 80 and 95. Decrease to reduce sensitivity.
`saturationMin`|Any integer between 0 and 255| Provides a minimum threshold for the saturation (colour intensity) channel when using hsv or exhsv algorithms. Typically between 4 and 20. Increase to reduce sensitivity.
`saturationMax`|Any integer between 0 and 255| Provides a maximum threshold for the saturation (colour intensity) channel when using hsv or exhsv algorithms. Typically between 200 and 250. Decrease to reduce sensitivity.
`brightnessMin`|Any integer between 0 and 255| Provides a minimum threshold for the value (brightness) channel when using hsv or exhsv algorithms. Typically between 10 and 60. Increase to reduce sensitivity particularly if false positives in shadows.
`brightnessMax`|Any integer between 0 and 255| Provides a maximum threshold for the value (brightness) channel when using hsv or exhsv algorithms. Typically between 190 and 250. Decrease to reduce sensitivity particularly if false positives in bright sun.
`resolution`|Tuple of (w, h) resolution| Changes output resolution from camera. Increasing rapidly decreased framerate but improves detection of small weeds.
**start()** | | All options when the sprayer.start() function is called
`sprayDur`|Any float (decimal)|Changes the length of time for which the relay is activated.|
`sample`|`True` or `False`| If sampling code is uncommented, images of weeds detected will be saved to OWL folder. Do not leave on for long periods or SD card will fill up and stop working.|
`sampleDim` | Any float (decimal) | Changes the length of time for which the relay is activated.|
`saveDir` | Any integer| Changes the width of the saved image.|
`algorithm`|Any of: `exg`,`exgr`,`exgs`,`exhu`,`hsv`| Changes the selected algorithm. Most sensitive: 'exg', least sensitive/most precise (least false positives): 'exgr', 'exhu', 'hsv'|
`selectorEnabled`|`True` or `False`| Enables algorithm selection based on a rotary switch. Only enable is switch is connected.|
`cameraName` | Any string | Changes the save name if recording videos of the camera. Ignore - only used if recording data.|
`minArea`| Any integer  | Changes the minimum size of the detection. Leave low for more sensitivity of small weeds and increase to reduce false positives.|

# Image Processing
So how does OWL actually detect the weeds and trigger the relay control board? It all starts by taking in the colour image from the camera using OpenCV and splitting it into its component channels: Red (R), Green (G) and Blue (B) (RGB) or loading and converting into the hue, saturation and value (HSV) colourspace. Following that, computer vision algorithms such as Excess Green `ExG = 2 * G - R - B` or thresholding type approaches on the HSV colourspace can be used to differentiate green locations from the background. 

![image](https://media.github.sydney.edu.au/user/3859/files/ced62b00-cea5-11eb-93af-477cbf582176)

Once the green locations are identified and a binary (purely black/white) mask generated, a contouring process is run to outline each detection. If the detection pixel area is greater than the minimum area set in `minArea=10`, the central pixel coordinates of that area are related to an activation zone. That zone is connected to a specific GPIO pin on the Raspberry Pi, itself connected to a specific channel on the relay (one of IN1-4). When the GPIO pin is driven high (activated) the relay switches and connects the solenoid for example to 12V and activates the solenoid. It's all summarised below.

![OWL - workflow](https://media.github.sydney.edu.au/user/3859/files/8aa06480-cf51-11eb-9f79-c802248b0ff8)

## Results
The performance of each algorithm on 7 different day/night fields is outlined below. The boxplot shows the range, interquartile range and median performance for each algorithm. Whilst there were no significant differences (P > 0.05) for the recall (how many weeds were detected of all weeds present) and precision (how many detections were actually weeds), trends indicated the ExHSV algorithm was less sensitive (fewer false detections) and more precise, but did miss more smaller/discoloured weeds compared to ExG.

![results boxplot](https://media.github.sydney.edu.au/user/3859/files/2bf26200-d4e0-11eb-970e-8478fae12c00)

The image below gives a better indication of the types of weeds that were detected/missed by the ExHSV algorithm. Large, green weeds were consistently found, but small discoloured or grasses with thin leaves that blurred into the background were missed. Faster shutter speed would help improve this performance.

![OWL - detections](https://media.github.sydney.edu.au/user/3859/files/f0a46300-d4e0-11eb-93ae-9f894587f0a6)

# 3D Printing
There are seven total items that need printing for the complete OWL unit. All items with links to the STL files are listed below. There are two options for OWL base:
1. Single connector (Bulgin) panel mount
   - Pros: of this method are easy/quick attach/detach from whatever you have connected.
   - Cons: more connections to make, more expensive
2. Cable gland
   - Pros: fewer connections to make, cheaper, faster to build
   - Cons: more difficult to remove

Description |  Image (click for link)
:-------------------------:|:-------------------------:
OWL base, onto which all components are mounted. The unit can be fitted using the M6 bolt holes on the rear panel. |  [![OWL Base - single outlet](https://media.github.sydney.edu.au/user/3859/files/bd7bde80-cb86-11eb-94a6-81a54480be8f)](https://github.sydney.edu.au/gcol4791/OpenSpotSprayer/blob/master/3D%20Models/Tall%20enclosure%20base%20-%20single%20connector.stl)
OPTIONAL: OWL base with cable glands instead of single Bulgin connector. |  [![OWL Base - cable glands](https://media.github.sydney.edu.au/user/3859/files/049c9480-d4e2-11eb-955d-f3961a7bb8a9)](https://github.sydney.edu.au/gcol4791/OpenWeedLocator/blob/master/3D%20Models/Tall%20enclosure%20base%20-%20cable%20glands.stl)
OWL cover, slides over the base and is fitted with 4 x M3 bolts/nuts. Provides basic splash protection. |  [![OWL Cover](https://media.github.sydney.edu.au/user/3859/files/d6848f80-cb86-11eb-956c-a7c0b5bb4c13)](https://github.sydney.edu.au/gcol4791/OpenSpotSprayer/blob/master/3D%20Models/Tall%20enclosure%20cover.stl)
OWL base port cover, covers the cable port on the rear panel. |  [![OWL base port cover](https://media.github.sydney.edu.au/user/3859/files/12b7f000-cb87-11eb-980b-564e7b4324f6)](https://github.sydney.edu.au/gcol4791/OpenSpotSprayer/blob/master/3D%20Models/Tall%20enclosure%20plug.stl)
Raspberry Pi mount, fixes to the Raspberry Pi for easy attachment to OWL base. |  [![Raspberry Pi mount](https://media.github.sydney.edu.au/user/3859/files/5d396c80-cb87-11eb-948c-d60efe433ac8)](https://github.sydney.edu.au/gcol4791/OpenSpotSprayer/blob/master/3D%20Models/Raspberry%20Pi%20mount.stl)
Raspberry Pi HQ Camera mount, fixes to the HQ Camera for simple attachment to the base with 2 x M3 bolts/nuts. |  [![Raspberry Pi HQ Camera mount](https://media.github.sydney.edu.au/user/3859/files/dcc73b80-cb87-11eb-9d4a-dd5f3abecbbd)](https://github.sydney.edu.au/gcol4791/OpenSpotSprayer/blob/master/3D%20Models/HQ%20camera%20mount.stl)
OPTIONAL Raspberry Pi v2 Camera mount, fixes to the v2 Camera for simple attachment to the base with 2 x M3 bolts/nuts. |  [![Raspberry Pi v2 Camera mount](https://media.github.sydney.edu.au/user/3859/files/1dbf5000-cb88-11eb-8e39-72fd6e1cb604)](https://github.sydney.edu.au/gcol4791/OpenSpotSprayer/blob/master/3D%20Models/PiCamera%20v2%20mount.stl)
Relay board mount, fixes to the relay board for simple attachment to the base. |  [![Relay board mount](https://media.github.sydney.edu.au/user/5402/files/d421aa00-d04c-11eb-9191-bcad7b51c1a4)](https://github.sydney.edu.au/gcol4791/OpenWeedLocator/blob/master/3D%20Models/Relay%20control%20board%20mount.stl)
Voltage regulator mount, fixes to the voltage regulator and onto the relay board for simple attachment to the base. |  [![Voltage regulator mount](https://media.github.sydney.edu.au/user/5402/files/8147f280-d04c-11eb-89ec-4af125a8f232)](https://github.sydney.edu.au/gcol4791/OpenWeedLocator/blob/master/3D%20Models/Voltage%20regulator%20mount.stl)

All .stl files for the 3D printed components of this build are available in the 3D Models directory. Ideally supports should be used for the base, and were tested at 0.2mm layer heights with 15% infill on a Prusa MK3S.

We also provide a link to the [3D models on Tinkercad](https://www.tinkercad.com/things/3An6a3MtL9C), an online and free 3D modelling software package, allowing for further customisation to cater for individual user needs. 

# Updating OWL
We and others will be continually contributing to and improving OWL as we become aware of issues or opportunities to increase detection performance. Once you have a functioning setup the process to update is simple. First, you'll need to connect a screen, keyboard and mouse to the OWL unit and boot it up. Navigate to the existing owl directory in `/home/owl/` and either delete or rename that folder. Remember if you've made any of your own changes to the parameters/code, write them down. Then open up a Terminal window (Ctrl + T) and follow these steps:

```
(owl) user@pi :-$ cd ~
(owl) user@pi :-$ git clone https://github.com/geezacoleman/OpenWeedLocator
(owl) user@pi :-$ chmod +x ~/owl/greenonbrown.py
```
And that's it! You're good to go with the latest software.

If you have multiple units running, the most efficient method is to update one and then copy the SD card disk image to every other unit. Follow these instructions here. ADD INSTRUCTIONS

## Version History
All versions of OWL can be found here. Only major changes will be recorded as separate disk images for use.
Version |  File
:-------------------------:|:-------------------------:
v0.1.0-owl.img | https://www.dropbox.com/s/ad6uieyk3awav9k/owl.img.zip?dl=0

# Troubleshooting
Here's a table of some of the common symptoms and possible explanations for errors we've come across. This is by no means exhaustive, but hopefully helps in diagnosing any issues you might have.

Symptom | Explanation | Possible solution
:-------------------------:|:-------------------------:|:-------------------------:
Raspberry Pi won't start (no green/red lights) | No power getting to the computer | Check the power source, and all downstream components. Such as Bulgin panel/plug connections fuse connections and fuse, connections to Wago 2-way block, voltage regulator connections, cable into the Raspberry Pi.
Raspberry Pi starts (green light flashing) but no beep | OWL software has not started | This is likely a configuration/camera connection error with many possible causes. To get more information, boot the Raspberry Pi with a screen connected, open up a Terminal window (Ctrl + T) and type `~/owl/./greenonbrown.py`. This will run the program. Check any errors that emerge.
Beep heard, but no relays activating when tested with green | Relays are not receiving (1) 12V power, (2) a signal from the Pi, (3) the Pi is not sending a signal | Check all your connections with a multimeter if necessary for the presence of 12V. Make sure everything is connected as per the wiring diagram. If you're confident there are no connection issues, open up a Terminal window (Ctrl + T) and type `~/owl/./greenonbrown.py`. This will run the program. Check any errors that emerge.

# Citing OWL
If you have used OWL in your research please consider citing this repository as below:


# Acknowledgements
This project has been developed by Guy Coleman and William Salter at the University of Sydney, Precision Weed Control Lab. It was supported and funded by the Grains Research and Development Corporation (GRDC) and Landcare Australia as part of the University of Sydney's Digifarm project in Narrabri, NSW, Australia.

# Disclaimer and License
While every effort has been made in the development of this guide to cover critical details, it is not an exhaustive nor perfectly complete set of instructions. It is important that people using this guide take all due care in assembly to avoid damage, loss of components and personal injury, and are supervised by someone experienced if necessary. Assembly and use of OWL is entirely at your own risk and the license expressly states there is no warranty.

```
MIT License

Copyright (c) 2020 Guy Coleman

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

# References
## Journal Papers
Woebbecke, D. M., Meyer, G. E., Von Bargen, K., Mortensen, D. A., Bargen, K. Von, and Mortensen, D. A. (1995). Color Indices for Weed Identification Under Various Soil, Residue, and Lighting Conditions. Trans. ASAE 38, 259–269. doi:https://doi.org/10.13031/2013.27838.

## Blog Posts
[How to run a Raspberry Pi script at startup](https://www.makeuseof.com/how-to-run-a-raspberry-pi-program-script-at-startup/)

[How to Run a Script at Boot on Raspberry Pi (with cron)](https://www.tomshardware.com/how-to/run-script-at-boot-raspberry-pi)

[Install OpenCV 4 on Raspberry Pi 4 and Raspbian Buster](https://www.pyimagesearch.com/2019/09/16/install-opencv-4-on-raspberry-pi-4-and-raspbian-buster/)

[How to solder](https://www.makerspaces.com/how-to-solder/)
