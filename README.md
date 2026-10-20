# hassio-bluetti-bt
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate with hassfest](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/hassfest_validation.yml/badge.svg)](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/hassfest_validation.yml)
[![HACS Action](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/HACS.yml/badge.svg)](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/HACS.yml)

### Bluetti Bluetooth integration with read and encrypted write support

This fork extends [Patrick762/hassio-bluetti-bt](https://github.com/Patrick762/hassio-bluetti-bt) with Bluetooth read support and write control for the encrypted devices **BLUETTI Charger 2**, **Apex 300**, **EL10**, **EL30 V2**, **EL100 V2**, **EL100 mini**, **EL200 V2** and **EL300**.

#### Device support and encrypted communication

This fork adds working controls for Elite Series devices that require encrypted Bluetooth communication. These capabilities are implemented through the extended [bluetti-bt-lib](https://github.com/sidieje/bluetti-bt-lib), which maintains persistent BLE connections shared between reading and writing, reducing repeated connections and encryption handshakes.

The **Charger 2** uses a different control protocol, with system power and silent charging sharing a multipurpose register that requires read-modify-write handling.

Unencrypted devices retain their existing controls, with additional controls added for the AC200M, AC300 and AC500. Battery packs report per-pack SOC and voltage. Sensors for empty slots are registered as disabled to avoid cluttering the device page. The integration also corrects itself when a pack is added or moved.

The library also improves connection recovery, cleans up failed connections, retries failed notification subscriptions and verifies writes by reading back the updated device state. Improved connection locking prevents retries or integration unloads from interrupting writes, while more controlled retry intervals and quieter logging improve reliability during outages.

Support for encrypted models is experimental, although it has been tested against physical hardware. This fork is maintained independently, and feedback, testing and issue reports are welcome.

#### Write controls by device

| Device | Controls |
| --- | --- |
| **Charger 2** | System on/off, silent charging |
| **Apex 300**, **EL30 V2**, **EL100 V2**, **EL100 mini**, **EL200 V2**, **EL300** | UPS mode, grid charge, AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout, battery SOC range |
| **EL10** | AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout |

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
