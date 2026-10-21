"""Tests for applying changed entry options to the running coordinator."""

import asyncio
import logging
import unittest
from datetime import timedelta
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bluetti_bt_lib import FieldName

from custom_components.bluetti_bt import (
    DATA_COORDINATOR,
    DOMAIN,
    _async_update_listener,
    entry_config,
)
from custom_components.bluetti_bt.coordinator import PollingCoordinator


def make_config(
    polling_interval: int = 20,
    polling_timeout: int = 45,
    max_retries: int = 5,
    pack_poll_interval: int = 0,
) -> SimpleNamespace:
    """A config shaped like FullDeviceConfig."""
    return SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="AC300",
        dev_type="AC300",
        use_encryption=False,
        polling_interval=polling_interval,
        polling_timeout=polling_timeout,
        max_retries=max_retries,
        pack_poll_interval=pack_poll_interval,
    )


def make_coordinator(pack_poll_interval: int = 0) -> PollingCoordinator:
    """Coordinator instance built without initializing Home Assistant."""
    coordinator = object.__new__(PollingCoordinator)
    coordinator.logger = Mock(spec=logging.Logger)
    coordinator.hass = SimpleNamespace(async_create_task=asyncio.create_task)
    coordinator.config = make_config(pack_poll_interval=pack_poll_interval)
    coordinator.bluetti_device = SimpleNamespace(max_packs=4)
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
    coordinator._pack_interval = pack_poll_interval * 60
    coordinator._pack_due_at = 0.0
    coordinator._pack_values = {}
    coordinator._pack_num = None
    coordinator._pack_retries = 0
    coordinator._pack_prefixes = tuple(
        f"pack_{slot}_" for slot in range(1, coordinator.bluetti_device.max_packs + 1)
    )
    coordinator.update_interval = timedelta(seconds=20)
    coordinator._update_interval_seconds = 20
    coordinator.async_request_refresh = AsyncMock()
    coordinator._async_drop_connection = AsyncMock()
    return coordinator


class TestApplyOptions(unittest.IsolatedAsyncioTestCase):
    """The live coordinator adopts the values from the options flow."""

    async def test_polling_interval_is_adopted(self):
        """A new interval changes the cadence and polls straight away."""
        coordinator = make_coordinator()

        await coordinator.apply_options(make_config(polling_interval=90))

        self.assertEqual(timedelta(seconds=90), coordinator.update_interval)
        self.assertEqual(90, coordinator._update_interval_seconds)
        coordinator.async_request_refresh.assert_awaited_once()
        coordinator._async_drop_connection.assert_not_awaited()

    async def test_unchanged_options_keep_the_defaults(self):
        """The shipped defaults stay put when nothing moved."""
        coordinator = make_coordinator()

        await coordinator.apply_options(make_config())

        self.assertEqual(timedelta(seconds=20), coordinator.update_interval)
        self.assertEqual(0, coordinator._pack_interval)
        coordinator._async_drop_connection.assert_not_awaited()

    async def test_timeout_change_rebuilds_the_connection(self):
        """The reader is built from the config, so the connection is dropped."""
        coordinator = make_coordinator()
        seen = []

        async def drop_while_locked(reason: str) -> None:
            seen.append((reason, coordinator.connection_lock.locked()))

        coordinator._async_drop_connection = AsyncMock(side_effect=drop_while_locked)

        await coordinator.apply_options(make_config(polling_timeout=90))

        self.assertEqual(1, len(seen))
        self.assertTrue(seen[0][1])
        self.assertEqual(90, coordinator.config.polling_timeout)
        self.assertFalse(coordinator.connection_lock.locked())

    async def test_retry_change_rebuilds_the_connection(self):
        coordinator = make_coordinator()

        await coordinator.apply_options(make_config(max_retries=9))

        coordinator._async_drop_connection.assert_awaited_once()

    async def test_enabling_pack_polling_walks_on_the_next_cycle(self):
        """Enabling pack polling must not wait out an inherited deadline."""
        coordinator = make_coordinator()
        coordinator._pack_due_at = monotonic() + 3600

        await coordinator.apply_options(make_config(pack_poll_interval=15))

        self.assertEqual(15 * 60, coordinator._pack_interval)
        self.assertTrue(coordinator._packs_due())

        # The walk re-arms the deadline, so one change cannot schedule two.
        coordinator._note_packs(
            {FieldName.PACK_NUM.value: 4, FieldName.PACK_SLOTS.value: [1, 2]},
            walked=True,
        )
        self.assertFalse(coordinator._packs_due())

    async def test_disabling_pack_polling_stops_the_walks(self):
        coordinator = make_coordinator(pack_poll_interval=15)

        await coordinator.apply_options(make_config(pack_poll_interval=0))

        self.assertEqual(0, coordinator._pack_interval)
        self.assertFalse(coordinator._packs_due())

    async def test_refresh_is_skipped_while_unloading(self):
        """A late options update must not restart polling on the way out."""
        coordinator = make_coordinator()
        coordinator._unloading = True

        await coordinator.apply_options(make_config(polling_interval=60))

        self.assertEqual(timedelta(seconds=60), coordinator.update_interval)
        coordinator.async_request_refresh.assert_not_awaited()


class FakeEntry:
    """Config entry stand-in with data and options."""

    def __init__(self, options: dict):
        self.entry_id = "entry-1"
        self.title = "AC300"
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
        self.options = options


class TestEntryConfig(unittest.TestCase):
    """Entry options win over the entry data."""

    def test_defaults_are_kept_without_options(self):
        config = entry_config(FakeEntry(options={}))

        self.assertEqual((20, 45, 5, 0), (
            config.polling_interval,
            config.polling_timeout,
            config.max_retries,
            config.pack_poll_interval,
        ))

    def test_options_win_over_data(self):
        config = entry_config(
            FakeEntry(options={"polling_interval": 90, "pack_poll_interval": 15})
        )

        self.assertEqual(90, config.polling_interval)
        self.assertEqual(15, config.pack_poll_interval)
        self.assertEqual(45, config.polling_timeout)


class TestUpdateListener(unittest.IsolatedAsyncioTestCase):
    """The entry's update listener reaches the running coordinator."""

    def make_hass(self, coordinator) -> SimpleNamespace:
        return SimpleNamespace(data={DOMAIN: {"entry-1": {DATA_COORDINATOR: coordinator}}})

    async def test_listener_applies_options_to_the_coordinator(self):
        coordinator = make_coordinator()
        coordinator.apply_options = AsyncMock()
        hass = self.make_hass(coordinator)
        entry = FakeEntry(options={"polling_interval": 90})

        await _async_update_listener(hass, entry)

        coordinator.apply_options.assert_awaited_once()
        config = coordinator.apply_options.await_args.args[0]
        self.assertEqual(90, config.polling_interval)
        self.assertEqual(45, config.polling_timeout)

    async def test_listener_ignores_a_missing_coordinator(self):
        hass = SimpleNamespace(data={})

        await _async_update_listener(hass, FakeEntry(options={"polling_interval": 90}))

    async def test_listener_ignores_unparseable_options(self):
        coordinator = make_coordinator()
        coordinator.apply_options = AsyncMock()
        hass = self.make_hass(coordinator)
        entry = FakeEntry(options={"polling_interval": "not a number"})
        entry.data = {**entry.data, "name": None}

        await _async_update_listener(hass, entry)

        coordinator.apply_options.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
