# Contributing to OWL
Firstly, thank you for taking the time to contribute to the OWL project!

To make sure everyone is on the same page, has clarity in how to contribute and does so in a respectful and consistent manner we have put together the following set of guidelines. They are mostly just guidelines and if you feel changes are needed, identify them in a pull request to this document.

## Table of Contents
* [Code of conduct](#code-of-conduct)
* [How to contribute](#how-to-contribute)

## Code of Conduct
This project and everyone involved is governed by the [OWL Code of Conduct](CODE_OF_CONDUCT.md). By participating in the project you are expected to uphold this code. Please report any unacceptable behaviour to project admins.  

## How to contribute
The best way to start contributing is to build an OWL for yourself! As you find opportunities for improvement, start logging them as issues here.

There are also a list of active projects that you can take on as whole projects or individual items within each project. These are summarised below. This is not a definitive list though, so if you have other ideas or suggestions just let us know.

## Project List
### [Project 1](https://github.com/geezacoleman/OpenWeedLocator/projects/1): Integrating position information (GPS, GLONASS etc.) information
This project seeks to incorporate position information from either a per-unit and low cost [GPS/GLONASS/Beidou sensor](https://uk.rs-online.com/web/p/gnss-gps-modules/9054630?cm_mmc=UK-PLA-DS3A-_-google-_-CSS_UK_EN_Semiconductors_Whoop-_-GNSS+%26+GPS+Modules_Whoop_OMNISerpNov-_-9054630&matchtype=&pla-532120398712&gclid=CjwKCAiAz--OBhBIEiwAG1rIOtxRjYlN5e7eCDyFBnrZ0RgSKc8NUHGi27VLPxOhBYGM_iqA3I6PkhoCOsUQAvD_BwE&gclsrc=aw.ds) or interpreting NMEA strings from tractor-mounted GPS sensors. Fortunately both seem to return the same standard of data, so supporting the variations should be straightforward.

A substantial portion of the code is now complete but untested in [`gps-string-reading`](https://github.com/geezacoleman/OpenWeedLocator/tree/gps-string-reading)

#### Benefits
- on/off delay based on speed to reduce wastage and ensure weeds aren't missed
- recording weed locations for weed mapping and density estimates
- turn compensation

### [Project 2](https://github.com/geezacoleman/OpenWeedLocator/projects/2): Optimising OWL enclosure design
The current enclosure has served a good job of supporting the initial OWL testing, but could be improved for efficiency in printing, camera mounting strength and dust/water resistance. This is a great project to start with if you have a background in design.

**Benefits:**
- reduced print area and cost
- better dust and water resistance by covering the current camera hole
- strong camera mounts

### [Project 3](https://github.com/geezacoleman/OpenWeedLocator/projects/3): Upgrading OWL to Green-on-Green (in-crop) Weed Detection
A highly sought after upgrade to the owl, this is the most complex project listed. It involves hardware, software and algorithm development but is quite an achievable set of tasks with significant benefits. The main areas of work include incorporating the Jetson Nano hardware, developing software to run and interpret object detection/image classification models and training initial algorithms on existing data.

**Benefits:**
- in-crop weed recognition
- more computing power

### [Project 4](https://github.com/geezacoleman/OpenWeedLocator/projects/4): Reducing image blur to improve forward travel speed
The highest priority project. Reducing image blur on the Raspberry Pi camera is likely a software issue, but requires dedicated testing of the current camera and developing settings that will ensure reduced image blur in a range of conditions. Reduced blur means higher forward travel speeds with fewer missed weeds.

**Benefits:**
- greater sensitivity to small weeds
- higher forward travel speeds without missing weeds
- better overall performance

**If you have any other project ideas, please raise an issue and let us know**

