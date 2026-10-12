"""Tests for the Bluetti polling coordinator's offline retry behaviour."""

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.bluetti_bt.coordinator import (
    OFFLINE_RETRY_DELAYS,
    PollingCoordinator,
)


class TestOfflineRetry(unittest.IsolatedAsyncioTestCase):
    """Test offline retry scheduling and state transitions."""

    def make_coordinator(self):
        """Create a coordinator instance without initializing Home Assistant."""
        coordinator = object.__new__(PollingCoordinator)
        coordinator.logger = Mock(spec=logging.Logger)
        coordinator.hass = SimpleNamespace(async_create_task=asyncio.create_task)
        coordinator.config = SimpleNamespace(
            address="AA:BB:CC:DD:EE:FF",
            name="AC300",
            polling_interval=20,
            polling_timeout=45,
            max_retries=5,
            use_encryption=False,
        )
        coordinator.bluetti_device = object()
        coordinator.client = None
        coordinator.reader = None
        coordinator.writer = None
        coordinator.disconnected = False
        coordinator.connection_lock = asyncio.Lock()
        coordinator.lock = asyncio.Lock()
        coordinator._offline = False
        coordinator._retry_attempt = False
        coordinator._retry_task = None
        coordinator._unloading = False
        coordinator.async_request_refresh = AsyncMock()
        return coordinator

    async def test_retry_delays_are_20_30_60_and_repeat_60(self):
        """Retry sleeps should follow the configured backoff sequence."""
        coordinator = self.make_coordinator()
        coordinator._offline = True
        delays = []
        attempts = 0

        async def fake_sleep(delay):
            delays.append(delay)
            if len(delays) == 4:
                coordinator._unloading = True

        async def fake_get_client():
            nonlocal attempts
            attempts += 1
            return None

        with (
            patch(
                "custom_components.bluetti_bt.coordinator.asyncio.sleep",
                new=fake_sleep,
            ),
            patch.object(
                coordinator, "_async_get_client", new=fake_get_client
            ),
        ):
            await coordinator._async_retry_loop()

        self.assertEqual(delays, [20, 30, 60, 60])
        self.assertEqual(attempts, 3)
        self.assertFalse(coordinator._retry_attempt)
        self.assertIsNone(coordinator._retry_task)

    async def test_offline_warning_is_logged_only_once(self):
        """Repeated failures should not produce repeated warnings."""
        coordinator = self.make_coordinator()

        with patch.object(coordinator, "_async_retry_loop", new=AsyncMock()):
            coordinator._mark_offline("Device not found")
            coordinator._mark_offline("Device not found")
            coordinator._mark_offline("Unable to connect")

        coordinator.logger.warning.assert_called_once_with("Device not found")
        self.assertEqual(coordinator.logger.debug.call_count, 2)
        self.assertTrue(coordinator._offline)

    async def test_polling_does_not_attempt_connection_while_offline(self):
        """Normal polling should respect the offline retry gate."""
        coordinator = self.make_coordinator()
        coordinator._offline = True
        coordinator._retry_attempt = False

        with patch(
            "custom_components.bluetti_bt.coordinator.BleakScanner.find_device_by_address",
            new=AsyncMock(),
        ) as find_device:
            client = await coordinator._async_get_client()

        self.assertIsNone(client)
        find_device.assert_not_awaited()

    async def test_unload_cancels_sleeping_retry_task(self):
        """Unloading should cancel the retry loop before dropping the connection."""
        coordinator = self.make_coordinator()
        coordinator._offline = True
        started = asyncio.Event()

        async def fake_sleep(_delay):
            started.set()
            await asyncio.Future()

        with patch(
            "custom_components.bluetti_bt.coordinator.asyncio.sleep",
            new=fake_sleep,
        ):
            coordinator._retry_task = asyncio.create_task(
                coordinator._async_retry_loop()
            )
            await started.wait()
            await coordinator.async_unload()

        self.assertTrue(coordinator._unloading)
        self.assertIsNone(coordinator._retry_task)

    async def test_successful_connection_logs_recovery(self):
        """Successful reader initialization clears the offline state."""
        coordinator = self.make_coordinator()
        coordinator._offline = True
        coordinator._retry_attempt = True
        coordinator.hass.loop = asyncio.get_running_loop()
        client = Mock()
        client.is_connected = True
        device = SimpleNamespace(name="AC300")
        reader = Mock()

        with (
            patch(
                "custom_components.bluetti_bt.coordinator.BleakScanner.find_device_by_address",
                new=AsyncMock(return_value=device),
            ),
            patch(
                "custom_components.bluetti_bt.coordinator.establish_connection",
                new=AsyncMock(return_value=client),
            ) as establish,
            patch(
                "custom_components.bluetti_bt.coordinator.DeviceReader",
                return_value=reader,
            ),
        ):
            result = await coordinator._async_get_client()

        self.assertIs(result, client)
        self.assertIs(coordinator.reader, reader)
        self.assertFalse(coordinator._offline)
        coordinator.logger.info.assert_any_call(
            "Bluetooth device recovered: %s",
            "XX:XX:XX:XX:XX:FF",
        )
        establish.assert_awaited_once()
        self.assertEqual(
            establish.await_args.kwargs["max_attempts"],
            coordinator.config.max_retries,
        )

    async def test_reader_initialization_failure_returns_to_offline(self):
        """A connection without a usable reader must remain offline."""
        coordinator = self.make_coordinator()
        coordinator._offline = True
        coordinator._retry_attempt = True
        coordinator.hass.loop = asyncio.get_running_loop()
        client = Mock()
        client.is_connected = True
        device = SimpleNamespace(name="AC300")

        with (
            patch(
                "custom_components.bluetti_bt.coordinator.BleakScanner.find_device_by_address",
                new=AsyncMock(return_value=device),
            ),
            patch(
                "custom_components.bluetti_bt.coordinator.establish_connection",
                new=AsyncMock(return_value=client),
            ),
            patch(
                "custom_components.bluetti_bt.coordinator.DeviceReader",
                side_effect=RuntimeError("reader initialization failed"),
            ),
        ):
            with patch.object(
                coordinator, "_async_drop_connection", new=AsyncMock()
            ):
                with patch.object(
                    coordinator, "_mark_offline"
                ) as mark_offline:
                    result = await coordinator._async_get_client()

        self.assertIsNone(result)
        self.assertTrue(coordinator._offline)
        mark_offline.assert_called_once()
        self.assertIn(
            "Unable to initialize reader",
            mark_offline.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
