# hassio-bluetti-bt
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate with hassfest](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/hassfest_validation.yml/badge.svg)](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/hassfest_validation.yml)
[![HACS Action](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/HACS.yml/badge.svg)](https://github.com/Patrick762/hassio-bluetti-bt/actions/workflows/HACS.yml)

### Bluetti Bluetooth integration with read and encrypted write support

This fork extends [Patrick762/hassio-bluetti-bt](https://github.com/Patrick762/hassio-bluetti-bt) with Bluetooth read support and write control for the encrypted devices **BLUETTI Charger 2**, **Apex 300**, **EL10**, **EL30 V2**, **EL100 V2**, **EL100 mini**, **EL200 V2**, **EL300** and **FridgePower**.

#### Device support and encrypted communication

This fork adds working controls for Elite Series devices that require encrypted Bluetooth communication. These capabilities are implemented through the extended [bluetti-bt-lib](https://github.com/sidieje/bluetti-bt-lib). 

### Fork Highlights

This fork focuses on improving connection reliability, polling efficiency, write verification and device support.

- **Persistent BLE connections:** Reading and writing share persistent connections, reducing repeated connections and encryption handshakes.
- **Verified writes:** All controls are checked against the device's reported state, improving reliability and the accuracy of Home Assistant entity states.
- **Write locking:** Prevents duplicate writes while verification is in progress.
- **Optimised register polling:** Adjacent registers are read in blocks, reducing polling times and adapter load.
- **Automatic device recognition:** Many known devices are recognised automatically, reducing the need for repeated discovery and encryption checks.
- **Improved connection recovery:** Failed connections are cleaned up, notification subscriptions are retried, and connection locking prevents retries or integration unloads from interrupting active writes.
- **More reliable state updates:** Controlled retry intervals, repeated read-back verification and quieter logging improve reliability, particularly during outages and when stations are slow to reflect changes.

The **Charger 2** uses a different control protocol. System power and silent charging share a multipurpose register, requiring read-modify-write handling to avoid overwriting unrelated settings.

For **unencrypted devices**, existing controls are retained, with additional controls added for the AC200M, AC300 and AC500. These tower stations also expose per-pack state of charge (SOC) and voltage. Pack polling is disabled by default because polling each pack adds approximately three seconds per pack; it can be enabled per device through the Configure dialog. Sensors for empty pack slots are registered as disabled to avoid cluttering the device page. The integration also updates its configuration when a pack is added or moved.

### Write verification and state handling

Every control write is verified by reading back the device state over a verification window, rather than relying on a single delayed read. The first read-back occurs one second after the write, followed by additional reads every half-second until three and a half seconds have elapsed.

This is particularly useful for stations such as the AC300, which can take longer to reflect a change. Later read-backs can confirm a write that an earlier read would miss, and the Home Assistant entity updates as soon as the change is confirmed.

If the verification window expires without confirmation, the switch retains its last known state rather than reverting to a value the station has not yet updated. The next polling cycle can then reconcile the entity with the device's reported state. Reads skipped by a station are also retried once before the polling cycle is abandoned.

Improved connection locking prevents retries and integration unloads from interrupting writes, while more controlled retry intervals and quieter logging improve reliability during outages.

Support for encrypted models remains **experimental**, although it has been tested against physical hardware. This fork is maintained independently, and feedback, testing and issue reports are welcome.

### Write controls by device
| Device | Controls |
| --- | --- |
| **Charger 2** | System on/off, silent charging |
| **Apex 300** | UPS mode, AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout, battery SOC range |
| **EL100 V2**, **EL100 mini** | UPS mode, grid charge, AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout, battery SOC range |
| **EL200 V2**, **EL300** | UPS mode, grid charge, AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout, battery SOC range |
| **EL30 V2** | Grid charge, AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout, battery SOC range |
| **EL10** | AC output, DC output, AC and DC eco mode (with eco timers and eco minimum power), charging mode, power lifting, display timeout |
| **FridgePower** | UPS mode, AC output, DC output, AC and DC eco mode (with eco timers), charging mode, power lifting, time of use, grid self-adaption |

The eco minimum power sliders use the ranges the Bluetti app offers on each model: 5-20 W on DC and 10-40 W on AC for the Apex 300, EL100 V2, EL100 mini, EL30 V2, EL200 V2 and EL300, and 5-10 W / 15-30 W on the EL10.

The **EL300** is the exception on UPS mode. It numbers that register differently from the other V2 portables, so its select offers only *Customized UPS*, the one value confirmed on a physical unit; the integration will not write a mode number that has not been seen on that model. The remaining modes are being read out on an Elite 300 in [bluetti-bt-lib#24](https://github.com/sidieje/bluetti-bt-lib/issues/24) and will appear as they are confirmed. The other V2 portables keep all four modes.

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
