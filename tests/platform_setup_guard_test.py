"""Guards that every platform bails out before touching an unparseable entry.

Each platform starts ``async_setup_entry`` by parsing ``entry.data`` and then
building a per-device logger from ``config.address``. Three of the five read
``config.address`` *before* checking that the parse succeeded, so a malformed
entry raised ``AttributeError: 'NoneType' object has no attribute 'address'``
inside platform setup instead of the intended clean bail-out.

These tests drive the real ``async_setup_entry`` of all five platforms with an
entry the config parser rejects and a coordinator that passes the
``isinstance`` check, so the only thing keeping setup from proceeding is the
guard. They also pin the two failure reasons apart: an unparseable entry and a
missing coordinator used to share one misleading "No coordinator found" message.
"""

import importlib
import inspect
import logging
import re
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.bluetti_bt.const import DATA_COORDINATOR, DOMAIN
from custom_components.bluetti_bt.coordinator import PollingCoordinator

PLATFORMS = ("sensor", "binary_sensor", "select", "switch", "number")

# A complete, valid entry data dict; each test removes one key to break it.
VALID_DATA = {
    "address": "AA:BB:CC:DD:EE:FF",
    "name": "AC300",
    "type": "AC300",
    "use_encryption": False,
    "polling_interval": 20,
    "polling_timeout": 45,
    "max_retries": 5,
    "pack_poll_interval": 0,
}


def make_entry(**overrides) -> SimpleNamespace:
    """Config entry stand-in whose data parses unless an override breaks it."""
    data = dict(VALID_DATA)
    data.update(overrides)
    return SimpleNamespace(entry_id="entry-1", data=data, options={})


def make_hass(coordinator) -> SimpleNamespace:
    """Home Assistant stand-in holding just the coordinator lookup."""
    return SimpleNamespace(data={DOMAIN: {"entry-1": {DATA_COORDINATOR: coordinator}}})


def valid_coordinator() -> Mock:
    """A coordinator that passes the platform's isinstance check."""
    return Mock(spec=PollingCoordinator)


class TestPlatformSetupGuards(unittest.IsolatedAsyncioTestCase):
    """Every platform must refuse an entry it cannot parse, without raising."""

    async def setup_platform(self, platform_name: str):
        module = importlib.import_module(
            f"custom_components.bluetti_bt.{platform_name}"
        )
        add_entities = Mock()
        return module, add_entities

    async def test_unparseable_entry_bails_out_without_raising(self):
        for platform_name in PLATFORMS:
            with self.subTest(platform=platform_name):
                module, add_entities = await self.setup_platform(platform_name)
                # A coordinator that would otherwise be accepted, so the
                # missing "type" is the only reason setup can stop.
                coordinator = valid_coordinator()
                self.assertIsInstance(coordinator, PollingCoordinator)
                hass = make_hass(coordinator)
                entry = make_entry()
                del entry.data["type"]

                with self.assertLogs(
                    f"custom_components.bluetti_bt.{platform_name}", logging.ERROR
                ) as logs:
                    await module.async_setup_entry(hass, entry, add_entities)

                add_entities.assert_not_called()
                self.assertEqual(
                    len(logs.output),
                    1,
                    "expected exactly one error, not a second one from the "
                    "coordinator branch",
                )
                self.assertIn("Unable to parse Bluetti config entry", logs.output[0])

    async def test_missing_address_is_also_refused(self):
        for platform_name in PLATFORMS:
            with self.subTest(platform=platform_name):
                module, add_entities = await self.setup_platform(platform_name)
                hass = make_hass(valid_coordinator())

                with self.assertLogs(
                    f"custom_components.bluetti_bt.{platform_name}", logging.ERROR
                ):
                    await module.async_setup_entry(
                        hass, make_entry(address=None), add_entities
                    )

                add_entities.assert_not_called()

    async def test_non_coordinator_is_refused_after_the_logger_is_built(self):
        """The second guard still works, and now names the real cause."""
        for platform_name in PLATFORMS:
            with self.subTest(platform=platform_name):
                module, add_entities = await self.setup_platform(platform_name)
                hass = make_hass(Mock())
                entry = make_entry()

                with self.assertLogs(
                    f"custom_components.bluetti_bt.{platform_name}", logging.ERROR
                ) as logs:
                    await module.async_setup_entry(hass, entry, add_entities)

                add_entities.assert_not_called()
                self.assertIn("No coordinator found", logs.output[0])
                # The address was available, so the logger could be named for
                # the device -- masked, because logger names end up in logs.
                self.assertEqual(
                    logs.records[0].name,
                    f"custom_components.bluetti_bt.{platform_name}.XX_XX_XX_XX_XX_FF",
                )

    async def test_all_platforms_read_the_guard_before_the_address(self):
        """The five copies must not drift apart again.

        ``const.py`` documents an earlier case where four of five copies of a
        guard drifted, and this one drifted three ways. Rather than compare the
        wording of five hand-maintained blocks, pin the invariant itself: the
        ``config is None`` check must come before ``config.address`` is read.
        """
        for platform_name in PLATFORMS:
            with self.subTest(platform=platform_name):
                module = importlib.import_module(
                    f"custom_components.bluetti_bt.{platform_name}"
                )
                source = inspect.getsource(module.async_setup_entry)
                # Drop the docstring so its wording can't satisfy the search.
                body = re.sub(r'^\s*""".*?"""\s*', "", source, count=1, flags=re.DOTALL)

                guard = body.find("if config is None")
                address = body.find("config.address")

                self.assertGreaterEqual(
                    guard,
                    0,
                    "the platform no longer checks for an unparseable entry",
                )
                self.assertGreaterEqual(
                    address,
                    0,
                    "the platform no longer logs the address it is setting up",
                )
                self.assertLess(
                    guard,
                    address,
                    "config.address is read before the parse is checked, so a "
                    "malformed entry raises AttributeError inside platform setup",
                )


if __name__ == "__main__":
    unittest.main()
