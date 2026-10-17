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

    async def test_verified_write_updates_coordinator_data(self):
        """Verified read-back values are published to coordinator data."""
        coordinator = self.make_coordinator()
        coordinator.data = {"existing_field": 10}
        coordinator.async_set_updated_data = Mock()

        result = SimpleNamespace(
            write_succeeded=True,
            readback_succeeded=True,
            verified=True,
            values={
                "ctrl_system_on_off": True,
                "ctrl_silent_charging": False,
            },
        )
        writer = SimpleNamespace(
            write_verified=AsyncMock(return_value=result)
        )

        with patch.object(
            coordinator,
            "_async_get_writer",
            new=AsyncMock(return_value=writer),
        ):
            returned = await coordinator.async_write_verified(
                "ctrl_system_on_off", True
            )

        self.assertIs(returned, result)
        writer.write_verified.assert_awaited_once_with(
            "ctrl_system_on_off", True
        )
        coordinator.async_set_updated_data.assert_called_once_with(
            {
                "existing_field": 10,
                "ctrl_system_on_off": True,
                "ctrl_silent_charging": False,
            }
        )

    async def test_verified_write_without_writer_returns_none(self):
        """A verified write without a connection returns None."""
        coordinator = self.make_coordinator()

        with patch.object(
            coordinator,
            "_async_get_writer",
            new=AsyncMock(return_value=None),
        ):
            result = await coordinator.async_write_verified(
                "ctrl_system_on_off", True
            )

        self.assertIsNone(result)
        coordinator.logger.warning.assert_called_once_with(
            "Unable to write %s: no connection",
            "ctrl_system_on_off",
        )

    async def test_verified_write_stops_if_encryption_handshake_fails(self):
        """A verified write must not proceed without ready encryption."""
        coordinator = self.make_coordinator()
        coordinator.config.use_encryption = True

        reader = SimpleNamespace(
            encryption=SimpleNamespace(is_ready_for_commands=False),
            read=AsyncMock(),
        )
        coordinator.reader = reader

        writer = SimpleNamespace(
            write_verified=AsyncMock(),
        )

        with patch.object(
            coordinator,
            "_async_get_writer",
            new=AsyncMock(return_value=writer),
        ):
            result = await coordinator.async_write_verified(
                "ctrl_system_on_off", True
            )

        self.assertIsNone(result)
        reader.read.assert_awaited_once_with()
        writer.write_verified.assert_not_awaited()
        coordinator.logger.warning.assert_called_once_with(
            "Encryption handshake not finished, not writing %s",
            "ctrl_system_on_off",
        )

    async def run_write_blocks_reconnect(self, write_method_name):
        """A reconnect must wait until an in-flight write has finished."""
        coordinator = self.make_coordinator()

        write_started = asyncio.Event()
        release_write = asyncio.Event()

        async def blocking_write(_field, _value):
            write_started.set()
            await release_write.wait()
            return True

        async def blocking_write_verified(_field, _value):
            write_started.set()
            await release_write.wait()
            return SimpleNamespace(
                write_succeeded=True,
                readback_succeeded=True,
                verified=True,
                values=None,
            )

        writer = SimpleNamespace(
            write=blocking_write,
            write_verified=blocking_write_verified,
        )

        entered_critical = asyncio.Event()

        async def fake_connect():
            entered_critical.set()
            return None

        with patch.object(
            coordinator, "_async_get_writer", new=AsyncMock(return_value=writer)
        ):
            write_task = asyncio.create_task(
                getattr(coordinator, write_method_name)(
                    "ctrl_system_on_off", True
                )
            )
            await write_started.wait()

            with patch.object(
                coordinator, "_async_get_client_unlocked", new=fake_connect
            ):
                reconnect_task = asyncio.create_task(
                    coordinator._async_get_client()
                )

                for _ in range(10):
                    await asyncio.sleep(0)

                # The reconnect needs connection_lock, which the write
                # still holds, so it must not have entered setup yet.
                self.assertFalse(entered_critical.is_set())
                self.assertFalse(reconnect_task.done())

                release_write.set()
                write_result = await write_task
                reconnect_result = await reconnect_task

        self.assertIsNotNone(write_result)
        self.assertTrue(entered_critical.is_set())
        self.assertIsNone(reconnect_result)
        self.assertFalse(coordinator.connection_lock.locked())

    async def test_plain_write_blocks_concurrent_reconnect(self):
        """async_write must hold connection_lock across the whole write."""
        await self.run_write_blocks_reconnect("async_write")

    async def test_verified_write_blocks_concurrent_reconnect(self):
        """async_write_verified must hold the lock across write+read-back."""
        await self.run_write_blocks_reconnect("async_write_verified")


if __name__ == "__main__":
    unittest.main()
