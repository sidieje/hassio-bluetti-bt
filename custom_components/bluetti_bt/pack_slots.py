"""Pack slot visibility.

The library reports which pack slots actually hold a pack. Entities for the
slots that are empty get registered disabled, so a tower with its packs in the
upper slots does not leave a row of unavailable sensors behind for every empty
slot.
"""

from __future__ import annotations

import logging
from typing import Any

from bluetti_bt_lib import FieldName
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import PollingCoordinator

SENSOR_DOMAIN = "sensor"


def occupied_slots(data: Any) -> set[int] | None:
    """Return the slots the last poll found packs in, None when it has no opinion.

    The key is absent for stations without pack registers, and a poll is
    dropped whole when a pack fails to answer, so a missing key means "unknown"
    rather than "no packs installed".
    """
    if not isinstance(data, dict):
        return None

    slots = data.get(FieldName.PACK_SLOTS.value)

    if not isinstance(slots, list):
        return None

    return {slot for slot in slots if isinstance(slot, int)}


def apply_pack_visibility(
    hass: HomeAssistant,
    entities_by_slot: dict[int, list[Any]],
    slots: set[int] | None,
    logger: logging.Logger,
) -> int:
    """Enable the entities of occupied slots and disable the empty ones.

    Runs before the entities are handed to the platform: an entry that is
    already in the registry keeps the visibility it has, so the integration
    moves it out of, or back into, view itself. Entries the user disabled by
    hand are left alone. Returns the number of entries it changed.
    """
    if slots is None:
        return 0

    registry = er.async_get(hass)
    changed = 0

    for slot, entities in entities_by_slot.items():
        occupied = slot in slots

        for entity in entities:
            entity_id = registry.async_get_entity_id(
                SENSOR_DOMAIN, DOMAIN, entity.unique_id
            )

            if entity_id is None:
                # First setup for this slot: the platform applies the entity's
                # own registry default when it creates the entry.
                continue

            entry = registry.async_get(entity_id)

            if entry is None:
                continue

            if occupied and entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
                registry.async_update_entity(entity_id, disabled_by=None)
            elif not occupied and not entry.disabled:
                registry.async_update_entity(
                    entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
                )
            else:
                continue

            changed += 1
            logger.debug(
                "Pack slot %s is %s, %s entity %s",
                slot,
                "occupied" if occupied else "empty",
                "enabling" if occupied else "disabling",
                entity_id,
            )

    return changed


def watch_pack_slots(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: PollingCoordinator,
    logger: logging.Logger,
) -> None:
    """Reload the config entry when the occupied pack slots change.

    Pack entities are created once per setup, so a pack that is added or moved
    only shows up once the entry has been set up again. The change has to hold
    for two consecutive polls before it is acted on, which keeps a slot that
    flaps between two sets from reloading the entry over and over.
    """
    applied = occupied_slots(coordinator.data)

    if applied is None:
        return

    pending: set[int] | None = None

    @callback
    def _check_slots(_: Any = None) -> None:
        nonlocal pending

        current = occupied_slots(coordinator.data)

        if current is None or current == applied:
            pending = None
            return

        if current != pending:
            pending = current
            return

        pending = None
        logger.info(
            "Pack slots changed from %s to %s, reloading the integration "
            "to refresh the pack entities",
            sorted(applied),
            sorted(current),
        )
        hass.async_create_task(
            hass.config_entries.async_schedule_reload(entry.entry_id)
        )

    entry.async_on_unload(coordinator.async_add_listener(_check_slots))
