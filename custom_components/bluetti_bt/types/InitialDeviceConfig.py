from typing import Any, Dict

CONF_ADDRESS = "address"
CONF_NAME = "name"
CONF_TYPE = "type"
CONF_USE_ENCRYPTION = "use_encryption"
CONF_ENCRYPTION_ASSUMED = "encryption_assumed"


class InitialDeviceConfig:
    def __init__(
        self,
        address: str,
        name: str,
        dev_type: str,
        use_encryption: bool,
        encryption_assumed: bool = False,
    ):
        self.address = address
        self.name = name
        self.dev_type = dev_type
        self.use_encryption = use_encryption
        # Set when the channel came from a table rather than from probing the
        # device, so the options flow can offer to correct it
        self.encryption_assumed = encryption_assumed

    @staticmethod
    def from_dict(raw: Dict[str, Any]):
        if not InitialDeviceConfig.has_values(raw):
            return None

        return InitialDeviceConfig(
            raw.get(CONF_ADDRESS),
            raw.get(CONF_NAME),
            raw.get(CONF_TYPE),
            raw.get(CONF_USE_ENCRYPTION, False),
            raw.get(CONF_ENCRYPTION_ASSUMED, False),
        )

    @property
    def as_dict(self) -> Dict[str, Any]:
        return {
            CONF_ADDRESS: self.address,
            CONF_NAME: self.name,
            CONF_TYPE: self.dev_type,
            CONF_USE_ENCRYPTION: self.use_encryption,
            CONF_ENCRYPTION_ASSUMED: self.encryption_assumed,
        }

    @staticmethod
    def has_values(raw: Dict[str, Any]) -> bool:
        return (
            isinstance(raw.get(CONF_ADDRESS), str)
            and isinstance(raw.get(CONF_NAME), str)
            and isinstance(raw.get(CONF_TYPE), str)
            and isinstance(raw.get(CONF_USE_ENCRYPTION, False), bool)
            and isinstance(raw.get(CONF_ENCRYPTION_ASSUMED, False), bool)
        )
