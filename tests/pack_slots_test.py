"""Tests for hiding the pack entities of empty slots.

The library reports the slots that hold a pack, and the integration uses that
to keep the entities of the empty ones out of the entity registry.
"""

import logging
import unittest
from unittest.mock import Mock, patch

from homeassistant.helpers import entity_registry as er

from custom_components.bluetti_bt.pack_slots import (
    apply_pack_visibility,
    occupied_slots,
    watch_pack_slots,
)


class FakeEntry:
    """Stand in for an entity registry entry."""

    def __init__(self, entity_id: str, disabled_by: str | None = None):
        self.entity_id = entity_id
        self.disabled_by = disabled_by
        self.disabled = disabled_by is not None


class FakeRegistry:
    """Stand in for the entity registry, keyed by unique id."""

    def __init__(self, entries: dict[str, FakeEntry]):
        self.entries = entries
        self.updates: list[tuple[str, str | None]] = []

    def async_get_entity_id(self, domain, platform, unique_id):
        entry = self.entries.get(unique_id)
        return entry.entity_id if entry else None

    def async_get(self, entity_id):
        for entry in self.entries.values():
            if entry.entity_id == entity_id:
                return entry
        return None

    def async_update_entity(self, entity_id, **kwargs):
        self.updates.append((entity_id, kwargs.get("disabled_by")))

        entry = self.async_get(entity_id)
        entry.disabled_by = kwargs.get("disabled_by")
        entry.disabled = entry.disabled_by is not None
        return entry


def entity(unique_id: str) -> Mock:
    stand_in = Mock()
    stand_in.unique_id = unique_id
    return stand_in


class TestOccupiedSlots(unittest.TestCase):
    """The pack_slots key says which slots are occupied, its absence says nothing."""

    def test_slots_are_read_from_the_poll(self):
        self.assertEqual(occupied_slots({"pack_slots": [2, 3, 4]}), {2, 3, 4})

    def test_empty_tower_reports_no_slots(self):
        self.assertEqual(occupied_slots({"pack_slots": []}), set())

    def test_missing_key_is_no_opinion(self):
        self.assertIsNone(occupied_slots({"pack_voltage": 52.0}))

    def test_failed_poll_is_no_opinion(self):
        self.assertIsNone(occupied_slots(None))

    def test_non_dict_data_is_no_opinion(self):
        self.assertIsNone(occupied_slots("offline"))


class TestApplyPackVisibility(unittest.TestCase):
    """Registry entries already created keep their state, so the integration moves them."""

    def apply(self, entries, slots):
        registry = FakeRegistry(entries)

        with patch.object(er, "async_get", return_value=registry):
            changed = apply_pack_visibility(
                Mock(),
                {2: [entity("pack_2"), entity("pack_2_soc")], 1: [entity("pack_1")]},
                slots,
                logging.getLogger(),
            )

        return registry, changed

    def test_empty_slot_entries_are_disabled(self):
        registry, changed = self.apply(
            {
                "pack_1": FakeEntry("sensor.ac300_pack_1"),
                "pack_2": FakeEntry("sensor.ac300_pack_2"),
                "pack_2_soc": FakeEntry("sensor.ac300_pack_2_soc"),
            },
            {2},
        )

        self.assertEqual(changed, 1)
        self.assertEqual(
            registry.updates, [("sensor.ac300_pack_1", er.RegistryEntryDisabler.INTEGRATION)]
        )

    def test_occupied_slot_entries_are_re_enabled(self):
        """A pack inserted since the last setup comes back on its own."""
        registry, changed = self.apply(
            {
                "pack_1": FakeEntry("sensor.ac300_pack_1", er.RegistryEntryDisabler.INTEGRATION),
                "pack_2": FakeEntry("sensor.ac300_pack_2"),
                "pack_2_soc": FakeEntry("sensor.ac300_pack_2_soc"),
            },
            {1, 2},
        )

        self.assertEqual(changed, 1)
        self.assertEqual(registry.updates, [("sensor.ac300_pack_1", None)])

    def test_entries_the_user_disabled_are_left_alone(self):
        registry, changed = self.apply(
            {
                "pack_1": FakeEntry("sensor.ac300_pack_1", er.RegistryEntryDisabler.USER),
                "pack_2": FakeEntry("sensor.ac300_pack_2"),
                "pack_2_soc": FakeEntry("sensor.ac300_pack_2_soc"),
            },
            {2},
        )

        self.assertEqual(changed, 0)
        self.assertEqual(registry.updates, [])

    def test_first_setup_has_no_entries_to_move(self):
        """The platform applies the entity's own default when it creates them."""
        registry, changed = self.apply({}, {2})

        self.assertEqual(changed, 0)
        self.assertEqual(registry.updates, [])

    def test_no_opinion_changes_nothing(self):
        registry, changed = self.apply(
            {
                "pack_1": FakeEntry("sensor.ac300_pack_1", er.RegistryEntryDisabler.INTEGRATION),
                "pack_2": FakeEntry("sensor.ac300_pack_2"),
                "pack_2_soc": FakeEntry("sensor.ac300_pack_2_soc"),
            },
            None,
        )

        self.assertEqual(changed, 0)
        self.assertEqual(registry.updates, [])


class TestWatchPackSlots(unittest.TestCase):
    """A slot change only reloads the entry once it has held for two polls."""

    def watch(self, data):
        hass = Mock()
        entry = Mock()
        coordinator = Mock()
        coordinator.data = data

        watch_pack_slots(hass, entry, coordinator, logging.getLogger())

        if not coordinator.async_add_listener.called:
            return hass, coordinator, []

        # The listener is what async_add_listener was handed; what it returns
        # is the unsubscribe callback that gets registered on the entry.
        listener = coordinator.async_add_listener.call_args[0][0]

        entry.async_on_unload.assert_called_once_with(
            coordinator.async_add_listener.return_value
        )

        return hass, coordinator, [listener]

    def test_no_watcher_without_pack_slots(self):
        _, _, listeners = self.watch({"pack_voltage": 52.0})

        self.assertEqual(listeners, [])

    def test_reload_waits_for_the_change_to_hold(self):
        hass, coordinator, listeners = self.watch({"pack_slots": [2, 3]})

        self.assertEqual(len(listeners), 1)

        coordinator.data = {"pack_slots": [2, 3, 4]}
        listeners[0]()

        self.assertFalse(hass.config_entries.async_schedule_reload.called)

        listeners[0]()

        self.assertTrue(hass.config_entries.async_schedule_reload.called)

    def test_unchanged_slots_never_reload(self):
        hass, coordinator, listeners = self.watch({"pack_slots": [2, 3]})

        listeners[0]()
        coordinator.data = {"pack_slots": [2, 3]}
        listeners[0]()

        self.assertFalse(hass.config_entries.async_schedule_reload.called)

    def test_flapping_slots_never_reload(self):
        """A slot that answers with nothing every other poll must not reload
        the entry over and over."""
        hass, coordinator, listeners = self.watch({"pack_slots": [2, 3]})

        for slots in ([2], [2, 3], [2], [2, 3], [2], [2, 3]):
            coordinator.data = {"pack_slots": slots}
            listeners[0]()

        self.assertFalse(hass.config_entries.async_schedule_reload.called)

    def test_dropped_pack_still_needs_two_polls(self):
        hass, coordinator, listeners = self.watch({"pack_slots": [2, 3, 4]})

        coordinator.data = {"pack_slots": [2, 3]}
        listeners[0]()
        coordinator.data = {"pack_slots": [2, 3, 4]}
        listeners[0]()
        listeners[0]()

        self.assertFalse(hass.config_entries.async_schedule_reload.called)


if __name__ == "__main__":
    unittest.main()
