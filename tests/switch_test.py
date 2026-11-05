
"""Tests for verified writes in the Bluetti switch entity."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import ServiceValidationError

from bluetti_bt_lib import FieldName, build_device
from bluetti_bt_lib.fields import PulseSwitchField

from custom_components.bluetti_bt.switch import BluettiSwitch


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


if __name__ == "__main__":
    unittest.main()
