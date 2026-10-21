"""Tests for the Configure dialog."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.bluetti_bt.config_flow import (
    OptionsFlowHandler,
    has_battery_packs,
)
from custom_components.bluetti_bt.types import (
    CONF_ENCRYPTION_ASSUMED,
    CONF_PACK_POLL_INTERVAL,
    CONF_TYPE,
    CONF_USE_ENCRYPTION,
)

OPTIONS = {
    "polling_interval": 20,
    "polling_timeout": 45,
    "max_retries": 5,
    "pack_poll_interval": 2,
}

AC300 = {CONF_TYPE: "AC300"}
CHARGER2 = {CONF_TYPE: "CHARGER2"}


class OptionsFlowTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared plumbing for the options flow tests."""

    def make_flow(self, data):
        """Create an options flow instance without initializing Home Assistant."""
        flow = object.__new__(OptionsFlowHandler)
        entry = SimpleNamespace(data=data, entry_id="entry-1")
        flow.handler = "entry-1"
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_get_known_entry=Mock(return_value=entry),
                async_update_entry=Mock(),
                async_schedule_reload=Mock(),
            )
        )
        flow.async_show_form = Mock(return_value="form")
        flow.async_abort = Mock(return_value="aborted")
        flow.async_create_entry = Mock(return_value="created")
        return flow

    async def form_fields(self, flow):
        """The fields the Configure dialog offers, by name."""
        return set(await self.form(flow))

    async def form(self, flow):
        """The fields the Configure dialog offers, with their defaults."""
        await flow.async_step_init()

        schema = flow.async_show_form.call_args.kwargs["data_schema"]

        return {
            str(key): key.default() if callable(key.default) else key.default
            for key in schema.schema
        }


class TestEncryptionOption(OptionsFlowTestCase):
    """Test the encryption toggle."""

    async def test_every_entry_is_offered_the_toggle(self):
        """The channel can be corrected wherever it was guessed wrong."""
        for data in (
            {**AC300, CONF_USE_ENCRYPTION: True, CONF_ENCRYPTION_ASSUMED: False},
            {**AC300, CONF_USE_ENCRYPTION: True},
            {**AC300, CONF_USE_ENCRYPTION: True, CONF_ENCRYPTION_ASSUMED: True},
        ):
            with self.subTest(data=data):
                self.assertIn(
                    CONF_USE_ENCRYPTION, await self.form_fields(self.make_flow(data))
                )

    async def test_toggle_shows_what_was_detected(self):
        """The checkbox starts where the device was found, not at encrypted."""
        for stored in (True, False):
            with self.subTest(stored=stored):
                flow = self.make_flow({**AC300, CONF_USE_ENCRYPTION: stored})

                self.assertEqual((await self.form(flow))[CONF_USE_ENCRYPTION], stored)

    async def test_toggling_restarts_the_entry(self):
        """The channel is read at setup, so the entry has to reload."""
        flow = self.make_flow({**AC300, CONF_USE_ENCRYPTION: True})

        await flow.async_step_init({**OPTIONS, CONF_USE_ENCRYPTION: False})

        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertFalse(data[CONF_USE_ENCRYPTION])
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
            "entry-1"
        )

    async def test_untouched_channel_does_not_restart_the_entry(self):
        """Saving the other options should not drop the connection."""
        flow = self.make_flow({**AC300, CONF_USE_ENCRYPTION: True})

        await flow.async_step_init({**OPTIONS, CONF_USE_ENCRYPTION: True})

        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertTrue(data[CONF_USE_ENCRYPTION])
        flow.hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_assumed_device_keeps_its_provenance(self):
        """The flag records how the entry was added, and survives an edit."""
        flow = self.make_flow(
            {**AC300, CONF_USE_ENCRYPTION: True, CONF_ENCRYPTION_ASSUMED: True}
        )

        await flow.async_step_init({**OPTIONS, CONF_USE_ENCRYPTION: False})

        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertFalse(data[CONF_USE_ENCRYPTION])
        self.assertTrue(data[CONF_ENCRYPTION_ASSUMED])


class TestPackPollingOption(OptionsFlowTestCase):
    """Test who is offered the pack polling interval."""

    async def test_tower_station_is_offered_the_interval(self):
        """The stations with packs behind a selector can be asked to walk them."""
        for dev_type in ("AC200M", "AC300", "AC500"):
            with self.subTest(dev_type=dev_type):
                flow = self.make_flow({CONF_TYPE: dev_type, **OPTIONS})
                fields = await self.form(flow)

                self.assertIn(CONF_PACK_POLL_INTERVAL, fields)
                self.assertEqual(fields[CONF_PACK_POLL_INTERVAL], 2)

    async def test_station_without_packs_is_not_offered_the_interval(self):
        """A device with no pack registers has nothing to walk."""
        for dev_type in ("CHARGER2", "EL100V2", "EP500", "Unknown"):
            with self.subTest(dev_type=dev_type):
                flow = self.make_flow({CONF_TYPE: dev_type, **OPTIONS})

                self.assertNotIn(CONF_PACK_POLL_INTERVAL, await self.form_fields(flow))

    async def test_legacy_entry_is_not_offered_the_interval(self):
        """An entry that does not say what it is cannot be checked either."""
        flow = self.make_flow({**OPTIONS})

        self.assertNotIn(CONF_PACK_POLL_INTERVAL, await self.form_fields(flow))

    async def test_saving_a_pack_less_device_clears_the_interval(self):
        """The option is gone, so the stored value goes with it."""
        flow = self.make_flow({**CHARGER2, **OPTIONS})

        await flow.async_step_init(
            {
                "polling_interval": 20,
                "polling_timeout": 45,
                "max_retries": 5,
                CONF_USE_ENCRYPTION: True,
            }
        )

        created = flow.async_create_entry.call_args.kwargs["data"]
        self.assertEqual(created[CONF_PACK_POLL_INTERVAL], 0)


class TestHasBatteryPacks(unittest.TestCase):
    """Test the model lookup behind the pack polling option."""

    def test_stations_with_packs(self):
        for dev_type in ("AC200M", "AC300", "AC500"):
            with self.subTest(dev_type=dev_type):
                self.assertTrue(has_battery_packs(dev_type))

    def test_stations_without_packs(self):
        for dev_type in (
            "CHARGER2",
            "EL10",
            "EL100V2",
            "EL200V2",
            "EL300",
            "AP300",
            "EP600",
            None,
            3,
            "NotAModel",
        ):
            with self.subTest(dev_type=dev_type):
                self.assertFalse(has_battery_packs(dev_type))
