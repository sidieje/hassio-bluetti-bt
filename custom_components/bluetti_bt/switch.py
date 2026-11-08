"""Bluetti BT switches."""

from __future__ import annotations
import logging
import async_timeout
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EntityCategory
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)

from bluetti_bt_lib import (
    BluettiDevice,
    DeviceField,
    FieldName,
)
from bluetti_bt_lib.fields import PulseSwitchField

from .types import FullDeviceConfig, get_category
from . import device_info as dev_info, get_unique_id
from .const import DATA_COORDINATOR, DOMAIN, UNAVAILABLE_AFTER_FAILED_CYCLES
from .coordinator import PollingCoordinator
from .utils import (
    build_bluetti_device,
    debounced_available,
    mac_loggable,
    unique_id_logable,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Setup switch entities."""

    config = FullDeviceConfig.from_dict(entry.data)
    if config is None:
        logger = logging.getLogger(__name__)
        logger.error("Unable to parse Bluetti config entry")
        return

    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

    logger = logging.getLogger(
        f"{__name__}.{mac_loggable(config.address).replace(':', '_')}"
    )

    if not isinstance(coordinator, PollingCoordinator):
        logger.error("No coordinator found")
        return None

    # Generate device info
    logger.info("Creating switches for device with address %s", config.address)
    device_info = dev_info(entry)

    # Add switches
    bluetti_device = build_bluetti_device(config.dev_type, config.name)

    switches_to_add = []
    switch_fields = bluetti_device.get_switch_fields()
    for field in switch_fields:
        category = get_category(FieldName(field.name))

        switches_to_add.append(
            BluettiSwitch(
                bluetti_device,
                config.address,
                coordinator,
                device_info,
                field,
                use_encryption=config.use_encryption,
                category=category,
                logger=logger,
            )
        )

    async_add_entities(switches_to_add)


class BluettiSwitch(CoordinatorEntity, SwitchEntity):
    """Bluetti universal switch."""

    def __init__(
        self,
        bluetti_device: BluettiDevice,
        address: str,
        coordinator: PollingCoordinator,
        device_info: DeviceInfo,
        field: DeviceField,
        use_encryption: bool = False,
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
        self._use_encryption = use_encryption
        self._unavailable_counter = 0

        self._attr_has_entity_name = True
        self._attr_device_info = device_info
        self._attr_translation_key = field.name
        self._attr_available = False
        self._attr_unique_id = get_unique_id(e_name)
        self._attr_entity_category = category

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return debounced_available(self.coordinator, self._attr_available)

    def _set_available(self):
        """Set switch as available."""
        self._attr_available = True
        self._unavailable_counter = 0
        self._attr_extra_state_attributes = {}
        self.async_write_ha_state()

    def _set_unavailable(self, cause: str = "Unknown"):
        """Set switch as unavailable."""
        # Capped at the threshold, because the attributes are published with the
        # counter in them: once the entity is unavailable Home Assistant skips
        # the state write when nothing has changed, but only if the counter has
        # stopped moving. An outage would otherwise rewrite every entity of a
        # tower station on every cycle for as long as it lasted.
        if self._unavailable_counter < UNAVAILABLE_AFTER_FAILED_CYCLES:
            self._unavailable_counter += 1

        self._attr_extra_state_attributes = {
            "unavailable_counter": self._unavailable_counter,
            "unavailable_cause": cause,
        }

        if self._unavailable_counter >= UNAVAILABLE_AFTER_FAILED_CYCLES:
            self._attr_available = False

        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        # A failed cycle reaches the entities through the coordinator's own
        # notification, and coordinator.data is still the last good dict rather
        # than None because Home Assistant leaves it alone when the refresh
        # raises. Treating that as fresh data would publish the stale readings
        # as available and clear the debounce.
        if not self.coordinator.last_update_success:
            self._set_unavailable("Polling failed")
            return

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
                "Invalid data from coordinator (switch.%s)",
                unique_id_logable(self._attr_unique_id),
            )
            self._set_unavailable("Invalid data")
            return

        response_data = self.coordinator.data.get(self._response_key)
        if response_data is None:
            self._set_unavailable("No data")
            return

        if not isinstance(response_data, bool):
            self._logger.warning(
                "Invalid response data type from coordinator (switch.%s): %s",
                unique_id_logable(self._attr_unique_id),
                response_data,
            )
            self._set_unavailable("Invalid data type")
            return

        self._set_available()
        self._attr_is_on = response_data is True
        self.async_write_ha_state()

    def _is_pulse_switch(self) -> bool:
        """Whether this switch is driven by a pulse in another register.

        A PulseSwitchField reports its state in one word (the standby state sits
        at 174) and is driven by a fixed value written to another (2 to 2013).
        That write is a power button: it moves the station whichever way it is
        asked, so the value carried says nothing about the state being asked for.
        """
        return isinstance(self._field, PulseSwitchField)

    def _reported_state(self) -> bool | None:
        """The state the station last reported, or None when it has not said."""
        data = self.coordinator.data
        if isinstance(data, dict):
            value = data.get(self._response_key)
            if isinstance(value, bool):
                return value

        if isinstance(self._attr_is_on, bool):
            return self._attr_is_on

        return None

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        self._logger.debug(
            "Turn on %s on %s", self._response_key, mac_loggable(self._address)
        )
        await self.write_to_device(True)

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        self._logger.debug(
            "Turn off %s on %s", self._response_key, mac_loggable(self._address)
        )
        await self.write_to_device(False)

    async def write_to_device(self, state: bool):
        """Write to the device and verify the reported state."""
        self._logger.debug(
            "Write %s=%s on %s",
            self._response_key,
            state,
            mac_loggable(self._address),
        )

        if self._is_pulse_switch() and self._reported_state() == state:
            # The pulse would still move the station, and it would move it out
            # of the state being asked for, so the read-back would come back
            # disagreeing with a request the station was already honouring.
            # Nothing needs to be sent for a state the station reports.
            self._logger.debug(
                "Not writing %s=%s on %s: the station already reports that state",
                self._response_key,
                state,
                mac_loggable(self._address),
            )
            return

        try:
            async with async_timeout.timeout(60):
                result = await self.coordinator.async_write_verified(
                    self._field.name, state
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
                "the state will update on the next poll"
            )

        if not result.verified:
            reported = (result.values or {}).get(self._response_key)

            if self._is_pulse_switch() and reported is not None:
                # The read-back answers awake or standby and nothing in
                # between, so a pulse that was not confirmed left the station
                # where it was rather than half way to the new state. Saying the
                # station did not move is both the truth and the useful report.
                self._logger.warning(
                    "Pulse sent for %s, but the station still reports %s",
                    self._response_key,
                    reported,
                )
                raise ServiceValidationError(
                    f"The Bluetti station did not change {self._response_key}; "
                    f"it still reports {self._response_key}={reported}"
                )

            self._logger.warning(
                "Write state mismatch for %s: requested %s; device reports %s",
                self._response_key,
                state,
                reported,
            )
            raise ServiceValidationError(
                f"The Bluetti device reports {self._response_key}={reported}, not {state}"
            )

        self._logger.debug(
            "Write verified for %s: %s",
            self._response_key,
            state,
        )
