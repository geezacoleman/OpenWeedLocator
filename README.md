<p align="center">
<img src="https://user-images.githubusercontent.com/51358498/152991504-005a1daa-2900-4f48-8bec-d163d6336ed2.png" width="400">
</p>

# OpenWeedLocator (OWL)

**Open-source, low-cost weed detection for site-specific control**

[![Tests](https://github.com/geezacoleman/OpenWeedLocator/actions/workflows/tests.yml/badge.svg)](https://github.com/geezacoleman/OpenWeedLocator/actions/workflows/tests.yml)
[![DOI](https://zenodo.org/badge/399194159.svg)](https://zenodo.org/badge/latestdoi/399194159)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)

OWL is a camera-based weed detector built on Raspberry Pi that uses green-detection algorithms to trigger relay-controlled solenoids for spot spraying. It's built entirely from off-the-shelf components and 3D-printable parts, making precision weed control accessible to anyone.

[**Website**](https://www.openweedlocator.org) | [**Documentation**](https://docs.openweedlocator.org) | [**Community**](https://community.openweedlocator.org) | [**Newsletter**](https://openagtech.beehiiv.com/)

---

### OWLs in Action

|                                                                OWL on a vehicle                                                                 |                                            OWL on the AgroIntelli Robotti                                             | OWL on the Agerris Digital Farmhand | OWL on a bicycle |
|:-----------------------------------------------------------------------------------------------------------------------------------------------:|:---------------------------------------------------------------------------------------------------------------------:|-------------------------------------|------------------|
| ![Fitted module - spot spraying vehicle](https://user-images.githubusercontent.com/51358498/130522810-bb19e6ca-5019-4de4-83cc-858eca358ef8.jpg) | ![robotti_crop](https://github.com/geezacoleman/OpenWeedLocator/assets/51358498/99df0188-a850-4753-ac48-ab743c46d563) |                   ![OWL - on robot agerris](https://github.com/geezacoleman/OpenWeedLocator/assets/51358498/9cb73514-dffc-4c53-969e-c1c816610f1b)                  |          ![bike_owl_cropped](https://github.com/geezacoleman/OpenWeedLocator/assets/51358498/17ad4ead-429e-4384-9e74-b050a536897f)        |

---

## Quick Start

```bash
# Clone and install on Raspberry Pi (Bookworm OS)
git clone https://github.com/geezacoleman/OpenWeedLocator owl
bash owl/owl_setup.sh

# Run detection
workon owl
python owl.py
```

See the [full installation guide](https://docs.openweedlocator.org/software/) for detailed instructions.

## Documentation

- [**Getting Started**](https://docs.openweedlocator.org/getting-started/) -- What you need, how it works
- [**Hardware Assembly**](https://docs.openweedlocator.org/hardware/) -- Parts list, wiring, 3D printing
- [**Software Setup**](https://docs.openweedlocator.org/software/) -- Installation, configuration, algorithms
- [**Web Controllers**](https://docs.openweedlocator.org/controllers/) -- Standalone and networked dashboards
- [**Troubleshooting**](https://docs.openweedlocator.org/troubleshooting/) -- Common issues and fixes

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines, or join the conversation at [community.openweedlocator.org](https://community.openweedlocator.org).

---

# Citing OWL

<details>
<summary>Citing OWL</summary>
<br>

OpenWeedLocator has been published in [Scientific Reports](https://www.nature.com/articles/s41598-021-03858-9). Please
consider citing the published article using the details below.

```
@article{Coleman2022,
author = {Coleman, Guy and Salter, William and Walsh, Michael},
doi = {10.1038/s41598-021-03858-9},
issn = {2045-2322},
journal = {Scientific Reports},
number = {1},
pages = {170},
title = {{OpenWeedLocator (OWL): an open-source, low-cost device for fallow weed detection}},
url = {https://doi.org/10.1038/s41598-021-03858-9},
volume = {12},
year = {2022}
}

```

</details>

## License

This project is licensed under the [MIT License](LICENSE).

---

### Star History

[![Star History Chart](https://api.star-history.com/svg?repos=geezacoleman/OpenWeedLocator&type=Timeline)](https://star-history.com/#geezacoleman/OpenWeedLocator&Timeline)
