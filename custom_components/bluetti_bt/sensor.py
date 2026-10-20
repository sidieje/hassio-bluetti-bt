"""Bluetti BT sensors."""

from __future__ import annotations
from enum import Enum
import logging
from decimal import Decimal
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from bluetti_bt_lib import build_device, FieldName, get_unit
from bluetti_bt_lib.fields import NumberField

from . import device_info as dev_info, get_unique_id, FullDeviceConfig
from .const import DATA_COORDINATOR, DOMAIN
from .coordinator import PollingCoordinator
from .pack_slots import apply_pack_visibility, occupied_slots, watch_pack_slots
from .utils import mac_loggable, unique_id_logable
from .types import get_device_class, get_state_class, get_category


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Setup sensor entities."""

    config = FullDeviceConfig.from_dict(entry.data)
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

    logger = logging.getLogger(
        f"{__name__}.{mac_loggable(config.address).replace(':', '_')}"
    )

    if config is None or not isinstance(coordinator, PollingCoordinator):
        logger.error("No coordinator found")
        return None

    # Generate device info
    logger.info("Creating sensors for device with address %s", config.address)
    device_info = dev_info(entry)

    # Add sensors
    bluetti_device = build_device(config.name)

    sensors_to_add = []
    sensor_fields: list[tuple[object, bool]] = [
        (field, False) for field in bluetti_device.get_sensor_fields()
    ]

    if config.use_encryption:
        # Encrypted stations also report each select register as a plain
        # reading, which shows up next to the real select entity.
        sensor_fields += [
            (field, True) for field in bluetti_device.get_select_fields()
        ]

    for field, is_mirror in sensor_fields:
        field_name = FieldName(field.name)

        if field_name == FieldName.PACK_SELECTED:
            continue

        # Writable numeric registers are exposed by the number platform.
        if isinstance(field, NumberField):
            continue

        unit = get_unit(field_name)
        device_class = get_device_class(field_name)
        state_class = get_state_class(field_name)
        # Read-only mirrors of a control entity are informational, so they
        # stay out of the main Sensors section.
        category = (
            EntityCategory.DIAGNOSTIC
            if is_mirror
            else get_category(field_name, writable=field.is_writeable())
        )

        if unit is not None:
            sensors_to_add.append(
                BluettiSensor(
                    coordinator,
                    device_info,
                    field.address,
                    field.name,
                    unit_of_measurement=unit,
                    device_class=device_class,
                    state_class=state_class,
                    category=category,
                    logger=logger,
                )
            )
        else:
            sensors_to_add.append(
                BluettiSensor(
                    coordinator,
                    device_info,
                    field.address,
                    field.name,
                    category=category,
                    logger=logger,
                )
            )

    # Pack fields
    # The library polls each pack and returns its values as pack_N_<field>
    # keys. They belong on the main device: separate pack devices only add
    # clutter, and empty slots would show up as phantom devices.
    pack_slots = occupied_slots(coordinator.data)
    pack_entities: dict[int, list[BluettiSensor]] = {}

    for field in bluetti_device.pack_fields:
        field_name = FieldName(field.name)

        if field_name in [FieldName.PACK_SELECTED]:
            continue

        unit = get_unit(field_name)
        device_class = get_device_class(field_name)
        state_class = get_state_class(field_name)
        category = get_category(field_name)

        for num in range(1, bluetti_device.max_packs + 1):
            sensor = BluettiSensor(
                coordinator,
                device_info,
                field.address,
                field.name,
                unit_of_measurement=unit,
                device_class=device_class,
                state_class=state_class,
                category=category,
                pack_num=num,
                # A slot the station reports as empty gets its entities
                # registered disabled rather than shown as unavailable.
                registry_enabled=pack_slots is None or num in pack_slots,
                logger=logger,
            )

            sensors_to_add.append(sensor)
            pack_entities.setdefault(num, []).append(sensor)

    # Entries already in the registry keep the visibility they have, so the
    # slots that changed since the last setup are moved here.
    apply_pack_visibility(hass, pack_entities, pack_slots, logger)

    async_add_entities(sensors_to_add)

    watch_pack_slots(hass, entry, coordinator, logger)


class BluettiSensor(CoordinatorEntity, SensorEntity):
    """Bluetti universal sensor."""

    def __init__(
        self,
        coordinator: PollingCoordinator,
        device_info: DeviceInfo,
        address,
        response_key: str,
        unit_of_measurement: str | None = None,
        device_class: str | None = None,
        state_class: str | None = None,
        category: EntityCategory | None = None,
        options: list[str] | None = None,
        pack_num: int | None = None,
        registry_enabled: bool = True,
        logger: logging.Logger = logging.getLogger(),
    ):
        """Init sensor entity."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._pack_num = pack_num
        self._logger = logger

        self._attr_has_entity_name = True

        # Pack entities used to sit on their own device, whose name carried
        # the pack label. Keeping that label in the unique id lets existing
        # entities survive the move onto the main device.
        base_name = device_info.get("name")
        if pack_num is not None:
            base_name = f"{base_name} Battery Pack {pack_num}"

        e_name = f"{base_name} {response_key}"

        self._address = address
        self._response_key = (
            f"pack_{pack_num}_{response_key}" if pack_num else response_key
        )
        self._unavailable_counter = 0

        self._attr_device_info = device_info
        self._attr_translation_key = (
            f"pack_{response_key}" if pack_num else response_key
        )

        if pack_num is not None:
            self._attr_translation_placeholders = {"pack_num": pack_num}

        self._attr_available = False
        # Empty pack slots are registered disabled so they stay off the
        # device page instead of showing up as unavailable readings.
        self._attr_entity_registry_enabled_default = registry_enabled
        self._attr_unique_id = get_unique_id(e_name)
        self._attr_native_unit_of_measurement = unit_of_measurement
        # The device page shows a battery badge for the first battery-class
        # sensor of the device, chosen by name order, so "Battery Pack 1 SOC"
        # would replace the station's total SOC there. Only the total reading
        # keeps the battery device class.
        if pack_num is not None and device_class == SensorDeviceClass.BATTERY:
            device_class = None
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_entity_category = category
        self._options = options

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self._attr_available

    def _set_available(self):
        """Set sensor as available."""
        self._attr_available = True
        self._unavailable_counter = 0
        self._attr_extra_state_attributes = {}
        self.async_write_ha_state()

    def _set_unavailable(self, cause: str = "Unknown"):
        """Set sensor as unavailable."""
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

        if not isinstance(self.coordinator.data, dict):
            self._logger.warning(
                "Invalid data from coordinator (sensor.%s)",
                unique_id_logable(self._attr_unique_id),
            )
            self._set_unavailable("Invalid data")
            return

        self._logger.debug(
            "Coordinator data: %s",
            self.coordinator.data,
        )

        response_data = self.coordinator.data.get(self._response_key)
        if response_data is None:
            self._logger.debug("No data for available for (%s)", self._response_key)
            self._set_unavailable("No data")
            return

        if (
            not isinstance(response_data, int)
            and not isinstance(response_data, float)
            and not isinstance(response_data, complex)
            and not isinstance(response_data, Decimal)
            and not isinstance(response_data, Enum)
            and not isinstance(response_data, str)
        ):
            self._logger.warning(
                "Invalid response data type from coordinator (sensor.%s): %s has type %s",
                unique_id_logable(self._attr_unique_id),
                response_data,
                type(response_data),
            )
            self._set_unavailable("Invalid data type")
            return

        self._set_available()

        # Different for enum and numeric
        if isinstance(response_data, Enum):
            # Enum
            self._attr_native_value = response_data.name
        else:
            # Numeric
            self._attr_native_value = response_data
        self.async_write_ha_state()
