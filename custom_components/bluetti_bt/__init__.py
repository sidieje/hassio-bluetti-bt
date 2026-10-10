"""Bluetti Bluetooth Integration"""

from __future__ import annotations
import asyncio
import re
import logging
from typing import List
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import UpdateFailed

from .utils import mac_loggable
from .const import (
    DATA_COORDINATOR,
    DOMAIN,
    MANUFACTURER,
)
from .types import FullDeviceConfig
from .coordinator import PollingCoordinator

PLATFORMS: List[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bluetti Powerstation from a config entry."""

    config = FullDeviceConfig.from_dict(entry.data)

    if config is None:
        logging.getLogger(__name__).error(
            "Failed to parse config entry data for '%s'. "
            "This may happen when upgrading from v0.1.6 to v0.2.x "
            "due to missing 'use_encryption' field.",
            entry.title,
        )
        return False

    logger = logging.getLogger(
        f"{__name__}.{mac_loggable(config.address).replace(':', '_')}"
    )

    logger.debug("Init Bluetti BT Integration")

    # Create data structure
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})

    # Create lock
    lock = asyncio.Lock()

    # Create coordinator for polling
    logger.debug("Creating coordinator")
    coordinator = PollingCoordinator(
        hass,
        config,
        lock,
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except (UpdateFailed, TimeoutError, OSError) as err:
        logger.warning(
            "Initial Bluetti poll failed; continuing setup and retrying later: %s",
            err,
        )
    hass.data[DOMAIN][entry.entry_id].setdefault(DATA_COORDINATOR, coordinator)

    logger.debug("Creating entities")
    # Setup platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    logger.debug("Setup done")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if not unload_ok:
        return False

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get(DATA_COORDINATOR)

    if isinstance(coordinator, PollingCoordinator):
        # Close the persistent bluetooth connection
        await coordinator.async_unload()

    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok


def device_info(entry: ConfigEntry):
    """Device info."""
    config = FullDeviceConfig.from_dict(entry.data)

    if config is None:
        return None

    return DeviceInfo(
        identifiers={(DOMAIN, config.address)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=config.dev_type,
    )


def get_unique_id(name: str, sensor_type: str | None = None):
    """Generate an unique id."""
    res = re.sub("[^A-Za-z0-9]+", "_", name).lower()
    if sensor_type is not None:
        return f"{sensor_type}.{res}"
    return res
