
"""Tests for verified writes in the Bluetti switch entity."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import ServiceValidationError

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


if __name__ == "__main__":
    unittest.main()
