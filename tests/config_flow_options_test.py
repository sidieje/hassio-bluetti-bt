"""Tests for the encryption option on the Configure dialog."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.bluetti_bt.config_flow import OptionsFlowHandler
from custom_components.bluetti_bt.types import (
    CONF_ENCRYPTION_ASSUMED,
    CONF_USE_ENCRYPTION,
)

OPTIONS = {
    "polling_interval": 20,
    "polling_timeout": 45,
    "max_retries": 5,
    "pack_poll_interval": 2,
}


class TestEncryptionOption(unittest.IsolatedAsyncioTestCase):
    """Test who is offered the encryption toggle."""

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

    async def schema_fields(self, flow):
        """The fields the Configure dialog offers."""
        await flow.async_step_init()

        schema = flow.async_show_form.call_args.kwargs["data_schema"]

        return {str(key) for key in schema.schema}

    async def test_probed_device_is_not_offered_the_toggle(self):
        """A device that answered a probe was asked which channel it uses."""
        flow = self.make_flow(
            {
                CONF_USE_ENCRYPTION: True,
                CONF_ENCRYPTION_ASSUMED: False,
            }
        )

        self.assertNotIn(CONF_USE_ENCRYPTION, await self.schema_fields(flow))

    async def test_legacy_device_is_not_offered_the_toggle(self):
        """Entries from before the shortcut keep behaving as probed."""
        flow = self.make_flow({CONF_USE_ENCRYPTION: True})

        self.assertNotIn(CONF_USE_ENCRYPTION, await self.schema_fields(flow))

    async def test_assumed_device_is_offered_the_toggle(self):
        """A device added from its advertised name can be corrected."""
        flow = self.make_flow(
            {
                CONF_USE_ENCRYPTION: True,
                CONF_ENCRYPTION_ASSUMED: True,
            }
        )

        self.assertIn(CONF_USE_ENCRYPTION, await self.schema_fields(flow))

    async def test_toggling_restarts_the_entry(self):
        """The channel is read at setup, so the entry has to reload."""
        flow = self.make_flow(
            {
                CONF_USE_ENCRYPTION: True,
                CONF_ENCRYPTION_ASSUMED: True,
            }
        )

        await flow.async_step_init({**OPTIONS, CONF_USE_ENCRYPTION: False})

        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertFalse(data[CONF_USE_ENCRYPTION])
        self.assertTrue(data[CONF_ENCRYPTION_ASSUMED])
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
            "entry-1"
        )

    async def test_probed_device_keeps_its_channel(self):
        """Nothing else can change the channel of a probed entry."""
        flow = self.make_flow(
            {
                CONF_USE_ENCRYPTION: True,
                CONF_ENCRYPTION_ASSUMED: False,
            }
        )

        await flow.async_step_init({**OPTIONS, CONF_USE_ENCRYPTION: False})

        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertTrue(data[CONF_USE_ENCRYPTION])
        flow.hass.config_entries.async_schedule_reload.assert_not_called()
