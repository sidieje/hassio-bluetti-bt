"""Tests for config entry setup: coordinator teardown and platform rollback."""

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntryNotReady, ConfigEntryState
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError

from custom_components.bluetti_bt import (
    DATA_COORDINATOR,
    DOMAIN,
    PLATFORMS,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.bluetti_bt.coordinator import PollingCoordinator


class FakeEntry:
    """Config entry stand-in that records the teardown callbacks it is given."""

    def __init__(self, options: dict | None = None):
        self.entry_id = "entry-1"
        self.title = "AC300"
        self.state = ConfigEntryState.SETUP_IN_PROGRESS
        self.data = {
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "AC300",
            "type": "AC300",
            "use_encryption": False,
            "polling_interval": 20,
            "polling_timeout": 45,
            "max_retries": 5,
            "pack_poll_interval": 0,
        }
        self.options = options if options is not None else {}
        self.on_unload = []
        self.update_listeners = []

    def async_on_unload(self, func) -> None:
        self.on_unload.append(func)

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)

        def remove() -> None:
            if listener in self.update_listeners:
                self.update_listeners.remove(listener)

        return remove


def make_hass() -> SimpleNamespace:
    """Home Assistant stand-in with the pieces setup uses."""
    return SimpleNamespace(
        data={},
        loop=asyncio.get_running_loop(),
        async_create_task=asyncio.create_task,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )


def make_coordinator_class(
    first_refresh: Exception | None = None,
    order: list | None = None,
    bluetti_device: object = "supported",
):
    """Build a PollingCoordinator stand-in that passes isinstance checks.

    bluetti_device is anything truthy by default, because setup refuses an
    entry whose device type the library does not recognise. Pass None to get
    that refusal.
    """

    class FakeCoordinator(PollingCoordinator):
        instances: list = []

        def __init__(self, hass, config, lock, config_entry):
            self.hass = hass
            self.config = config
            self.config_entry = config_entry
            self.logger = Mock(spec=logging.Logger)
            self.connection_lock = asyncio.Lock()
            # The real coordinator builds the device in its constructor, so
            # setup can refuse an unsupported type before it ever polls.
            self.bluetti_device = bluetti_device
            self.consecutive_failures = 0
            self.last_update_success = True
            # The real coordinator keeps the exception that ended a refresh
            # here, and setup reports it when it survives an offline first
            # refresh.
            self.last_exception = (
                first_refresh if isinstance(first_refresh, Exception) else None
            )
            self.data = None
            self.async_config_entry_first_refresh = AsyncMock(side_effect=first_refresh)
            self.apply_options = AsyncMock()

            def note_unload() -> None:
                if order is not None:
                    order.append("unload")

            self.async_unload = AsyncMock(side_effect=note_unload)
            FakeCoordinator.instances.append(self)

    return FakeCoordinator


async def run_on_unload(entry: FakeEntry) -> None:
    """Run the entry's on_unload callbacks the way Home Assistant does."""
    while entry.on_unload:
        job = entry.on_unload.pop()()
        if job is not None:
            await job


class TestSetupTeardown(unittest.IsolatedAsyncioTestCase):
    """A failed or retried setup must not leave anything running."""

    async def test_fatal_first_refresh_unloads_coordinator(self):
        """A ConfigEntryError leaves no coordinator and no entry data.

        Only ConfigEntryNotReady is survived: an error that says the entry can
        never work still ends setup, and its teardown must still run.
        """
        hass = make_hass()
        entry = FakeEntry()
        coordinator_class = make_coordinator_class(
            first_refresh=ConfigEntryError("no bluetooth connection")
        )

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            with self.assertRaises(ConfigEntryError):
                await async_setup_entry(hass, entry)

            coordinator = coordinator_class.instances[0]
            await run_on_unload(entry)

        coordinator.async_unload.assert_awaited_once()
        self.assertNotIn(entry.entry_id, hass.data.get(DOMAIN, {}))

    async def test_unsupported_device_type_refuses_the_entry(self):
        """A type the library cannot build is fatal, not retried forever.

        ConfigEntryError puts the entry in SETUP_ERROR with the reason shown
        in the UI; ConfigEntryNotReady would retry on a backoff that can
        never succeed.
        """
        hass = make_hass()
        entry = FakeEntry()
        entry.data = {**entry.data, "type": "NOT_A_REAL_TYPE"}
        coordinator_class = make_coordinator_class(bluetti_device=None)

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            with self.assertRaises(ConfigEntryError):
                await async_setup_entry(hass, entry)

            coordinator = coordinator_class.instances[0]
            await run_on_unload(entry)

        self.assertNotIn(entry.entry_id, hass.data[DOMAIN])
        coordinator.async_unload.assert_awaited_once()
        coordinator.async_config_entry_first_refresh.assert_not_awaited()
        hass.config_entries.async_forward_entry_setups.assert_not_awaited()

    async def test_teardown_registered_before_first_refresh(self):
        """Teardown must exist before the first poll can fail."""
        hass = make_hass()
        entry = FakeEntry()
        coordinator_class = make_coordinator_class(
            first_refresh=ConfigEntryError("no bluetooth connection")
        )

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            with self.assertRaises(ConfigEntryError):
                await async_setup_entry(hass, entry)

            coordinator = coordinator_class.instances[0]
            registered = len(entry.on_unload)
            await run_on_unload(entry)

        self.assertEqual(2, registered)
        coordinator.async_unload.assert_awaited_once()

    async def test_offline_first_refresh_still_loads_the_entry(self):
        """A station that is not there is not a reason to refuse the entry."""
        hass = make_hass()
        entry = FakeEntry()
        coordinator_class = make_coordinator_class(
            first_refresh=ConfigEntryNotReady()
        )

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            self.assertTrue(await async_setup_entry(hass, entry))

        coordinator = coordinator_class.instances[0]
        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry, PLATFORMS
        )
        self.assertIs(
            hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR], coordinator
        )
        self.assertEqual(1, len(entry.update_listeners))

    async def test_coordinator_receives_the_config_entry(self):
        """The coordinator is built with its entry so HA can schedule it."""
        hass = make_hass()
        entry = FakeEntry()
        coordinator_class = make_coordinator_class()

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            self.assertTrue(await async_setup_entry(hass, entry))

        coordinator = coordinator_class.instances[0]
        self.assertIs(entry, coordinator.config_entry)
        self.assertIs(hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR], coordinator)

    async def test_removal_keeps_a_replacement_coordinator(self):
        """A stale setup must not drop the coordinator that replaced it."""
        hass = make_hass()
        entry = FakeEntry()
        coordinator_class = make_coordinator_class()

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            self.assertTrue(await async_setup_entry(hass, entry))
            replacement = coordinator_class(hass, None, None, entry)

        hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR] = replacement

        await run_on_unload(entry)

        self.assertIs(hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR], replacement)

    async def test_unload_callback_order_is_lifo(self):
        """The coordinator is unloaded before its entry data is dropped."""
        hass = make_hass()
        entry = FakeEntry()
        order = []
        coordinator_class = make_coordinator_class(order=order)

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            self.assertTrue(await async_setup_entry(hass, entry))
            coordinator = coordinator_class.instances[0]

        # Home Assistant pops the callbacks in reverse registration order.
        self.assertEqual(3, len(entry.on_unload))
        entry.on_unload.pop()()
        await entry.on_unload.pop()()
        entry.on_unload.pop()()
        order.append("forget")

        self.assertEqual(["unload", "forget"], order)
        coordinator.async_unload.assert_awaited_once()
        self.assertNotIn(entry.entry_id, hass.data[DOMAIN])

    async def test_platform_failure_unloads_platforms_and_keeps_error(self):
        """Already forwarded platforms are rolled back, original error raised."""
        hass = make_hass()
        entry = FakeEntry()
        coordinator_class = make_coordinator_class()
        hass.config_entries.async_forward_entry_setups = AsyncMock(
            side_effect=HomeAssistantError("platform boom")
        )

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            with self.assertRaises(HomeAssistantError) as raised:
                await async_setup_entry(hass, entry)

            coordinator = coordinator_class.instances[0]
            await run_on_unload(entry)

        self.assertEqual("platform boom", str(raised.exception))
        hass.config_entries.async_unload_platforms.assert_awaited_once_with(
            entry, PLATFORMS
        )
        coordinator.async_unload.assert_awaited_once()
        self.assertNotIn(entry.entry_id, hass.data.get(DOMAIN, {}))

    async def test_rollback_failure_does_not_hide_setup_error(self):
        """A cleanup error is logged, the setup error still propagates."""
        hass = make_hass()
        entry = FakeEntry()
        coordinator_class = make_coordinator_class()
        hass.config_entries.async_forward_entry_setups = AsyncMock(
            side_effect=HomeAssistantError("platform boom")
        )
        hass.config_entries.async_unload_platforms = AsyncMock(
            side_effect=RuntimeError("cleanup failed")
        )

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            with self.assertRaises(HomeAssistantError) as raised:
                await async_setup_entry(hass, entry)

            coordinator = coordinator_class.instances[0]
            await run_on_unload(entry)

        self.assertEqual("platform boom", str(raised.exception))
        coordinator.async_unload.assert_awaited_once()

    async def test_successful_setup_registers_update_listener(self):
        """Options changes reach the coordinator through the listener."""
        hass = make_hass()
        entry = FakeEntry()
        coordinator_class = make_coordinator_class()

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            self.assertTrue(await async_setup_entry(hass, entry))

        self.assertEqual(1, len(entry.update_listeners))
        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry, PLATFORMS
        )

    async def test_unload_entry_unloads_coordinator(self):
        """Unloading an entry tears the coordinator down."""
        hass = make_hass()
        entry = FakeEntry()
        coordinator_class = make_coordinator_class()

        with patch("custom_components.bluetti_bt.PollingCoordinator", coordinator_class):
            self.assertTrue(await async_setup_entry(hass, entry))
            coordinator = coordinator_class.instances[0]
            self.assertTrue(await async_unload_entry(hass, entry))

        coordinator.async_unload.assert_awaited_once()
        self.assertNotIn(entry.entry_id, hass.data[DOMAIN])

    async def test_repeated_coordinator_unload_is_safe(self):
        """The on_unload callback and async_unload_entry can both fire."""
        coordinator = make_real_coordinator()
        client, reader = coordinator.client, coordinator.reader

        await coordinator.async_unload()
        await coordinator.async_unload()

        self.assertTrue(coordinator._unloading)
        self.assertIsNone(coordinator.client)
        client.disconnect.assert_awaited_once()
        reader.disconnect.assert_awaited_once()

    async def test_offline_setup_loads_the_entry_and_keeps_its_ladder(self):
        """An unreachable device loads, retries fast, and leaks no task.

        Failing setup here instead handed the retry to Home Assistant, whose
        backoff tops out at 80s and then waits behind another full connection
        attempt, so a station powered back on took about five minutes to come
        back. The coordinator's own ladder retries after 20, 30 then every 60s,
        so the entry has to load with that timer armed and the platforms
        forwarded. Bootstrap still must not end up waiting on a task of ours.
        """
        hass = make_hass()
        entry = FakeEntry()
        # The library wants a name with the serial appended, like the devices
        # advertise over bluetooth.
        entry.data = {**entry.data, "name": "AC3001234567890"}
        before = asyncio.all_tasks()

        with patch(
            "custom_components.bluetti_bt.coordinator.BleakScanner.find_device_by_address",
            new=AsyncMock(return_value=None),
        ):
            self.assertTrue(await async_setup_entry(hass, entry))

        coordinator = hass.data[DOMAIN][entry.entry_id][DATA_COORDINATOR]

        self.assertTrue(coordinator._offline)
        self.assertIsNone(coordinator.data)
        self.assertEqual(1, coordinator.consecutive_failures)
        self.assertIsNotNone(coordinator._retry_unsub)
        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry, PLATFORMS
        )
        self.assertEqual(set(), asyncio.all_tasks() - before)

        # Unloading the loaded entry has to take the ladder with it.
        with patch(
            "custom_components.bluetti_bt.coordinator.BleakScanner.find_device_by_address",
            new=AsyncMock(return_value=None),
        ):
            self.assertTrue(await async_unload_entry(hass, entry))

        self.assertIsNone(coordinator._retry_unsub)
        self.assertEqual(set(), asyncio.all_tasks() - before)
        self.assertNotIn(entry.entry_id, hass.data[DOMAIN])


def make_real_coordinator() -> PollingCoordinator:
    """Coordinator instance built without initializing Home Assistant."""
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
        pack_poll_interval=0,
    )
    coordinator.bluetti_device = SimpleNamespace(max_packs=4)
    coordinator.client = AsyncMock()
    coordinator.reader = AsyncMock()
    coordinator.writer = AsyncMock()
    coordinator.disconnected = False
    coordinator.connection_lock = asyncio.Lock()
    coordinator.lock = asyncio.Lock()
    coordinator._offline = False
    coordinator._retry_attempt = False
    coordinator._retry_delay_index = 0
    coordinator._retry_unsub = None
    coordinator._retry_attempt_task = None
    coordinator._unloading = False
    coordinator._pack_interval = 0
    coordinator._pack_due_at = 0.0
    coordinator._pack_values = {}
    coordinator._pack_num = None
    coordinator._pack_retries = 0
    coordinator._pack_prefixes = tuple(
        f"pack_{slot}_" for slot in range(1, coordinator.bluetti_device.max_packs + 1)
    )
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


if __name__ == "__main__":
    unittest.main()
