"""Tests for the Bluetti switch entity: verified writes and state updates.

availability_test.py drives the shared availability debounce through unbound
methods, but nothing exercised _handle_coordinator_update on this platform: the
six guards that decide whether a reading is published at all ran untested.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import ServiceValidationError

from bluetti_bt_lib import FieldName, build_device
from bluetti_bt_lib.fields import PulseSwitchField

from custom_components.bluetti_bt.switch import BluettiSwitch

RESPONSE_KEY = "ctrl_system_on_off"
UNIQUE_ID = "AABBCCDDEEFF_ctrl_system_on_off"
LOGABLE_ID = "AABBCCXXXXXXXXXXctrl_system_on_off"


class TestBluettiSwitchWrite(unittest.IsolatedAsyncioTestCase):
    """Test switch write verification and failure handling."""

    def make_switch(self, result):
        """Create a switch without initializing Home Assistant."""
        switch = object.__new__(BluettiSwitch)
        switch._logger = Mock()
        switch._address = "AA:BB:CC:DD:EE:FF"
        switch._response_key = "ctrl_system_on_off"
        switch._field = SimpleNamespace(name="ctrl_system_on_off")
        switch.coordinator = SimpleNamespace(
            async_write_verified=AsyncMock(return_value=result)
        )
        return switch

    async def test_readback_failure_does_not_retry(self):
        """A failed read-back should warn, report the failure, not retry."""
        result = SimpleNamespace(
            write_succeeded=True,
            readback_succeeded=False,
            verified=False,
            values=None,
        )
        switch = self.make_switch(result)

        with self.assertRaises(ServiceValidationError):
            await switch.write_to_device(True)

        switch.coordinator.async_write_verified.assert_awaited_once_with(
            "ctrl_system_on_off", True
        )
        switch._logger.warning.assert_called_once_with(
            "Write sent for %s, but read-back failed; not retrying",
            "ctrl_system_on_off",
        )
        switch._logger.error.assert_not_called()

    async def test_state_mismatch_does_not_retry(self):
        """A mismatched device state should warn and report the device value."""
        result = SimpleNamespace(
            write_succeeded=True,
            readback_succeeded=True,
            verified=False,
            values={"ctrl_system_on_off": False},
        )
        switch = self.make_switch(result)

        with self.assertRaises(ServiceValidationError) as raised:
            await switch.write_to_device(True)

        switch.coordinator.async_write_verified.assert_awaited_once_with(
            "ctrl_system_on_off", True
        )
        switch._logger.warning.assert_called_once_with(
            "Write state mismatch for %s: requested %s; device reports %s",
            "ctrl_system_on_off",
            True,
            False,
        )
        self.assertIn("ctrl_system_on_off=False, not True", str(raised.exception))

    async def test_rejected_write_is_reported(self):
        """A write the device refused should be visible in the UI."""
        result = SimpleNamespace(
            write_succeeded=False,
            readback_succeeded=False,
            verified=False,
            values=None,
        )
        switch = self.make_switch(result)

        with self.assertRaises(ServiceValidationError):
            await switch.write_to_device(False)

        switch._logger.error.assert_called_once_with(
            "Write failed for %s; device state was not confirmed",
            "ctrl_system_on_off",
        )

    async def test_lost_connection_is_reported(self):
        """A write that never reached the device should be visible in the UI."""
        switch = self.make_switch(None)
        switch.coordinator.async_write_verified = AsyncMock(
            side_effect=ConnectionError("no response")
        )

        with self.assertRaises(ServiceValidationError) as raised:
            await switch.write_to_device(True)

        self.assertIsInstance(raised.exception.__cause__, ConnectionError)
        switch._logger.error.assert_called_once_with(
            "Write operation failed for %s: %s",
            "ctrl_system_on_off",
            raised.exception.__cause__,
        )


class TestStandbyPulseSwitch(unittest.IsolatedAsyncioTestCase):
    """The standby switch is a pulse, so a redundant request must not be sent.

    Register 174 says whether the station is in standby and 2013 is the pulse
    that moves it: the same value wakes and sleeps the station, so asking for
    the state it already reports would flip it the other way and the read-back
    would then disagree with the request.
    """

    def make_standby_switch(self, reported, result=None):
        """A standby switch whose station last reported ``reported``."""
        field = PulseSwitchField(
            FieldName.CTRL_SLEEP_MODE,
            174,
            pulse_address=2013,
            pulse_value=2,
            on_bit=8,
            verification_delay=10.0,
        )

        switch = object.__new__(BluettiSwitch)
        switch._logger = Mock()
        switch._address = "AA:BB:CC:DD:EE:FF"
        switch._response_key = field.name
        switch._field = field
        switch._attr_is_on = reported
        switch.coordinator = SimpleNamespace(
            data={} if reported is None else {field.name: reported},
            async_write_verified=AsyncMock(return_value=result),
        )
        return switch

    def verified(self, state: bool):
        return SimpleNamespace(
            write_succeeded=True,
            readback_succeeded=True,
            verified=True,
            values={"ctrl_sleep_mode": state},
        )

    async def test_state_the_station_already_reports_is_not_written(self):
        """A standby station asked to stay in standby is not pulsed again."""
        switch = self.make_standby_switch(True)

        await switch.write_to_device(True)

        switch.coordinator.async_write_verified.assert_not_awaited()
        switch._logger.warning.assert_not_called()
        switch._logger.error.assert_not_called()

    async def test_waking_a_standby_station_sends_the_pulse(self):
        """Asking for the other state is the only thing that moves it."""
        switch = self.make_standby_switch(True, result=self.verified(False))

        await switch.write_to_device(False)

        switch.coordinator.async_write_verified.assert_awaited_once_with(
            "ctrl_sleep_mode", False
        )

    async def test_state_is_taken_from_the_coordinator(self):
        """The polled state decides, even before the entity has a state."""
        switch = self.make_standby_switch(True)
        switch._attr_is_on = None

        await switch.write_to_device(True)

        switch.coordinator.async_write_verified.assert_not_awaited()

    async def test_unknown_state_still_sends_the_pulse(self):
        """A station that has not reported standby is asked to change."""
        switch = self.make_standby_switch(None)

        switch.coordinator.async_write_verified.return_value = self.verified(True)

        await switch.write_to_device(True)

        switch.coordinator.async_write_verified.assert_awaited_once_with(
            "ctrl_sleep_mode", True
        )

    async def test_pulse_that_did_not_move_the_station_is_reported(self):
        """An unconfirmed pulse means the station did not move, not that it
        moved the wrong way, because standby is the only other state."""
        result = SimpleNamespace(
            write_succeeded=True,
            readback_succeeded=True,
            verified=False,
            values={"ctrl_sleep_mode": False},
        )
        switch = self.make_standby_switch(False, result=result)

        with self.assertRaises(ServiceValidationError) as raised:
            await switch.write_to_device(True)

        switch.coordinator.async_write_verified.assert_awaited_once_with(
            "ctrl_sleep_mode", True
        )
        switch._logger.warning.assert_called_once_with(
            "Pulse sent for %s, but the station still reports %s",
            "ctrl_sleep_mode",
            False,
        )
        self.assertIn("did not change ctrl_sleep_mode", str(raised.exception))
        self.assertIn("still reports ctrl_sleep_mode=False", str(raised.exception))

    def test_standby_field_is_a_pulse_switch_on_both_models(self):
        """The switch platform sees a pulse switch: state at 174, pulse at 2013,
        and no separate sensor for the same register."""
        for model in ("AP300", "Elite 200 V2"):
            device = build_device(f"{model}12345678")
            with self.subTest(model=model):
                standby = [
                    field
                    for field in device.get_switch_fields()
                    if field.name == FieldName.CTRL_SLEEP_MODE.value
                ]

                self.assertEqual(len(standby), 1)
                self.assertIsInstance(standby[0], PulseSwitchField)
                self.assertEqual(standby[0].address, 174)
                self.assertEqual(standby[0].write_address, 2013)
                self.assertNotIn(
                    FieldName.CTRL_SLEEP_MODE.value,
                    [field.name for field in device.get_sensor_fields()],
                )


class TestBluettiSwitchUpdate(unittest.TestCase):
    """Test how a switch turns a coordinator cycle into a state."""

    def make_switch(self, data, last_update_success: bool = True, field=None):
        """Create a switch without initializing Home Assistant."""
        switch = object.__new__(BluettiSwitch)
        switch._logger = Mock()
        switch._response_key = RESPONSE_KEY
        switch._field = field or SimpleNamespace(name=RESPONSE_KEY)
        switch._attr_unique_id = UNIQUE_ID
        switch._attr_is_on = None
        switch._attr_available = True
        switch._attr_extra_state_attributes = {}
        switch._unavailable_counter = 0
        switch.coordinator = SimpleNamespace(
            data=data,
            last_update_success=last_update_success,
            consecutive_failures=0,
        )
        # Record what each write would have published, so a state that is only
        # set after the last write is caught rather than trusted.
        switch.writes: list[bool | None] = []
        switch.async_write_ha_state = Mock(
            side_effect=lambda: switch.writes.append(switch._attr_is_on)
        )
        return switch

    def assert_unavailable_reported(self, switch, cause: str) -> None:
        """A rejected cycle should report the cause and never publish a state."""
        self.assertEqual(
            switch._attr_extra_state_attributes.get("unavailable_cause"), cause
        )
        switch.async_write_ha_state.assert_called_once_with()
        self.assertEqual(switch.writes, [None])

    def test_failed_poll_is_not_read_as_fresh_data(self):
        """A failed cycle must not publish, even though data still holds the last good dict."""
        switch = self.make_switch({RESPONSE_KEY: True}, last_update_success=False)

        switch._handle_coordinator_update()

        self.assert_unavailable_reported(switch, "Polling failed")

    def test_none_data_reports_data_is_none(self):
        """No dict at all is a distinct cause from an empty one."""
        switch = self.make_switch(None)

        switch._handle_coordinator_update()

        self.assert_unavailable_reported(switch, "Data is None")
        switch._logger.debug.assert_called_with("Data from coordinator is None")

    def test_non_dict_data_reports_invalid_data(self):
        """Coordinator data that is not a mapping cannot be indexed by key."""
        switch = self.make_switch("not a dict")

        switch._handle_coordinator_update()

        self.assert_unavailable_reported(switch, "Invalid data")

    def test_missing_key_reports_no_data(self):
        """A station that answered without this control has no state to publish."""
        switch = self.make_switch({"other_field": True})

        switch._handle_coordinator_update()

        self.assert_unavailable_reported(switch, "No data")

    def test_non_bool_value_is_rejected_not_coerced(self):
        """A register that answers 1 is not a bool, so it must not switch on."""
        switch = self.make_switch({RESPONSE_KEY: 1})

        switch._handle_coordinator_update()

        self.assert_unavailable_reported(switch, "Invalid data type")
        switch._logger.warning.assert_called_once_with(
            "Invalid response data type from coordinator (switch.%s): %s",
            LOGABLE_ID,
            1,
        )

    def test_true_reading_turns_the_switch_on(self):
        switch = self.make_switch({RESPONSE_KEY: True})

        switch._handle_coordinator_update()

        self.assertIs(switch._attr_is_on, True)
        self.assertTrue(switch._attr_available)
        self.assertEqual(switch._unavailable_counter, 0)
        self.assertEqual(switch._attr_extra_state_attributes, {})
        # _set_available writes once before the state is known and the update
        # writes again after it, so the last write is what Home Assistant keeps.
        self.assertEqual(switch.writes, [None, True])

    def test_false_reading_turns_the_switch_off(self):
        """A reading of False is a real state, not a missing reading."""
        switch = self.make_switch({RESPONSE_KEY: False})

        switch._handle_coordinator_update()

        self.assertIs(switch._attr_is_on, False)
        self.assertTrue(switch._attr_available)
        self.assertNotIn("unavailable_cause", switch._attr_extra_state_attributes)
        self.assertEqual(switch.writes, [None, False])

    def test_pulse_switch_state_comes_from_the_polled_register(self):
        """A pulse switch reports its state in another register, and that is
        what the poll carries, so the update path must not special-case it."""
        field = PulseSwitchField(
            FieldName.CTRL_SLEEP_MODE,
            174,
            pulse_address=2013,
            pulse_value=2,
            on_bit=8,
            verification_delay=10.0,
        )
        switch = self.make_switch({field.name: True}, field=field)
        switch._response_key = field.name

        switch._handle_coordinator_update()

        self.assertIs(switch._attr_is_on, True)

    def test_a_good_cycle_clears_a_previous_outage(self):
        """The counter from an outage must not keep the entity unavailable."""
        switch = self.make_switch({RESPONSE_KEY: True})
        switch._unavailable_counter = 3
        switch._attr_extra_state_attributes = {"unavailable_cause": "No data"}

        switch._handle_coordinator_update()

        self.assertEqual(switch._unavailable_counter, 0)
        self.assertEqual(switch._attr_extra_state_attributes, {})
        self.assertTrue(switch.available)


if __name__ == "__main__":
    unittest.main()
