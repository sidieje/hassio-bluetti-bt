# hassio-bluetti-bt
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate with hassfest](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/hassfest_validation.yml/badge.svg)](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/hassfest_validation.yml)
[![HACS Action](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/HACS.yml/badge.svg)](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/HACS.yml)

### Bluetti Bluetooth integration with read and encrypted write support

This fork extends [Patrick762/hassio-bluetti-bt](https://github.com/Patrick762/hassio-bluetti-bt) with Bluetooth read support and write control for the encrypted devices **BLUETTI Charger 2**, **Apex 300**, **EL10**, **EL30 V2**, **EL100 V2**, **EL100 mini**, **EL200 V2** and **EL300**.

#### Device support and encrypted communication

This fork adds working controls for Elite Series devices that require encrypted Bluetooth communication. These capabilities are implemented through the extended [bluetti-bt-lib](https://github.com/sidieje/bluetti-bt-lib), which maintains persistent BLE connections shared between reading and writing, reducing repeated connections and encryption handshakes.

The **EL200 V2** is now recognised by the name it actually reports, `Elite 200 V2`, and that name is in the bluetooth matcher list, so the station appears under Discovered instead of being missed by every entry in the previous list.

The **Apex 300** no longer offers the grid charge switch: its register reads zero and ignores writes, so the switch could never show or change the station's real state.

The **Charger 2** uses a different control protocol, with system power and silent charging sharing a multipurpose register that requires read-modify-write handling.

Unencrypted devices retain their existing controls, with additional controls added for the AC200M, AC300 and AC500. The **AC180P** answers a few more registers than its profile claimed, so it now reports DC input voltage and current, AC output voltage and frequency, and its charge range. Per-pack SOC and voltage are available on those tower stations, but pack polling is off by default because walking the packs costs about three seconds per pack; enable it per device from its Configure dialog. Sensors for empty slots are registered as disabled to avoid cluttering the device page. The integration also corrects itself when a pack is added or moved.

The library also improves connection recovery, cleans up failed connections, retries failed notification subscriptions and verifies writes by reading back the updated device state. A read that a station skips is asked for once more before the cycle is given up on, and every control is verified against a re-read window rather than a single delayed read: the first read-back lands one second after the write, then every half second until three and a half seconds have passed. A station that is slow to reflect a change, like the AC300, is caught by a later read instead of a single unlucky one, and the entity updates as soon as a read-back confirms the write. If nothing in the window confirms it, the switch keeps its last known state instead of snapping back to the value the station had not updated yet, and the next poll corrects it. Improved connection locking prevents retries or integration unloads from interrupting writes, while more controlled retry intervals and quieter logging improve reliability during outages.

Support for encrypted models is experimental, although it has been tested against physical hardware. This fork is maintained independently, and feedback, testing and issue reports are welcome.

#### Write controls by device

| Device | Controls |
| --- | --- |
| **Charger 2** | System on/off, silent charging |
| **Apex 300** | UPS mode, AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout, battery SOC range |
| **EL100 V2**, **EL100 mini** | UPS mode, grid charge, AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout, battery SOC range |
| **EL200 V2**, **EL300** | UPS mode, grid charge, AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout, battery SOC range |
| **EL30 V2** | Grid charge, AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout, battery SOC range |
| **EL10** | AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout |

The eco minimum power sliders use the ranges the Bluetti app offers on each model: 5-20 W on DC and 10-40 W on AC for the Apex 300, EL100 V2, EL100 mini, EL30 V2, EL200 V2 and EL300, and 5-10 W / 15-30 W on the EL10.

Every settings slider moves in single units, and the battery SOC range is limited to 5-100% like the app. The stop threshold still has to be above the start threshold, so the integration rejects a pair the app would refuse.

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
