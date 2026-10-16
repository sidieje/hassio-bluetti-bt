
"""Tests for verified writes in the Bluetti switch entity."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
        """A failed read-back should warn and must not retry the write."""
        result = SimpleNamespace(
            write_succeeded=True,
            readback_succeeded=False,
            verified=False,
            values=None,
        )
        switch = self.make_switch(result)

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
        """A mismatched device state should warn and must not retry."""
        result = SimpleNamespace(
            write_succeeded=True,
            readback_succeeded=True,
            verified=False,
            values={"ctrl_system_on_off": False},
        )
        switch = self.make_switch(result)

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
        switch._logger.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
