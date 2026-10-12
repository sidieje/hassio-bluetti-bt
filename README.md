# hassio-bluetti-bt
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate with hassfest](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/hassfest_validation.yml/badge.svg)](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/hassfest_validation.yml)
[![HACS Action](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/HACS.yml/badge.svg)](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/HACS.yml)

### Charger 2 and Apex 300 Read and Write Support

This fork extends [Patrick762/hassio-bluetti-bt](https://github.com/Patrick762/hassio-bluetti-bt) with Bluetooth read and control support for the **BLUETTI Charger 2**, and **Apex 300**.

The Charger 2 uses a different register layout and control protocol from other supported BLUETTI devices. Its system power and silent charging controls share a multipurpose register rather than using independent Boolean registers, requiring additional read-modify-write handling in the Bluetooth communication layer.

Charger 2 support has been tested with physical hardware, including reading device data and operating both controls. As these changes introduce device-specific handling beyond the original implementation, support remains experimental. This fork is maintained independently, and feedback, testing, and issue reports are welcome.

Apex 300 support adds controls for AC and DC outputs, AC Eco Mode, DC Eco Mode, Power Lifting, UPS charging mode, and Eco timers, along with many additional sensor readings.

A Bluetooth integration for BLUETTI power stations in Home Assistant.

## Disclaimer
This integration is provided without any warranty or support by Bluetti. I do not take responsibility for any problems it may cause in all cases. Use it at your own risk.

## Installation
To install this integration, you first need [HACS](https://hacs.xyz/) installed.
After the installation, you can use this button to install the integration:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=sidieje&repository=hassio-bluetti-bt&category=integration)

### Supported devices:

See [bluetti-bt-lib](https://github.com/sidieje/bluetti-bt-lib/tree/ha-deployment#supported-powerstations-and-data)

### Available controls:
See [bluetti-bt-lib](https://github.com/sidieje/bluetti-bt-lib/tree/ha-deployment#supported-powerstations-and-data)

### Adding devices or fields

Please use the issue template at [bluetti-bt-lib](https://github.com/Patrick762/bluetti-bt-lib?tab=readme-ov-file#supported-powerstations-and-data)
