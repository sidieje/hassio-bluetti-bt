"""Bluetti BT switches."""

from __future__ import annotations
import logging
import async_timeout
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from bluetti_bt_lib import build_device, BluettiDevice, FieldName
from bluetti_bt_lib.fields import SelectField

from .types import FullDeviceConfig, get_category
from . import device_info as dev_info, get_unique_id
from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import PollingCoordinator
from .utils import mac_loggable, unique_id_logable


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Setup select entities."""

    config = FullDeviceConfig.from_dict(entry.data)
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

    logger = logging.getLogger(
        f"{__name__}.{mac_loggable(config.address).replace(':', '_')}"
    )

    if config is None or not isinstance(coordinator, PollingCoordinator):
        logger.error("No coordinator found")
        return None

    # Generate device info
    logger.info("Creating selects for device with address %s", config.address)
    device_info = dev_info(entry)

    # Add switches
    bluetti_device = build_device(config.name)

    switches_to_add = []
    switch_fields = bluetti_device.get_select_fields()
    for field in switch_fields:
        category = get_category(FieldName(field.name))

        switches_to_add.append(
            BluettiSelect(
                bluetti_device,
                config.address,
                coordinator,
                device_info,
                field,
                category=category,
                logger=logger,
            )
        )

    async_add_entities(switches_to_add)


class BluettiSelect(CoordinatorEntity, SelectEntity):
    """Bluetti universal switch."""

    def __init__(
        self,
        bluetti_device: BluettiDevice,
        address: str,
        coordinator: PollingCoordinator,
        device_info: DeviceInfo,
        field: SelectField,
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
        self._attr_options = [e.name for e in field.e]

        self._attr_has_entity_name = True
        self._attr_device_info = device_info
        self._attr_translation_key = field.name
        self._attr_available = False
        self._attr_unique_id = get_unique_id(e_name)
        self._attr_entity_category = category

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._attr_available

    def _set_available(self):
        """Set switch as available."""
        self._attr_available = True
        self._unavailable_counter = 0
        self._attr_extra_state_attributes = {}
        self.async_write_ha_state()

    def _set_unavailable(self, cause: str = "Unknown"):
        """Set switch as unavailable."""
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
                "Invalid data from coordinator (select.%s)",
                unique_id_logable(self._attr_unique_id),
            )
            self._set_unavailable("Invalid data")
            return

        response_data = self.coordinator.data.get(self._response_key)
        if response_data is None:
            self._set_unavailable("No data")
            return

        if not isinstance(response_data, self._field.e):
            self._logger.warning(
                "Invalid response data type from coordinator (select.%s): %s",
                unique_id_logable(self._attr_unique_id),
                response_data,
            )
            self._set_unavailable("Invalid data type")
            return

        self._set_available()
        self.current_option = response_data.name
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Set the entity to value."""
        if option not in self._attr_options:
            raise ServiceValidationError(
                f"{option} is not a valid option for {self._response_key}",
                translation_domain=DOMAIN,
                translation_key="invalid_option",
                translation_placeholders={
                    "field": self._response_key,
                    "option": option,
                },
            )

        self._logger.debug(
            "Set %s on %s to %s",
            self._response_key,
            mac_loggable(self._address),
            option,
        )
        await self.write_to_device(option)

    async def write_to_device(self, option: str) -> None:
        """Write the option and confirm it against what the device reports.

        The library is given the option name, which is what its command
        builder turns into the register value, and it compares the read-back
        Enum member against the option that name refers to. Its verdict is
        therefore the same comparison this entity would make, so the entity
        trusts it; the write only counts as done when the device agrees.
        """
        try:
            async with async_timeout.timeout(60):
                result = await self.coordinator.async_write_verified(
                    self._field.name, option
                )
        except (TimeoutError, ConnectionError) as err:
            self._logger.error(
                "Write operation failed for %s: %s", self._response_key, err
            )
            raise HomeAssistantError(
                f"Bluetti could not reach the device to change {self._response_key}",
                translation_domain=DOMAIN,
                translation_key="select_write_unreachable",
                translation_placeholders={"field": self._response_key},
            ) from err

        if result is None or not result.write_succeeded:
            self._logger.error(
                "Write of %s=%s was rejected; the device did not change",
                self._response_key,
                option,
            )
            raise HomeAssistantError(
                f"The Bluetti device did not accept {option} for {self._response_key}",
                translation_domain=DOMAIN,
                translation_key="select_write_rejected",
                translation_placeholders={
                    "field": self._response_key,
                    "option": option,
                },
            )

        if not result.readback_succeeded:
            self._logger.warning(
                "Write of %s=%s was accepted, but the device would not read back",
                self._response_key,
                option,
            )
            raise HomeAssistantError(
                f"The Bluetti device did not confirm {self._response_key}; "
                "the value may only show up on the next poll",
                translation_domain=DOMAIN,
                translation_key="select_write_unconfirmed",
                translation_placeholders={"field": self._response_key},
            )

        if not result.verified:
            reported = (result.values or {}).get(self._response_key)
            reported_label = getattr(reported, "name", reported)
            self._logger.warning(
                "Write value mismatch for %s: requested %s; device reports %s",
                self._response_key,
                option,
                reported_label,
            )
            raise HomeAssistantError(
                f"The Bluetti device reports {self._response_key}={reported_label}, "
                f"not {option}",
                translation_domain=DOMAIN,
                translation_key="select_write_mismatch",
                translation_placeholders={
                    "field": self._response_key,
                    "option": option,
                    "reported": reported_label,
                },
            )

        # The coordinator merged the read-back into its data, which is what
        # leaves the entity showing the value the device actually holds.
        self._logger.debug(
            "Write verified for %s: %s", self._response_key, option
        )

