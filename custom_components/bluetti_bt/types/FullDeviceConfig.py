"""The config entry shape for one station.

Entry data, written when the entry is created:

    address             str   MAC address, e.g. "AA:BB:CC:DD:EE:FF"
    name                str   station name, used to build the device
    type                str   model name, e.g. "AC300"
    use_encryption      bool  default False
    encryption_assumed  bool  default False

Options, which the options flow mirrors into the entry data as well:

    polling_interval     int  default 20
    polling_timeout      int  default 45
    max_retries          int  default 5
    pack_poll_interval   int  default DEFAULT_PACK_POLL_INTERVAL (0, off)

The first three are required; everything else arrives through a ``.get()``
default in InitialDeviceConfig or OptionalDeviceConfig, which is what makes
the schema extendable without a migration.

Versioning: the integration deliberately declares no VERSION and no
async_migrate_entry. ConfigFlow inherits VERSION = 1 from Home Assistant's
BaseFlowHandler, so entries are already stored at version 1 and
ConfigEntry.async_migrate() returns early without looking for a hook.
use_encryption, encryption_assumed and pack_poll_interval were all added
this way. Set VERSION on the ConfigFlow class and add async_migrate_entry
only for a change that genuinely cannot be defaulted.
"""

from typing import Any, Dict

from .InitialDeviceConfig import InitialDeviceConfig
from .OptionalDeviceConfig import OptionalDeviceConfig


class FullDeviceConfig:
    def __init__(
        self,
        initial: InitialDeviceConfig,
        optional: OptionalDeviceConfig,
    ):
        self.address = initial.address
        self.name = initial.name
        self.dev_type = initial.dev_type
        self.use_encryption = initial.use_encryption
        self.polling_interval = optional.polling_interval
        self.polling_timeout = optional.polling_timeout
        self.max_retries = optional.max_retries
        self.pack_poll_interval = optional.pack_poll_interval

    @staticmethod
    def from_dict(raw: Dict[str, Any]):
        initial = InitialDeviceConfig.from_dict(raw)

        if initial is None:
            return None

        return FullDeviceConfig(
            initial,
            OptionalDeviceConfig.from_dict(raw),
        )
