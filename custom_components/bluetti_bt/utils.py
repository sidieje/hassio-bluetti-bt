"""Tools."""

from typing import Any, Callable

from bluetti_bt_lib import BluettiDevice, build_device
from bluetti_bt_lib.devices import DEVICE_ALIASES, DEVICES


def device_class_for_type(dev_type: Any) -> Callable[[], BluettiDevice] | None:
    """The device class a stored type refers to, or None when it means nothing.

    The type is stored exactly as it was recognised, which is either a key of
    the library's device table or one of its aliases: the Charger 2 reports
    "CHARGER 2" in its type register while the table is keyed "CHARGER2".
    """
    if not isinstance(dev_type, str):
        return None

    return DEVICES.get(DEVICE_ALIASES.get(dev_type, dev_type))


def build_bluetti_device(dev_type: Any, name: Any) -> BluettiDevice | None:
    """Build the device a config entry refers to.

    The entry name keeps only the capitals and digits of what was advertised,
    which is lossy: an EL100mini normalises to EL100<serial>, which the library
    reads back as an EL10. The type stored when the device was added is exact,
    so it is used first and the name only backs up entries saved without one.
    """
    device_cls = device_class_for_type(dev_type)

    if device_cls is not None:
        return device_cls()

    return build_device(name) if isinstance(name, str) else None


def mac_loggable(mac: str) -> str:
    """Remove parts of the mac address for logging."""
    splitted = mac.split(":")
    return "XX:XX:XX:XX:XX:" + splitted[-1]


def unique_id_logable(unique_id: str) -> str:
    """Remove parts of the unique id for logging."""
    splitted = unique_id.split("_", maxsplit=1)
    serial = splitted[0][:6]
    return serial + "XXXXXXXXXX" + splitted[1]
