"""Tests for hiding the pack entities of empty slots.

The library reports the slots that hold a pack, and the integration uses that
to keep the entities of the empty ones out of the entity registry.
"""

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant
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

    def watch(self, data, max_packs: int = 4):
        hass = Mock()
        entry = Mock()
        coordinator = Mock()
        coordinator.data = data
        # The watcher compares the reported slots against the slots the model
        # has room for, so the device has to say how many it has.
        coordinator.bluetti_device = SimpleNamespace(max_packs=max_packs)

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

    def test_watcher_installed_without_a_baseline(self):
        """An entry loaded offline, or on a first walk that lost a pack, has no
        slots to compare against yet; the watcher still has to be listening."""
        for data in (None, {}, {"pack_voltage": 52.0}):
            with self.subTest(data=data):
                _, _, listeners = self.watch(data)

                self.assertEqual(len(listeners), 1)

    def test_first_reading_seeds_the_baseline(self):
        """The first reading that names the slots becomes what was applied, so
        the two-poll rule counts from it rather than from nothing."""
        hass, coordinator, listeners = self.watch(None, max_packs=2)

        coordinator.data = {"pack_slots": [1, 2]}
        listeners[0]()

        self.assertFalse(hass.config_entries.async_schedule_reload.called)

        # Seeded, so a change now needs to hold for a second poll as usual.
        coordinator.data = {"pack_slots": [1, 2, 3]}
        listeners[0]()

        self.assertFalse(hass.config_entries.async_schedule_reload.called)

        listeners[0]()

        self.assertTrue(hass.config_entries.async_schedule_reload.called)

    def test_seed_reloads_when_slots_are_empty(self):
        """Setup registered every slot enabled, so a station that leaves some
        of them empty has to be set up again to register them disabled."""
        hass, coordinator, listeners = self.watch(None, max_packs=4)

        coordinator.data = {"pack_slots": [2, 3]}
        listeners[0]()

        self.assertTrue(hass.config_entries.async_schedule_reload.called)

    def test_seed_does_not_reload_twice(self):
        """The reload gives setup a real baseline, so the watcher installed by
        the new setup never seeds and the entry is not reloaded again."""
        hass, coordinator, listeners = self.watch(None, max_packs=4)

        coordinator.data = {"pack_slots": [2, 3]}
        listeners[0]()

        self.assertTrue(hass.config_entries.async_schedule_reload.called)

        # The same watcher keeps running until the reload replaces it; a later
        # poll must not reload again just because the slots are still partial.
        hass.config_entries.async_schedule_reload.reset_mock()

        for _ in range(3):
            listeners[0]()

        self.assertFalse(hass.config_entries.async_schedule_reload.called)

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

    def strict_watch(self, data):
        """Watch with a hass that behaves like asyncio does.

        A plain Mock accepts anything, so it happily took the None that
        async_schedule_reload returns and handed it to create_task. Give the
        double asyncio's refusal to schedule a non-coroutine instead.
        """
        hass = Mock(spec=HomeAssistant)
        entry = Mock()
        coordinator = Mock()
        coordinator.data = data

        def create_task(target, **_kwargs):
            if not asyncio.iscoroutine(target):
                raise TypeError(f"a coroutine was expected, got {target!r}")
            return Mock()

        hass.async_create_task.side_effect = create_task

        watch_pack_slots(hass, entry, coordinator, logging.getLogger())
        listener = coordinator.async_add_listener.call_args[0][0]

        return hass, entry, coordinator, [listener]

    def test_reload_is_not_wrapped_in_a_task(self):
        """The reload survives being scheduled the way asyncio expects.

        async_schedule_reload is a callback that creates its own task and
        returns None. Wrapping that in async_create_task raised TypeError
        inside the coordinator listener, and async_update_listeners has no
        per-listener guard, so the reload never happened.
        """
        hass, entry, coordinator, listeners = self.strict_watch({"pack_slots": [2, 3]})

        coordinator.data = {"pack_slots": [2, 3, 4]}
        listeners[0]()
        listeners[0]()

        hass.config_entries.async_schedule_reload.assert_called_once_with(
            entry.entry_id
        )
        hass.async_create_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
