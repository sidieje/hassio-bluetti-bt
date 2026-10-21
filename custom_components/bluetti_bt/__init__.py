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
    Platform.NUMBER,
]


def entry_config(entry: ConfigEntry) -> FullDeviceConfig | None:
    """Config for an entry.

    The options flow writes its values into both the entry data and the
    entry options, and the options are what the user changed last, so they
    win where the two disagree.
    """
    return FullDeviceConfig.from_dict({**entry.data, **entry.options})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bluetti Powerstation from a config entry."""

    config = entry_config(entry)

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
    coordinator = PollingCoordinator(hass, config, lock, entry)

    def forget_coordinator() -> None:
        """Drop this coordinator from hass.data.

        Guarded by identity: a torn down setup attempt must not remove a
        coordinator that a later attempt has stored in its place.
        """
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)

        if entry_data is None:
            return

        if entry_data.get(DATA_COORDINATOR) is coordinator:
            entry_data.pop(DATA_COORDINATOR, None)

        if not entry_data:
            hass.data[DOMAIN].pop(entry.entry_id, None)

    # Registered before the first refresh, because Home Assistant runs the
    # entry's on_unload callbacks when setup returns False, raises, or ends in
    # ConfigEntryNotReady. Without them a failed attempt leaves its bluetooth
    # connection and its offline retry timer running, and they pile up on
    # every retry. Callbacks run in reverse registration order, so the
    # coordinator is unloaded before its entry data is dropped.
    entry.async_on_unload(forget_coordinator)
    entry.async_on_unload(coordinator.async_unload)

    hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR] = coordinator

    # A failed first poll raises ConfigEntryNotReady, which makes Home
    # Assistant retry the whole setup with its own backoff. The teardown above
    # has already closed the connection and cancelled the pending retry timer
    # by then, so nothing is left running between attempts.
    await coordinator.async_config_entry_first_refresh()

    # Options are written to the entry by the options flow; apply them to the
    # coordinator that is running rather than waiting for a restart.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    logger.debug("Creating entities")
    # Setup platforms
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:  # pylint: disable=broad-except
        # Home Assistant leaves the platforms it already forwarded loaded when
        # setup fails, so unload them here. A failure while unloading is logged
        # separately and must not hide the error that ended setup.
        logger.exception("Setting up the Bluetti platforms failed")

        try:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except Exception as unload_err:  # pylint: disable=broad-except
            logger.warning(
                "Error unloading platforms after a setup failure: %s", unload_err
            )

        raise

    logger.debug("Setup done")

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed entry options to the running coordinator."""

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get(DATA_COORDINATOR)

    if not isinstance(coordinator, PollingCoordinator):
        return

    config = entry_config(entry)

    if config is None:
        coordinator.logger.warning(
            "Options for '%s' were ignored: the entry data could not be parsed",
            entry.title,
        )
        return

    await coordinator.apply_options(config)


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
