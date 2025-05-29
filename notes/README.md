# OWL Documentation Notes

This directory contains detailed documentation for each component of the OpenWeedLocator (OWL) system.

| File                                             | Description                                   | Last Updated |
|--------------------------------------------------|-----------------------------------------------|--------------|
| [owl.py](owl_py_notes.txt)                       | Main control script and system initialization | 10/11/2024   |
| [error_manager.py](error_manager_py_notes.txt)   | Error handling and management system          | 10/11/2024   |
| [video_manager.py](video_manager_py_notes.txt)   | Camera handling and video stream management   | 10/11/2024   |
| [input_manager.py](input_manager_py_notes.txt)   | Physical controls and GPIO management         | 10/11/2024   |
| [output_manager.py](output_manager_py_notes.txt) | Relay control and status indicators           | 10/11/2024   |
| [algorithms.py](algorithms_py_notes.txt)         | Image processing algorithms | 29/05/2025 |
| [config_manager.py](config_manager_py_notes.txt) | Configuration validation    | 29/05/2025 |
| [directory_manager.py](directory_manager_py_notes.txt) | USB directory setup | 29/05/2025 |
| [frame_reader.py](frame_reader_py_notes.txt)     | Read frames from files      | 29/05/2025 |
| [greenonbrown.py](greenonbrown_py_notes.txt)     | Colour-based weed detection | 29/05/2025 |
| [greenongreen.py](greenongreen_py_notes.txt)     | YOLO weed detection         | 29/05/2025 |
| [image_sampler.py](image_sampler_py_notes.txt)   | Asynchronous image saving   | 29/05/2025 |
| [log_manager.py](log_manager_py_notes.txt)       | Central logging utilities   | 29/05/2025 |
| [vis_manager.py](vis_manager_py_notes.txt)       | Terminal relay visualiser   | 29/05/2025 |
| [version.py](version_py_notes.txt)               | Version and system info     | 29/05/2025 |

Each note file follows a consistent format:
```
################################################################################
Notes on <filename>

Summary completed on DD/MM/YYYY
Summary based on commit XXXXXXX
################################################################################

Purpose:
- Core functionality overview

Classes:
- List of classes

[Detailed class documentation]
- Methods
- Attributes 
- Dependencies
```

These notes are maintained alongside code changes and are updated with each 
commit that modifies the corresponding file's functionality.
