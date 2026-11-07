"""Tests for identifying a device from the name it advertises."""

import asyncio
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bluetti_bt_lib.devices import DEVICES
from bluetti_bt_lib.utils.device_builder import build_device
from custom_components.bluetti_bt.config_flow import (
    KNOWN_DEVICE_TYPES,
    BluettiConfigFlow,
    identify_from_bt_name,
    manual_device_name,
    selectable_device_types,
)
from custom_components.bluetti_bt.types import (
    CONF_ENCRYPTION_ASSUMED,
    CONF_TYPE,
    CONF_USE_ENCRYPTION,
    ManufacturerData,
)
from custom_components.bluetti_bt.utils import (
    build_bluetti_device,
    device_class_for_type,
)


class TestIdentifyFromBtName(unittest.TestCase):
    """Test the advertised name parser."""

    def test_advertised_names(self):
        """Names seen on real devices identify the model, serial and channel."""
        cases = [
            ("AC200M1234567890123", "AC200M", False),
            ("AC3001234567890123", "AC300", False),
            ("AP3002519000242488", "AP300", True),
            ("EL102543110143137", "EL10", True),
            ("EL100V22539111584127", "EL100V2", True),
            ("EL100mini1234567890123", "EL100mini", True),
            ("Elite 200 V22512002239825", "Elite 200 V2", True),
            ("EL30V21234567890123", "EL30V2", True),
            ("EL3001234567890123", "EL300", True),
            ("CHARGER2123456789012", "CHARGER2", True),
        ]

        for advertised, dev_type, use_encryption in cases:
            with self.subTest(advertised):
                identified = identify_from_bt_name(advertised)

                self.assertIsNotNone(identified)
                self.assertEqual(identified[0], dev_type)
                self.assertEqual(identified[1], advertised)
                self.assertEqual(identified[2], use_encryption)

    def test_charger_2_advertises_a_space(self):
        """The Charger 2 name is closed up to match the device table key."""
        identified = identify_from_bt_name("CHARGER 22551114213971")

        self.assertIsNotNone(identified)
        self.assertEqual(identified[0], "CHARGER2")
        self.assertEqual(identified[1], "CHARGER22551114213971")
        self.assertTrue(identified[2])

        # The entry name is built from this, so it has to be buildable
        self.assertEqual(type(build_device(identified[1])).__name__, "CHARGER2")

    def test_elite_200_v2_name_survives_the_entry_name(self):
        """The Elite 200 V2 is the one model whose name keeps a space.

        The entry stores only the capitals and digits of what was advertised,
        which leaves the E200V2 spelling the library aliases back to the model.
        """
        identified = identify_from_bt_name("Elite 200 V22512002239825")

        self.assertIsNotNone(identified)
        self.assertEqual(identified[0], "Elite 200 V2")
        self.assertTrue(identified[2])

        entry_name = re.sub("[^A-Z0-9]+", "", identified[1])
        self.assertEqual(entry_name, "E200V22512002239825")
        self.assertEqual(type(build_device(entry_name)).__name__, "EL200V2")

    def test_handsfree_name_is_still_buildable(self):
        """The shortcut skips Handsfree, but its stored name still builds.

        A Handsfree keeps its space in the device table, so the entry name
        closes it to H1<serial>. The library aliases that spelling from
        v1.0.12-beta.4.
        """
        entry_name = re.sub("[^A-Z0-9]+", "", "Handsfree 112345678")
        self.assertEqual(entry_name, "H112345678")

        device = build_device(entry_name)

        self.assertIsNotNone(device)
        self.assertEqual(type(device).__name__, "Handsfree1")

    def test_every_known_type_is_buildable(self):
        """The shortcut only lists types the library can actually build."""
        for dev_type, use_encryption in KNOWN_DEVICE_TYPES.items():
            with self.subTest(dev_type=dev_type):
                identified = identify_from_bt_name(f"{dev_type}12345678")

                self.assertIsNotNone(identified)
                self.assertEqual(identified[2], use_encryption)
                self.assertIsNotNone(build_device(identified[1]))

    def test_other_names_fall_back_to_probing(self):
        """Anything unproven keeps using the connection based recognition."""
        for name in [
            "AC50012345678",  # later revisions encrypt
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
        flow.async_show_form = Mock(return_value="form")
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
        self.assertTrue(flow._encryption_assumed)
        self.assertEqual(
            flow.context["title_placeholders"], {"name": "EL102543110143137"}
        )

    async def test_known_unencrypted_name_skips_the_probe(self):
        """A model with a known channel is added without probing it."""
        flow = self.make_flow()
        discovery = self.make_discovery("AC3001234567890123")

        with patch(
            "custom_components.bluetti_bt.config_flow.recognize_device"
        ) as recognize:
            result = await flow.async_step_bluetooth(discovery)

        recognize.assert_not_called()
        self.assertEqual(result, "created")

        manufacturer_data = ManufacturerData.from_dict(
            flow._discovery_info.manufacturer_data
        )
        self.assertEqual(manufacturer_data.dev_type, "AC300")
        self.assertFalse(manufacturer_data.use_encryption)

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
        self.assertFalse(flow._encryption_assumed)

    async def test_unrecognised_name_asks_the_user(self):
        """A station nothing recognised is offered to the user, not refused."""
        flow = self.make_flow()
        discovery = self.make_discovery("PBOX12345678")

        with patch(
            "custom_components.bluetti_bt.config_flow.recognize_device",
            new=AsyncMock(return_value=None),
        ):
            result = await flow.async_step_bluetooth(discovery)

        flow.async_abort.assert_not_called()
        self.assertEqual(result, "form")
        self.assertEqual(
            flow.async_show_form.call_args.kwargs["step_id"], "device_type"
        )
        # The advertisement is kept, since it still carries the serial number
        self.assertEqual(flow._discovery_info.name, "PBOX12345678")

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


class TestManualDeviceName(unittest.TestCase):
    """Test the name a manually picked model is stored under."""

    def test_serial_is_kept_from_the_advertisement(self):
        """The serial number outlives a name whose model was not recognised."""
        self.assertEqual(
            manual_device_name("AC500", "AC50012345678"), "AC50012345678"
        )
        self.assertEqual(
            manual_device_name("CHARGER2", "CHARGER 22551114213971"),
            "CHARGER22551114213971",
        )

    def test_bare_type_when_the_advertisement_cannot_be_read(self):
        for advertised in ("PBOX12345678", "el102543110143137", "", None):
            with self.subTest(advertised=advertised):
                self.assertEqual(manual_device_name("AC300", advertised), "AC300")

    def test_the_stored_entry_still_builds(self):
        """However the name turned out, the picked type builds the device."""
        for advertised in ("AC50012345678", "PBOX12345678", None):
            for dev_type in selectable_device_types():
                name = re.sub(
                    "[^A-Z0-9]+", "", manual_device_name(dev_type, advertised)
                )

                with self.subTest(dev_type=dev_type, advertised=advertised):
                    self.assertIsNotNone(build_bluetti_device(dev_type, name))


class TestSelectableDeviceTypes(unittest.TestCase):
    """Test the models a manual pick offers."""

    def test_the_whole_device_table_is_offered(self):
        self.assertEqual(set(selectable_device_types()), set(DEVICES))

    def test_known_models_come_first(self):
        """The stations with a known channel sit at the top of the list."""
        offered = selectable_device_types()
        known = [dev_type for dev_type in offered if dev_type in KNOWN_DEVICE_TYPES]

        self.assertEqual(len(known), len(KNOWN_DEVICE_TYPES))
        self.assertEqual(offered[: len(known)], known)

    def test_every_offered_model_is_buildable(self):
        for dev_type in selectable_device_types():
            with self.subTest(dev_type=dev_type):
                self.assertIs(device_class_for_type(dev_type), DEVICES[dev_type])


class TestManualDeviceTypeStep(unittest.IsolatedAsyncioTestCase):
    """Test the step that asks the user about an unrecognised station."""

    def make_flow(self):
        """Create a config flow instance without initializing Home Assistant."""
        flow = object.__new__(BluettiConfigFlow)
        flow._discovery_info = None
        flow._encryption_assumed = False
        flow.context = {}
        flow.hass = SimpleNamespace(loop=asyncio.get_running_loop())
        flow.async_set_unique_id = AsyncMock()
        flow._async_current_entries = Mock(return_value=[])
        flow._abort_if_unique_id_configured = Mock()
        flow.async_abort = Mock(return_value="aborted")
        flow.async_show_form = Mock(return_value="form")
        flow.async_create_entry = Mock(return_value="created")
        flow.async_step_user = AsyncMock(return_value="created")
        return flow

    def make_discovery(self, name):
        """An advertisement for a station nothing recognised."""
        return SimpleNamespace(
            address="CB:DC:14:DC:2A:C0",
            name=name,
            manufacturer_data={},
        )

    async def form(self, flow):
        """The fields the step offers, with their defaults."""
        await flow.async_step_device_type()

        schema = flow.async_show_form.call_args.kwargs["data_schema"]

        return {
            str(key): key.default() if callable(key.default) else key.default
            for key in schema.schema
        }

    async def test_the_form_asks_for_model_and_channel(self):
        flow = self.make_flow()
        flow._discovery_info = self.make_discovery("PBOX12345678")

        fields = await self.form(flow)

        self.assertEqual(sorted(fields), sorted([CONF_TYPE, CONF_USE_ENCRYPTION]))
        # Most current stations encrypt, so that is where the box starts
        self.assertTrue(fields[CONF_USE_ENCRYPTION])

    async def test_the_model_field_offers_the_device_table(self):
        flow = self.make_flow()
        flow._discovery_info = self.make_discovery("PBOX12345678")

        await flow.async_step_device_type()

        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        offered = next(
            validator.container
            for key, validator in schema.schema.items()
            if key.schema == CONF_TYPE
        )

        self.assertEqual(set(offered), set(DEVICES))

    async def test_without_a_discovery_there_is_nothing_to_ask(self):
        flow = self.make_flow()

        result = await flow.async_step_device_type()

        self.assertEqual(result, "aborted")
        flow.async_abort.assert_called_once_with(reason="no_unconfigured_devices")

    async def test_a_pick_continues_into_the_user_step(self):
        flow = self.make_flow()
        flow._discovery_info = self.make_discovery("AC50012345678")

        result = await flow.async_step_device_type(
            {CONF_TYPE: "AC500", CONF_USE_ENCRYPTION: True}
        )

        self.assertEqual(result, "created")
        flow.async_step_user.assert_awaited_once()

        manufacturer_data = ManufacturerData.from_dict(
            flow._discovery_info.manufacturer_data
        )
        self.assertEqual(manufacturer_data.dev_type, "AC500")
        self.assertTrue(manufacturer_data.use_encryption)
        self.assertEqual(flow._discovery_info.name, "AC50012345678")
        self.assertTrue(flow._encryption_assumed)
        self.assertEqual(
            flow.context["title_placeholders"], {"name": "AC50012345678"}
        )

    async def test_an_unreadable_advertisement_leaves_the_bare_type(self):
        flow = self.make_flow()
        flow._discovery_info = self.make_discovery("PBOX12345678")

        await flow.async_step_device_type(
            {CONF_TYPE: "AC300", CONF_USE_ENCRYPTION: False}
        )

        manufacturer_data = ManufacturerData.from_dict(
            flow._discovery_info.manufacturer_data
        )
        self.assertEqual(manufacturer_data.dev_type, "AC300")
        self.assertFalse(manufacturer_data.use_encryption)
        self.assertEqual(flow._discovery_info.name, "AC300")

    async def test_the_entry_is_created_as_a_recognised_one_is(self):
        """The manual pick has to end in the same entry the probe path makes."""
        flow = self.make_flow()
        flow._discovery_info = self.make_discovery("AC50012345678")
        # Run the real user step so the stored entry is checked end to end
        flow.async_step_user = BluettiConfigFlow.async_step_user.__get__(flow)

        await flow.async_step_device_type(
            {CONF_TYPE: "AC500", CONF_USE_ENCRYPTION: True}
        )
        # The user still confirms the address, exactly as a recognised device does
        result = await flow.async_step_user({})

        self.assertEqual(result, "created")

        entry = flow.async_create_entry.call_args.kwargs
        self.assertEqual(entry["title"], "AC50012345678")
        self.assertEqual(entry["data"][CONF_TYPE], "AC500")
        self.assertTrue(entry["data"][CONF_USE_ENCRYPTION])
        self.assertTrue(entry["data"][CONF_ENCRYPTION_ASSUMED])


if __name__ == "__main__":
    unittest.main()
