"""Tests for the writable select entities (verified writes and failures)."""

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bluetti_bt_lib import FieldName
from bluetti_bt_lib.enums import UpsMode
from bluetti_bt_lib.fields import SelectField
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bluetti_bt.const import DOMAIN
from custom_components.bluetti_bt.select import BluettiSelect


def ups_field() -> SelectField:
    """The UPS mode register as the AC300 defines it."""
    return SelectField(FieldName.CTRL_UPS_MODE, 3001, UpsMode)


def result(
    write_succeeded: bool = True,
    readback_succeeded: bool = True,
    values: dict | None = None,
    verified: bool = True,
) -> SimpleNamespace:
    """A WriteVerificationResult stand-in.

    ``verified`` is the pinned library's own verdict: it compares the read-back
    against the option the requested value names, so it is False exactly when
    the device reports something other than what was written.
    """
    return SimpleNamespace(
        write_succeeded=write_succeeded,
        readback_succeeded=readback_succeeded,
        values=values,
        verified=verified,
    )


def make_select(write_result=None, write_error=None) -> BluettiSelect:
    """Select entity built without initializing Home Assistant."""
    field = ups_field()
    select = object.__new__(BluettiSelect)
    select._logger = Mock(spec=logging.Logger)
    select._address = "AA:BB:CC:DD:EE:FF"
    select._field = field
    select._response_key = field.name
    select._unavailable_counter = 0
    select._attr_options = [e.name for e in field.e]
    select._attr_unique_id = "bluetti.ctrl_ups_mode"
    select._attr_available = True
    select.coordinator = Mock()
    select.coordinator.data = {"ctrl_ups_mode": UpsMode.STANDARD}
    select.coordinator.async_write_verified = AsyncMock(
        return_value=write_result, side_effect=write_error
    )
    select.async_write_ha_state = Mock()
    return select


class TestSelectWrite(unittest.IsolatedAsyncioTestCase):
    """A select change only counts as done when the device agrees."""

    async def test_option_name_is_written_and_read_back(self):
        """The library gets the option name; the read-back is the Enum member."""
        select = make_select(
            write_result=result(values={"ctrl_ups_mode": UpsMode.PV_PRIORITY})
        )

        await select.async_select_option("PV_PRIORITY")

        select.coordinator.async_write_verified.assert_awaited_once_with(
            "ctrl_ups_mode", "PV_PRIORITY"
        )

    async def test_read_back_of_a_different_option_is_a_mismatch(self):
        select = make_select(
            write_result=result(
                values={"ctrl_ups_mode": UpsMode.STANDARD}, verified=False
            )
        )

        with self.assertRaises(HomeAssistantError) as raised:
            await select.async_select_option("PV_PRIORITY")

        self.assertEqual("select_write_mismatch", raised.exception.translation_key)
        self.assertEqual(DOMAIN, raised.exception.translation_domain)
        self.assertEqual(
            {"field": "ctrl_ups_mode", "option": "PV_PRIORITY", "reported": "STANDARD"},
            raised.exception.translation_placeholders,
        )

    async def test_the_library_verdict_is_what_the_entity_trusts(self):
        """The pinned library normalises the option name before comparing, so
        its own verdict already says whether the device applied the change."""
        applied = make_select(
            write_result=result(
                values={"ctrl_ups_mode": UpsMode.PV_PRIORITY}, verified=True
            )
        )
        await applied.async_select_option("PV_PRIORITY")

        stale = make_select(
            write_result=result(
                values={"ctrl_ups_mode": UpsMode.STANDARD}, verified=False
            )
        )

        with self.assertRaises(HomeAssistantError) as raised:
            await stale.async_select_option("PV_PRIORITY")

        self.assertEqual("select_write_mismatch", raised.exception.translation_key)

    async def test_rejected_write_raises(self):
        select = make_select(write_result=result(write_succeeded=False))

        with self.assertRaises(HomeAssistantError) as raised:
            await select.async_select_option("STANDARD")

        self.assertEqual("select_write_rejected", raised.exception.translation_key)

    async def test_write_without_a_connection_raises(self):
        select = make_select(write_result=None)

        with self.assertRaises(HomeAssistantError) as raised:
            await select.async_select_option("STANDARD")

        self.assertEqual("select_write_rejected", raised.exception.translation_key)

    async def test_unconfirmed_write_raises(self):
        """The device took the write but would not read the register back."""
        select = make_select(write_result=result(readback_succeeded=False))

        with self.assertRaises(HomeAssistantError) as raised:
            await select.async_select_option("STANDARD")

        self.assertEqual("select_write_unconfirmed", raised.exception.translation_key)

    async def test_unreachable_device_raises(self):
        select = make_select(write_error=asyncio.TimeoutError())

        with self.assertRaises(HomeAssistantError) as raised:
            await select.async_select_option("STANDARD")

        self.assertEqual("select_write_unreachable", raised.exception.translation_key)

    async def test_connection_error_raises(self):
        select = make_select(write_error=ConnectionError("lost"))

        with self.assertRaises(HomeAssistantError) as raised:
            await select.async_select_option("STANDARD")

        self.assertEqual("select_write_unreachable", raised.exception.translation_key)

    async def test_unknown_option_is_rejected_before_the_write(self):
        select = make_select()

        with self.assertRaises(ServiceValidationError) as raised:
            await select.async_select_option("NOT_AN_OPTION")

        self.assertEqual("invalid_option", raised.exception.translation_key)
        select.coordinator.async_write_verified.assert_not_awaited()

    async def test_read_back_updates_the_entity(self):
        """The coordinator merged the read-back, so the entity shows it."""
        select = make_select(
            write_result=result(values={"ctrl_ups_mode": UpsMode.PV_PRIORITY})
        )
        select.coordinator.data = {"ctrl_ups_mode": UpsMode.PV_PRIORITY}

        await select.async_select_option("PV_PRIORITY")
        select._handle_coordinator_update()

        self.assertEqual("PV_PRIORITY", select.current_option)

    async def test_failed_write_leaves_the_entity_on_the_device_value(self):
        select = make_select(
            write_result=result(
                values={"ctrl_ups_mode": UpsMode.STANDARD}, verified=False
            )
        )
        select.current_option = "PV_PRIORITY"

        with self.assertRaises(HomeAssistantError):
            await select.async_select_option("PV_PRIORITY")

        select._handle_coordinator_update()
        self.assertEqual("STANDARD", select.current_option)


if __name__ == "__main__":
    unittest.main()
