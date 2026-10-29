"""Bluetti BT numbers."""

from __future__ import annotations
import logging
import async_timeout
from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from bluetti_bt_lib import BluettiDevice, FieldName, get_unit
from bluetti_bt_lib.fields import NumberField

from .types import FullDeviceConfig, get_category
from . import device_info as dev_info, get_unique_id
from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import PollingCoordinator
from .utils import build_bluetti_device, mac_loggable, unique_id_logable

# Registers that only make sense in a given order relative to each other.
# Maps field -> (counterpart field, direction the value must keep).
ORDERED_PAIRS: dict[FieldName, tuple[FieldName, str]] = {
    FieldName.BATTERY_SOC_RANGE_START: (FieldName.BATTERY_SOC_RANGE_END, "below"),
    FieldName.BATTERY_SOC_RANGE_END: (FieldName.BATTERY_SOC_RANGE_START, "above"),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Setup number entities."""

    config = FullDeviceConfig.from_dict(entry.data)
    if config is None:
        logging.getLogger(__name__).error("Unable to parse Bluetti config entry")
        return

    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

    logger = logging.getLogger(
        f"{__name__}.{mac_loggable(config.address).replace(':', '_')}"
    )

    if not isinstance(coordinator, PollingCoordinator):
        logger.error("No coordinator found")
        return None

    # Generate device info
    logger.info("Creating numbers for device with address %s", config.address)
    device_info = dev_info(entry)

    # Add numbers
    bluetti_device = build_bluetti_device(config.dev_type, config.name)

    numbers_to_add = []
    for field in bluetti_device.fields:
        if not isinstance(field, NumberField):
            continue

        numbers_to_add.append(
            BluettiNumber(
                bluetti_device,
                config.address,
                coordinator,
                device_info,
                field,
                category=get_category(FieldName(field.name)),
                logger=logger,
            )
        )

    async_add_entities(numbers_to_add)


class BluettiNumber(CoordinatorEntity, NumberEntity):
    """Bluetti universal number."""

    def __init__(
        self,
        bluetti_device: BluettiDevice,
        address: str,
        coordinator: PollingCoordinator,
        device_info: DeviceInfo,
        field: NumberField,
        category: EntityCategory | None = None,
        logger: logging.Logger = logging.getLogger(),
    ):
        """Init entity."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._logger = logger

        e_name = f"{device_info.get('name')} {field.name}"
        self._bluetti_device = bluetti_device
        self._address = address
        self._field = field
        self._response_key = field.name
        self._unavailable_counter = 5

        unit = get_unit(FieldName(field.name))

        self._attr_has_entity_name = True
        self._attr_device_info = device_info
        self._attr_translation_key = field.name
        self._attr_available = False
        self._attr_unique_id = get_unique_id(e_name)
        self._attr_entity_category = category
        self._attr_native_unit_of_measurement = unit

        if unit == PERCENTAGE:
            self._attr_device_class = NumberDeviceClass.BATTERY

        self._attr_native_min_value = field.min if field.min is not None else 0
        self._attr_native_max_value = field.max if field.max is not None else 100
        # The library's step is a presentation hint for the UI slider; the
        # settings registers accept whole units, so the profiles declare 1.
        self._attr_native_step = field.step if field.step else 1
        self._attr_mode = NumberMode.AUTO

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._attr_available

    def _set_available(self):
        """Set number as available."""
        self._attr_available = True
        self._unavailable_counter = 0
        self._attr_extra_state_attributes = {}
        self.async_write_ha_state()

    def _set_unavailable(self, cause: str = "Unknown"):
        """Set number as unavailable."""
        self._unavailable_counter += 1

        self._attr_extra_state_attributes = {
            "unavailable_counter": self._unavailable_counter,
            "unavailable_cause": cause,
        }

        if self._unavailable_counter >= 5:
            self._attr_available = False

        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        if self.coordinator.data is None:
            self._logger.debug(
                "Data from coordinator is None",
            )
            self._set_unavailable("Data is None")
            return

        self._logger.debug(
            "Updating state of %s", unique_id_logable(self._attr_unique_id)
        )
        if not isinstance(self.coordinator.data, dict):
            self._logger.debug(
                "Invalid data from coordinator (number.%s)",
                unique_id_logable(self._attr_unique_id),
            )
            self._set_unavailable("Invalid data")
            return

        response_data = self.coordinator.data.get(self._response_key)
        if response_data is None:
            self._set_unavailable("No data")
            return

        if not isinstance(response_data, int) or isinstance(response_data, bool):
            self._logger.warning(
                "Invalid response data type from coordinator (number.%s): %s",
                unique_id_logable(self._attr_unique_id),
                response_data,
            )
            self._set_unavailable("Invalid data type")
            return

        self._set_available()
        self._attr_native_value = response_data
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Set the entity to a value."""
        target = int(round(value))

        if not self._field.allowed_write_type(target) or not self._field.in_range(
            target
        ):
            self._logger.warning(
                "Ignoring out of range value for %s: %s", self._response_key, value
            )
            raise ServiceValidationError(
                f"{self._response_key} only accepts values from "
                f"{self._field.min} to {self._field.max}"
            )

        refusal = self._ordering_refusal(target)
        if refusal is not None:
            raise ServiceValidationError(refusal)

        self._logger.debug(
            "Set %s on %s to %s",
            self._response_key,
            mac_loggable(self._address),
            target,
        )
        await self.write_to_device(target)

    def _ordering_refusal(self, target: int) -> str | None:
        """Return why the value breaks its register pair ordering, if it does."""
        pair = ORDERED_PAIRS.get(FieldName(self._field.name))

        if pair is None:
            return None

        counterpart, direction = pair
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        other = data.get(counterpart.value)

        if not isinstance(other, int) or isinstance(other, bool):
            # Nothing to compare against yet, let the device decide.
            return None

        if (direction == "below" and target >= other) or (
            direction == "above" and target <= other
        ):
            self._logger.warning(
                "Refusing %s=%s: it must stay %s %s (%s)",
                self._response_key,
                target,
                direction,
                counterpart.value,
                other,
            )
            return (
                f"{self._response_key} must stay {direction} {counterpart.value} "
                f"({other})"
            )

        return None

    async def write_to_device(self, value: int):
        """Write to the device and verify the reported value."""
        self._logger.debug(
            "Write %s=%s on %s",
            self._response_key,
            value,
            mac_loggable(self._address),
        )

        try:
            async with async_timeout.timeout(60):
                result = await self.coordinator.async_write_verified(
                    self._field.name, value
                )
        except (TimeoutError, ConnectionError) as err:
            self._logger.error(
                "Write operation failed for %s: %s",
                self._response_key,
                err,
            )
            raise ServiceValidationError(
                f"Bluetti could not reach the device to change {self._response_key}"
            ) from err

        if result is None or not result.write_succeeded:
            self._logger.error(
                "Write failed for %s; device state was not confirmed",
                self._response_key,
            )
            raise ServiceValidationError(
                f"The Bluetti device did not accept the change to {self._response_key}"
            )

        if not result.readback_succeeded:
            self._logger.warning(
                "Write sent for %s, but read-back failed; not retrying",
                self._response_key,
            )
            raise ServiceValidationError(
                f"The Bluetti device did not confirm {self._response_key}; "
                "the value will update on the next poll"
            )

        if not result.verified:
            reported = (result.values or {}).get(self._response_key)
            self._logger.warning(
                "Write value mismatch for %s: requested %s; device reports %s",
                self._response_key,
                value,
                reported,
            )
            raise ServiceValidationError(
                f"The Bluetti device reports {self._response_key}={reported}, not {value}"
            )

        self._logger.debug(
            "Write verified for %s: %s",
            self._response_key,
            value,
        )
