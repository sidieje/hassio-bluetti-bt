"""Tests for identifying a device from the name it advertises."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bluetti_bt_lib.utils.device_builder import build_device
from custom_components.bluetti_bt.config_flow import (
    ENCRYPTED_DEVICE_TYPES,
    BluettiConfigFlow,
    identify_from_bt_name,
)
from custom_components.bluetti_bt.types import ManufacturerData


class TestIdentifyFromBtName(unittest.TestCase):
    """Test the advertised name parser."""

    def test_advertised_names(self):
        """Names seen on real devices identify the model and its serial."""
        cases = [
            ("AP3002519000242488", "AP300"),
            ("EL102543110143137", "EL10"),
            ("EL100V22539111584127", "EL100V2"),
            ("EL100mini1234567890123", "EL100mini"),
            ("EL200V21234567890123", "EL200V2"),
            ("EL30V21234567890123", "EL30V2"),
            ("EL3001234567890123", "EL300"),
            ("CHARGER2123456789012", "CHARGER2"),
        ]

        for advertised, dev_type in cases:
            with self.subTest(advertised):
                identified = identify_from_bt_name(advertised)

                self.assertIsNotNone(identified)
                self.assertEqual(identified[0], dev_type)
                self.assertEqual(identified[1], advertised)
                self.assertTrue(identified[2])

    def test_charger_2_advertises_a_space(self):
        """The Charger 2 name is closed up to match the device table key."""
        identified = identify_from_bt_name("CHARGER 22551114213971")

        self.assertIsNotNone(identified)
        self.assertEqual(identified[0], "CHARGER2")
        self.assertEqual(identified[1], "CHARGER22551114213971")
        self.assertTrue(identified[2])

        # The entry name is built from this, so it has to be buildable
        self.assertEqual(type(build_device(identified[1])).__name__, "CHARGER2")

    def test_every_encrypted_type_is_buildable(self):
        """The shortcut only lists types the library can actually build."""
        for dev_type in ENCRYPTED_DEVICE_TYPES:
            with self.subTest(dev_type=dev_type):
                self.assertIsNotNone(identify_from_bt_name(f"{dev_type}12345678"))

    def test_other_names_fall_back_to_probing(self):
        """Anything unproven keeps using the connection based recognition."""
        for name in [
            "AC30012345678",  # unencrypted model
            "EP50012345678",
            "PR30V212345678",
            "PBOX12345678",  # advertises a generic name
            "EBOX12345678",
            "EL10",  # no serial number to take with it
            "el102543110143137",
            "EL10V312345678",
            "Handsfree 112345678",  # keeps its space in the device table
            "",
            None,
        ]:
            with self.subTest(name=name):
                self.assertIsNone(identify_from_bt_name(name))


class TestDiscoveryShortcut(unittest.IsolatedAsyncioTestCase):
    """Test that a recognised name costs no extra bluetooth connection."""

    def make_flow(self, configured_addresses=()):
        """Create a config flow instance without initializing Home Assistant."""
        flow = object.__new__(BluettiConfigFlow)
        flow._discovery_info = None
        flow.context = {}
        flow.hass = SimpleNamespace(loop=asyncio.get_running_loop())
        flow.async_set_unique_id = AsyncMock()
        flow._async_current_entries = Mock(
            return_value=[
                SimpleNamespace(unique_id=address)
                for address in configured_addresses
            ]
        )
        flow._abort_if_unique_id_configured = Mock()
        flow.async_abort = Mock(return_value="aborted")
        flow.async_step_user = AsyncMock(return_value="created")
        return flow

    def make_discovery(self, name):
        """An advertisement for a powerstation."""
        return SimpleNamespace(
            address="CB:DC:14:DC:2A:C0",
            name=name,
            manufacturer_data={},
        )

    async def test_identified_name_skips_the_probe(self):
        flow = self.make_flow()
        discovery = self.make_discovery("EL102543110143137")

        with patch(
            "custom_components.bluetti_bt.config_flow.recognize_device"
        ) as recognize:
            result = await flow.async_step_bluetooth(discovery)

        recognize.assert_not_called()
        self.assertEqual(result, "created")

        manufacturer_data = ManufacturerData.from_dict(
            flow._discovery_info.manufacturer_data
        )
        self.assertEqual(manufacturer_data.dev_type, "EL10")
        self.assertTrue(manufacturer_data.use_encryption)
        self.assertEqual(flow._discovery_info.name, "EL102543110143137")
        self.assertEqual(
            flow.context["title_placeholders"], {"name": "EL102543110143137"}
        )

    async def test_charger_2_name_skips_the_probe(self):
        """The Charger 2 answers on registers the generic probes miss."""
        flow = self.make_flow()
        discovery = self.make_discovery("CHARGER 22551114213971")

        with patch(
            "custom_components.bluetti_bt.config_flow.recognize_device"
        ) as recognize:
            result = await flow.async_step_bluetooth(discovery)

        recognize.assert_not_called()
        self.assertEqual(result, "created")

        manufacturer_data = ManufacturerData.from_dict(
            flow._discovery_info.manufacturer_data
        )
        self.assertEqual(manufacturer_data.dev_type, "CHARGER2")
        self.assertTrue(manufacturer_data.use_encryption)
        self.assertEqual(flow._discovery_info.name, "CHARGER22551114213971")

    async def test_unrecognised_name_still_probes(self):
        flow = self.make_flow()
        discovery = self.make_discovery("PBOX12345678")
        recognized = SimpleNamespace(
            name="AC300",
            iot_version=1,
            encrypted=False,
            full_name="AC30012345678",
        )

        with patch(
            "custom_components.bluetti_bt.config_flow.recognize_device",
            new=AsyncMock(return_value=recognized),
        ) as recognize:
            result = await flow.async_step_bluetooth(discovery)

        recognize.assert_awaited_once()
        self.assertEqual(result, "created")

        manufacturer_data = ManufacturerData.from_dict(
            flow._discovery_info.manufacturer_data
        )
        self.assertEqual(manufacturer_data.dev_type, "AC300")
        self.assertFalse(manufacturer_data.use_encryption)
        self.assertEqual(flow._discovery_info.name, "AC30012345678")

    async def test_unsupported_device_aborts(self):
        flow = self.make_flow()
        discovery = self.make_discovery("PBOX12345678")

        with patch(
            "custom_components.bluetti_bt.config_flow.recognize_device",
            new=AsyncMock(return_value=None),
        ):
            result = await flow.async_step_bluetooth(discovery)

        self.assertEqual(result, "aborted")
        flow.async_abort.assert_called_once_with(reason="Device type not supported")
        self.assertIsNone(flow._discovery_info)

    async def test_configured_device_says_why_it_was_skipped(self):
        """A duplicate advertisement is reported instead of failing silently."""
        flow = self.make_flow(configured_addresses=["CB:DC:14:DC:2A:C0"])
        discovery = self.make_discovery("EL102543110143137")
        flow._abort_if_unique_id_configured = Mock(side_effect=RuntimeError("abort"))

        with patch(
            "custom_components.bluetti_bt.config_flow.recognize_device"
        ) as recognize, self.assertLogs(
            "custom_components.bluetti_bt.config_flow", level="INFO"
        ) as logs:
            with self.assertRaises(RuntimeError):
                await flow.async_step_bluetooth(discovery)

        recognize.assert_not_called()
        self.assertIn("already configured", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
