"""Tools."""

from typing import TYPE_CHECKING, Any, Callable

from bluetti_bt_lib import BluettiDevice, build_device
from bluetti_bt_lib.devices import DEVICE_ALIASES, DEVICES

from .const import UNAVAILABLE_AFTER_FAILED_CYCLES

if TYPE_CHECKING:
    from .coordinator import PollingCoordinator


def debounced_available(
    coordinator: "PollingCoordinator", attr_available: bool
) -> bool:
    """Whether an entity presents itself as available.

    Home Assistant's own CoordinatorEntity.available reports
    coordinator.last_update_success, and every entity in this integration
    overrides it, so the debounce is applied here instead. Counting the
    coordinator's failed cycles rather than a counter the entity keeps itself is
    what makes that work: Home Assistant stops notifying the entities on the
    cycles after a refresh fails, so an entity counting its own notifications
    would stop counting at the first one.

    attr_available carries the reasons an entity has of its own, such as an
    empty pack slot or a field the device does not report.
    """
    return (
        attr_available
        and coordinator.consecutive_failures < UNAVAILABLE_AFTER_FAILED_CYCLES
    )


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
