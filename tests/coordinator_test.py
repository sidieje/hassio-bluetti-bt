"""Tests for the Bluetti polling coordinator's offline retry behaviour."""

import asyncio
import logging
import unittest
from datetime import UTC, datetime
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bleak.exc import (
    BleakBluetoothNotAvailableError,
    BleakBluetoothNotAvailableReason,
    BleakError,
)
from bleak_retry_connector import (
    BleakConnectionError,
    BleakOutOfConnectionSlotsError,
)

from custom_components.bluetti_bt.coordinator import (
    FIRST_REFRESH_MAX_ATTEMPTS,
    OFFLINE_RETRY_DELAYS,
    STARTUP_GRACE_SECONDS,
    PollingCoordinator,
)


def make_coordinator(pack_poll_interval: int = 2) -> PollingCoordinator:
    """Create a coordinator instance without initializing Home Assistant."""
    coordinator = object.__new__(PollingCoordinator)
    coordinator.logger = Mock(spec=logging.Logger)
    coordinator.hass = SimpleNamespace(
        async_create_task=asyncio.create_task,
        loop=asyncio.get_running_loop(),
    )
    coordinator.config = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="AC300",
        polling_interval=20,
        polling_timeout=45,
        max_retries=5,
        use_encryption=False,
        pack_poll_interval=pack_poll_interval,
    )
    coordinator.bluetti_device = SimpleNamespace(max_packs=4)
    coordinator.client = None
    coordinator.reader = None
    coordinator.writer = None
    coordinator.disconnected = False
    coordinator.connection_lock = asyncio.Lock()
    coordinator.lock = asyncio.Lock()
    coordinator._offline = False
    coordinator._offline_reported = False
    coordinator._ever_connected = False
    # Past by default, so the startup grace is off unless a test asks for it.
    coordinator._startup_grace_until = 0.0
    # Off by default: only the refresh Home Assistant waits for during entry
    # setup connects with the reduced attempt count.
    coordinator._setup_refresh = False
    coordinator._retry_attempt = False
    coordinator._retry_delay_index = 0
    coordinator._retry_unsub = None
    coordinator._retry_attempt_task = None
    coordinator._unloading = False
    coordinator._pack_interval = pack_poll_interval * 60
    coordinator._pack_due_at = 0.0
    coordinator._pack_values = {}
    coordinator._pack_num = None
    coordinator._pack_retries = 0
    coordinator._failed_reads = 0
    coordinator._pack_prefixes = tuple(
        f"pack_{slot}_" for slot in range(1, coordinator.bluetti_device.max_packs + 1)
    )
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


class RetryTimerRecorder:
    """Stand-in for async_call_later that records the timers it is given."""

    def __init__(self) -> None:
        self.timers: list[dict] = []

    def __call__(self, _hass, delay, action):
        """Arm a timer and return the unsub that cancels it."""
        timer = {"delay": delay, "action": action, "cancelled": False}
        self.timers.append(timer)

        def cancel() -> None:
            timer["cancelled"] = True

        return cancel

    @property
    def pending(self) -> list[dict]:
        """The timers still armed."""
        return [timer for timer in self.timers if not timer["cancelled"]]

    def delays(self) -> list[float]:
        """Every delay that has been armed, in order."""
        return [timer["delay"] for timer in self.timers]

    def fire(self) -> asyncio.Task:
        """Arm the pending timer's callback as a task, as Home Assistant does.

        The caller awaits the returned task, or leaves it running when it
        wants an attempt that is still in flight.
        """
        timer = self.pending[-1]
        timer["cancelled"] = True
        return asyncio.create_task(timer["action"](datetime.now(UTC)))


class TestOfflineRetry(unittest.IsolatedAsyncioTestCase):
    """Test offline retry scheduling and state transitions."""

    def make_coordinator(self):
        return make_coordinator()

    def patch_call_later(self, recorder: RetryTimerRecorder):
        """Swap the timer helper for the recorder."""
        return patch(
            "custom_components.bluetti_bt.coordinator.async_call_later",
            new=recorder,
        )

    async def test_retry_delays_are_20_30_60_and_repeat_60(self):
        """Failed attempts should follow the backoff and clamp at the last."""
        coordinator = self.make_coordinator()
        timers = RetryTimerRecorder()
        coordinator._offline = True

        async def fail_attempt() -> None:
            # The real connection attempt reports its own failure.
            coordinator._mark_offline("Unable to connect")

        coordinator._async_get_client = fail_attempt

        with self.patch_call_later(timers):
            coordinator._mark_offline("Device not found")

            for _ in range(3):
                await timers.fire()

        self.assertEqual([20, 30, 60, 60], timers.delays())
        self.assertEqual(list(OFFLINE_RETRY_DELAYS) + [60], timers.delays())
        self.assertEqual(2, coordinator._retry_delay_index)
        self.assertFalse(coordinator._retry_attempt)
        self.assertIsNone(coordinator._retry_attempt_task)

    async def test_timer_fires_exactly_one_connection_attempt(self):
        """One armed timer means one attempt, and a failure re-arms later."""
        coordinator = self.make_coordinator()
        timers = RetryTimerRecorder()
        coordinator._offline = True
        coordinator._async_get_client = AsyncMock(return_value=None)

        with self.patch_call_later(timers):
            coordinator._mark_offline("Device not found")
            self.assertEqual([20], timers.delays())

            await timers.fire()

        coordinator._async_get_client.assert_awaited_once()
        self.assertEqual([30], timers.delays()[1:])
        self.assertEqual(1, len(timers.pending))
        self.assertFalse(coordinator._retry_attempt)
        self.assertIsNone(coordinator._retry_attempt_task)

    async def test_mark_offline_does_not_stack_retries(self):
        """Repeated failures while a timer is pending must not add more."""
        coordinator = self.make_coordinator()
        timers = RetryTimerRecorder()

        with self.patch_call_later(timers):
            coordinator._mark_offline("Device not found")
            coordinator._mark_offline("Device not found")
            coordinator._mark_offline("Unable to connect")

        self.assertEqual([20], timers.delays())
        self.assertEqual(1, len(timers.pending))

    async def test_offline_warning_is_logged_only_once(self):
        """Repeated failures should not produce repeated warnings."""
        coordinator = self.make_coordinator()

        with self.patch_call_later(RetryTimerRecorder()):
            coordinator._mark_offline("Device not found")
            coordinator._mark_offline("Device not found")
            coordinator._mark_offline("Unable to connect")

        coordinator.logger.warning.assert_called_once_with("Device not found")
        self.assertEqual(coordinator.logger.debug.call_count, 2)
        self.assertTrue(coordinator._offline)

    async def test_mark_offline_does_not_arm_a_retry_while_unloading(self):
        """Nothing may be scheduled once the entry is on its way out."""
        coordinator = self.make_coordinator()
        timers = RetryTimerRecorder()
        coordinator._unloading = True

        with self.patch_call_later(timers):
            coordinator._mark_offline("Device not found")

        self.assertEqual([], timers.delays())
        self.assertIsNone(coordinator._retry_unsub)

    async def test_unload_cancels_the_pending_retry_timer(self):
        """Unloading should leave no timer armed and no attempt running."""
        coordinator = self.make_coordinator()
        timers = RetryTimerRecorder()
        coordinator._offline = True
        coordinator._async_get_client = AsyncMock()

        with self.patch_call_later(timers):
            coordinator._mark_offline("Device not found")
            await coordinator.async_unload()

            # A callback that still reaches the coordinator must be ignored.
            await timers.timers[0]["action"](datetime.now(UTC))

        self.assertTrue(timers.timers[0]["cancelled"])
        self.assertEqual([], timers.pending)
        self.assertIsNone(coordinator._retry_unsub)
        self.assertTrue(coordinator._unloading)
        coordinator._async_get_client.assert_not_awaited()

    async def test_unload_cancels_an_in_flight_attempt(self):
        """An attempt that is mid-connection is cancelled on unload."""
        coordinator = self.make_coordinator()
        timers = RetryTimerRecorder()
        coordinator._offline = True
        started = asyncio.Event()

        async def blocking_attempt() -> None:
            started.set()
            await asyncio.Future()

        coordinator._async_get_client = blocking_attempt

        with self.patch_call_later(timers):
            coordinator._mark_offline("Device not found")
            attempt = timers.fire()
            await started.wait()
            self.assertIs(attempt, coordinator._retry_attempt_task)

            await coordinator.async_unload()

        self.assertTrue(attempt.cancelled())
        self.assertIsNone(coordinator._retry_attempt_task)
        self.assertFalse(coordinator._retry_attempt)

    async def test_recovery_resets_the_backoff_and_cancels_the_timer(self):
        """Coming back online drops the pending retry and the delay index."""
        coordinator = self.make_coordinator()
        timers = RetryTimerRecorder()
        coordinator._offline = True
        coordinator._retry_attempt = True
        coordinator._retry_delay_index = 2
        client = Mock()
        client.is_connected = True
        device = SimpleNamespace(name="AC300")

        with (
            self.patch_call_later(timers),
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
                return_value=Mock(),
            ),
        ):
            coordinator._mark_offline("Device not found")
            self.assertEqual([60], timers.delays())

            self.assertIs(client, await coordinator._async_get_client())

            self.assertFalse(coordinator._offline)
            self.assertEqual(0, coordinator._retry_delay_index)
            self.assertTrue(timers.timers[0]["cancelled"])
            self.assertIsNone(coordinator._retry_unsub)

            # A later outage starts at the short end of the backoff again.
            coordinator._offline = True
            coordinator._mark_offline("Device not found")

        self.assertEqual([60, 20], timers.delays())

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

    async def test_successful_connection_logs_recovery(self):
        """Successful reader initialization clears the offline state."""
        coordinator = self.make_coordinator()
        coordinator._offline = True
        coordinator._retry_attempt = True
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

    async def test_unverified_write_readback_is_not_merged(self):
        """An unverified read-back must not overwrite the cached data."""
        coordinator = self.make_coordinator()
        coordinator.data = {"ctrl_system_on_off": False}
        coordinator.async_set_updated_data = Mock()

        result = SimpleNamespace(
            write_succeeded=True,
            readback_succeeded=True,
            verified=False,
            values={
                "ctrl_system_on_off": False,
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
        coordinator.async_set_updated_data.assert_not_called()
        self.assertEqual(
            {"ctrl_system_on_off": False},
            coordinator.data,
        )

    async def test_unverified_write_without_readback_is_not_merged(self):
        """A write whose read-back failed has no values to merge."""
        coordinator = self.make_coordinator()
        coordinator.data = {"ctrl_system_on_off": False}
        coordinator.async_set_updated_data = Mock()

        result = SimpleNamespace(
            write_succeeded=True,
            readback_succeeded=False,
            verified=False,
            values=None,
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
        coordinator.async_set_updated_data.assert_not_called()

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


class TestOfflineLogging(unittest.IsolatedAsyncioTestCase):
    """Test how an offline device reaches the log."""

    def patch_timer(self) -> RetryTimerRecorder:
        """Record the offline retry timers instead of arming real ones."""
        timers = RetryTimerRecorder()
        self._timer_patch = patch(
            "custom_components.bluetti_bt.coordinator.async_call_later", new=timers
        )
        self._timer_patch.start()
        self.addCleanup(self._timer_patch.stop)
        return timers

    async def test_startup_grace_holds_the_warning_back(self):
        """Failing before the adapters and proxies are up is expected."""
        coordinator = make_coordinator()
        coordinator._startup_grace_until = monotonic() + STARTUP_GRACE_SECONDS
        timers = self.patch_timer()

        coordinator._mark_offline("XX:XX:XX:XX:XX:FF is not advertising")

        coordinator.logger.warning.assert_not_called()
        self.assertEqual(1, coordinator.logger.debug.call_count)
        self.assertIn("startup grace", coordinator.logger.debug.call_args.args[0])
        # The backoff still runs during the grace, so the device is retried.
        self.assertEqual([20], timers.delays())
        self.assertTrue(coordinator._offline)

    async def test_a_device_still_absent_after_the_grace_warns(self):
        """The grace defers the warning, it does not drop it."""
        coordinator = make_coordinator()
        coordinator._startup_grace_until = monotonic() + STARTUP_GRACE_SECONDS
        timers = self.patch_timer()

        async def not_found() -> None:
            coordinator._mark_offline("XX:XX:XX:XX:XX:FF is not advertising")

        coordinator._async_get_client = not_found

        coordinator._mark_offline("XX:XX:XX:XX:XX:FF is not advertising")

        # The retry attempt lands once the grace has run out.
        coordinator._startup_grace_until = monotonic() - 1
        await timers.fire()

        coordinator.logger.warning.assert_called_once_with(
            "XX:XX:XX:XX:XX:FF is not advertising"
        )
        self.assertEqual([20, 30], timers.delays())

    async def test_the_grace_ends_once_the_device_has_connected(self):
        """A device that was up and then dropped is not a startup race."""
        coordinator = make_coordinator()
        coordinator._startup_grace_until = monotonic() + STARTUP_GRACE_SECONDS
        coordinator._ever_connected = True
        self.patch_timer()

        coordinator._mark_offline("XX:XX:XX:XX:XX:FF stopped responding")

        coordinator.logger.warning.assert_called_once_with(
            "XX:XX:XX:XX:XX:FF stopped responding"
        )

    async def connect_failure(self, coordinator: PollingCoordinator, err: Exception):
        """Run one connection attempt that fails with err."""
        device = SimpleNamespace(name="Bluetti")

        with (
            patch(
                "custom_components.bluetti_bt.coordinator.BleakScanner.find_device_by_address",
                new=AsyncMock(return_value=device),
            ),
            patch(
                "custom_components.bluetti_bt.coordinator.establish_connection",
                new=AsyncMock(side_effect=err),
            ),
        ):
            self.assertIsNone(await coordinator._async_get_client())

        return coordinator.logger.warning.call_args.args[0]

    async def test_connection_failure_warns_in_plain_words(self):
        """The warning says what an outage means, the bleak dump goes to debug."""
        coordinator = make_coordinator()
        self.patch_timer()

        warning = await self.connect_failure(
            coordinator,
            BleakError(
                "AC3002225000400310 - AA:BB:CC:DD:EE:FF: Failed to connect "
                "after 7 attempt(s): TimeoutError"
            ),
        )

        self.assertIn("XX:XX:XX:XX:XX:FF", warning)
        self.assertIn("powered off, asleep or out of range", warning)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", warning)
        self.assertNotIn("Failed to connect after", warning)

        details = [
            call.args[1]
            for call in coordinator.logger.debug.call_args_list
            if call.args[0] == "Connection failure detail: %s"
        ]
        self.assertEqual(1, len(details))
        self.assertIn("Failed to connect after 7 attempt(s)", details[0])
        self.assertNotIn("AA:BB:CC:DD:EE:FF", details[0])

    async def test_out_of_slots_advice_stays_on_the_warning(self):
        """The one failure the user can act on is not hidden behind debug."""
        coordinator = make_coordinator()
        self.patch_timer()

        warning = await self.connect_failure(
            coordinator,
            BleakError(
                "AP3002519000242488 - AA:BB:CC:DD:EE:FF: Failed to connect after "
                "10 attempt(s): No backend with an available connection slot was "
                "found: The proxy/adapter is out of connection slots or the "
                "device is no longer reachable; Add additional proxies "
                "(https://esphome.io/projects/?type=bluetooth) near this device"
            ),
        )

        self.assertIn("out of connection slots", warning)
        self.assertIn("https://esphome.io/projects/?type=bluetooth", warning)
        self.assertIn("XX:XX:XX:XX:XX:FF", warning)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", warning)

    async def test_a_device_no_backend_can_reach_stays_neutral(self):
        """A station switched off at the wall looks exactly like this."""
        coordinator = make_coordinator()
        self.patch_timer()

        warning = await self.connect_failure(
            coordinator,
            BleakOutOfConnectionSlotsError(
                "AC300 - AA:BB:CC:DD:EE:FF: Failed to connect after 10 attempt(s): "
                "AA:BB:CC:DD:EE:FF: No backend with an available connection slot "
                "that can reach address AA:BB:CC:DD:EE:FF was found: "
                "The proxy/adapter is out of connection slots or the device is no "
                "longer reachable; Add additional proxies "
                "(https://esphome.io/projects/?type=bluetooth) near this device"
            ),
        )

        self.assertIn("powered off, asleep or out of range", warning)
        self.assertNotIn("connection slots", warning)
        self.assertNotIn("https://esphome.io/projects/?type=bluetooth", warning)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", warning)

        # The bleak text still reaches debug, where the guess can be checked.
        details = [
            call.args[1]
            for call in coordinator.logger.debug.call_args_list
            if call.args[0] == "Connection failure detail: %s"
        ]
        self.assertEqual(1, len(details))
        self.assertIn("No backend with an available connection slot", details[0])

    async def test_a_proxy_out_of_slots_keeps_the_advice(self):
        """A proxy that has no free slot still names the fix on the warning."""
        coordinator = make_coordinator()
        self.patch_timer()

        warning = await self.connect_failure(
            coordinator,
            BleakOutOfConnectionSlotsError(
                "AC300 - AA:BB:CC:DD:EE:FF: Failed to connect after 10 attempt(s): "
                "Timeout connecting after 60.0s: No available connection slot: "
                "The proxy/adapter is out of connection slots or the device is no "
                "longer reachable; Add additional proxies "
                "(https://esphome.io/projects/?type=bluetooth) near this device"
            ),
        )

        self.assertIn("out of connection slots", warning)
        self.assertIn("https://esphome.io/projects/?type=bluetooth", warning)

    async def test_a_missing_adapter_keeps_the_advice(self):
        """Bluetooth being off or absent is the host's problem, not the device's."""
        coordinator = make_coordinator()
        self.patch_timer()

        warning = await self.connect_failure(
            coordinator,
            BleakBluetoothNotAvailableError(
                "Bluetooth adapter is powered off",
                BleakBluetoothNotAvailableReason.POWERED_OFF,
            ),
        )

        self.assertIn("https://esphome.io/projects/?type=bluetooth", warning)
        self.assertNotIn("powered off, asleep or out of range", warning)

    async def test_a_connection_that_never_happens_stays_neutral(self):
        """A plain failed connect is not blamed on the adapter or a proxy."""
        coordinator = make_coordinator()
        self.patch_timer()

        warning = await self.connect_failure(
            coordinator,
            BleakConnectionError(
                "AC300 - AA:BB:CC:DD:EE:FF: Failed to connect after 5 attempt(s): "
                "Cannot connect to AA:BB:CC:DD:EE:FF: Unreachable"
            ),
        )

        self.assertIn("powered off, asleep or out of range", warning)
        self.assertNotIn("https://esphome.io/projects/?type=bluetooth", warning)


class TestFirstRefreshConnection(unittest.IsolatedAsyncioTestCase):
    """Test how hard the coordinator tries while the entry is being set up."""

    async def refresh(self, coordinator: PollingCoordinator) -> AsyncMock:
        """Run one refresh that connects and reads, and return bleak's mock."""
        client = AsyncMock()
        client.is_connected = True
        reader = AsyncMock()
        reader.read.return_value = {"pack_num": 2, "pack_slots": [2]}

        with (
            patch(
                "custom_components.bluetti_bt.coordinator.BleakScanner.find_device_by_address",
                new=AsyncMock(return_value=SimpleNamespace(name="AC300")),
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
            await coordinator._async_update_data()

        return establish

    async def test_the_setup_refresh_connects_with_fewer_attempts(self):
        """A tower that is unreachable at startup must not stall bootstrap."""
        coordinator = make_coordinator()
        coordinator.config.max_retries = 10
        coordinator._setup_refresh = True

        establish = await self.refresh(coordinator)

        self.assertEqual(
            FIRST_REFRESH_MAX_ATTEMPTS,
            establish.await_args.kwargs["max_attempts"],
        )
        self.assertFalse(coordinator._setup_refresh)

    async def test_later_polls_use_the_attempts_the_user_configured(self):
        """Only the refresh Home Assistant waits behind is capped."""
        coordinator = make_coordinator()
        coordinator.config.max_retries = 10
        coordinator._setup_refresh = True

        await self.refresh(coordinator)

        # The connection dropped, so the next cycle reconnects.
        coordinator.disconnected = True
        establish = await self.refresh(coordinator)

        self.assertEqual(10, establish.await_args.kwargs["max_attempts"])

    async def test_the_cap_never_raises_a_smaller_retry_count(self):
        """A user who asked for fewer attempts still gets fewer."""
        coordinator = make_coordinator()
        coordinator.config.max_retries = 1
        coordinator._setup_refresh = True

        establish = await self.refresh(coordinator)

        self.assertEqual(1, establish.await_args.kwargs["max_attempts"])


class TestPackPolling(unittest.IsolatedAsyncioTestCase):
    """Test how often the battery pack slots are walked."""

    STATION = {"pack_num": 2, "battery_soc": 55}

    def full_walk(self) -> dict:
        """A walk that answered every pack the station reported."""
        return {
            **self.STATION,
            "pack_slots": [2, 3],
            "pack_2_voltage": 51.7,
            "pack_3_voltage": 51.6,
        }

    def partial_walk(self) -> dict:
        """A walk where one pack did not answer."""
        return {
            **self.STATION,
            "pack_slots": [2],
            "pack_2_voltage": 51.7,
        }

    def make_coordinator(self, pack_poll_interval: int, cycles: list[dict]):
        """A coordinator whose reader answers each cycle from the list."""
        coordinator = make_coordinator(pack_poll_interval)
        coordinator._async_get_client = AsyncMock(return_value=Mock())
        coordinator.reader = Mock(read=AsyncMock(side_effect=cycles))
        return coordinator

    async def walked(self, coordinator) -> bool:
        """Whether the last cycle asked the reader to walk the slots."""
        return coordinator.reader.read.await_args.kwargs["include_packs"]

    def walk_next_cycle(self, coordinator) -> None:
        """Open the next cycle for a walk without waiting out the interval."""
        coordinator._pack_due_at = 0.0

    async def test_first_cycle_walks_the_slots(self):
        """A fresh connection reports what the device has in it now."""
        coordinator = self.make_coordinator(2, [self.full_walk()])

        data = await coordinator._async_update_data()

        self.assertTrue(await self.walked(coordinator))
        self.assertEqual(data["pack_slots"], [2, 3])

    async def test_later_cycles_reuse_the_pack_values(self):
        """Skipping the walk still reports the packs from the last walk."""
        coordinator = self.make_coordinator(2, [self.full_walk(), dict(self.STATION)])

        await coordinator._async_update_data()
        data = await coordinator._async_update_data()

        self.assertFalse(await self.walked(coordinator))
        self.assertEqual(data["battery_soc"], 55)
        self.assertEqual(data["pack_slots"], [2, 3])
        self.assertEqual(data["pack_2_voltage"], 51.7)
        self.assertNotIn("pack_4_voltage", data)

    async def test_zero_interval_never_walks_the_slots(self):
        """Pack polling is off unless the user asks for it."""
        coordinator = self.make_coordinator(0, [dict(self.STATION) for _ in range(2)])

        await coordinator._async_update_data()
        data = await coordinator._async_update_data()

        self.assertFalse(await self.walked(coordinator))
        self.assertNotIn("pack_slots", data)

    async def test_pack_count_change_walks_the_next_cycle(self):
        """The pack count is a station register, so a swap is noticed early."""
        coordinator = self.make_coordinator(
            2,
            [self.full_walk(), {**self.STATION, "pack_num": 3}, self.full_walk()],
        )

        await coordinator._async_update_data()
        await coordinator._async_update_data()
        await coordinator._async_update_data()

        self.assertTrue(await self.walked(coordinator))

    async def test_a_walk_replaces_values_from_the_previous_walk(self):
        """A pack that was moved does not linger in the reused values."""
        moved = {
            **self.STATION,
            "pack_slots": [1, 4],
            "pack_1_voltage": 51.9,
            "pack_4_voltage": 51.8,
        }
        coordinator = self.make_coordinator(
            2,
            [self.full_walk(), dict(self.STATION), moved, dict(self.STATION)],
        )

        await coordinator._async_update_data()
        await coordinator._async_update_data()
        self.walk_next_cycle(coordinator)

        await coordinator._async_update_data()
        data = await coordinator._async_update_data()

        self.assertEqual(data["pack_slots"], [1, 4])
        self.assertEqual(data["pack_4_voltage"], 51.8)
        self.assertNotIn("pack_2_voltage", data)

    async def test_incomplete_walk_keeps_the_previous_pack_values(self):
        """A pack that failed to answer is not reported as missing."""
        coordinator = self.make_coordinator(
            2,
            [self.full_walk(), self.partial_walk(), dict(self.STATION)],
        )

        await coordinator._async_update_data()
        self.walk_next_cycle(coordinator)

        data = await coordinator._async_update_data()

        self.assertTrue(await self.walked(coordinator))
        self.assertEqual(data["pack_slots"], [2, 3])
        self.assertEqual(data["pack_3_voltage"], 51.6)

        # The retry is on the next cycle rather than after the whole interval.
        await coordinator._async_update_data()
        self.assertTrue(await self.walked(coordinator))

    async def test_repeated_incomplete_walks_are_used_anyway(self):
        """A station that never answers a pack is not walked forever."""
        coordinator = self.make_coordinator(
            2,
            [self.full_walk()] + [self.partial_walk() for _ in range(3)],
        )

        # The complete walk seeds the values the retries fall back to.
        await coordinator._async_update_data()

        for _ in range(2):
            self.walk_next_cycle(coordinator)
            data = await coordinator._async_update_data()
            self.assertEqual(data["pack_slots"], [2, 3])

        # The next incomplete walk is used rather than retrying forever.
        self.walk_next_cycle(coordinator)
        data = await coordinator._async_update_data()

        self.assertEqual(data["pack_slots"], [2])
        self.assertNotIn("pack_3_voltage", data)


class TestFailedReads(unittest.IsolatedAsyncioTestCase):
    """Test what happens when a live connection stops answering."""

    STATION = {"battery_soc": 55}

    def make_coordinator(self, cycles: list[dict | None]):
        """A coordinator whose reader answers each cycle from the list."""
        coordinator = make_coordinator(0)
        coordinator._async_get_client = AsyncMock(return_value=Mock())
        coordinator.reader = Mock(read=AsyncMock(side_effect=cycles))
        return coordinator

    async def test_one_quiet_cycle_leaves_the_connection_alone(self):
        """A busy station resyncs its own pipeline over the live link."""
        coordinator = self.make_coordinator([None])

        self.assertIsNone(await coordinator._async_update_data())

        self.assertFalse(coordinator.disconnected)
        self.assertEqual(1, coordinator._failed_reads)
        # The reader already logged why the read failed.
        coordinator.logger.warning.assert_not_called()

    async def test_second_quiet_cycle_rebuilds_the_connection(self):
        """A half-dead link is not left to report the same error forever."""
        coordinator = self.make_coordinator([None, None])

        await coordinator._async_update_data()
        self.assertIsNone(await coordinator._async_update_data())

        self.assertTrue(coordinator.disconnected)
        self.assertEqual(0, coordinator._failed_reads)
        coordinator.logger.warning.assert_not_called()

    async def test_a_good_cycle_clears_the_count(self):
        """An isolated miss does not count towards a rebuild."""
        coordinator = self.make_coordinator([None, dict(self.STATION), None])

        await coordinator._async_update_data()
        await coordinator._async_update_data()
        self.assertIsNone(await coordinator._async_update_data())

        self.assertFalse(coordinator.disconnected)
        self.assertEqual(1, coordinator._failed_reads)


if __name__ == "__main__":
    unittest.main()
