"""Tests for building the device a config entry refers to."""

import re
import unittest

from bluetti_bt_lib.devices import DEVICES, DEVICE_ALIASES
from bluetti_bt_lib.devices.el10 import EL10
from bluetti_bt_lib.devices.el100mini import EL100mini

from custom_components.bluetti_bt.utils import (
    build_bluetti_device,
    device_class_for_type,
)


def entry_name(advertised: str) -> str:
    """The name the config flow stores for an advertisement."""
    return re.sub("[^A-Z0-9]+", "", advertised)


class TestStoredType(unittest.TestCase):
    """The type stored when the device was added is the exact one."""

    def test_el100mini_type_beats_its_lossy_name(self):
        """An EL100mini is not an EL10, whatever its stored name says.

        The entry name keeps only the capitals and digits of what was
        advertised, so EL100mini<serial> is stored as EL100<serial>, which the
        library reads back as an EL10 with a serial that starts with a zero.
        """
        name = entry_name("EL100mini2345678901234")
        self.assertEqual(name, "EL1002345678901234")

        # What the name alone still builds, which is why the type comes first
        self.assertIsInstance(build_bluetti_device(None, name), EL10)

        device = build_bluetti_device("EL100mini", name)

        self.assertIsInstance(device, EL100mini)
        self.assertNotIsInstance(device, EL10)

    def test_type_register_spelling_is_an_alias(self):
        """The Charger 2 answers with a space the device table does not use."""
        device = build_bluetti_device("CHARGER 2", "CHARGER22551114213971")

        self.assertIsNotNone(device)
        self.assertEqual(type(device).__name__, "CHARGER2")

    def test_every_stored_spelling_builds(self):
        """Both the table keys and the aliases name a buildable device."""
        for dev_type in list(DEVICES) + list(DEVICE_ALIASES):
            with self.subTest(dev_type=dev_type):
                self.assertIsNotNone(build_bluetti_device(dev_type, None))

    def test_unusable_types_are_not_classes(self):
        for dev_type in [None, "", 5, "Unknown"]:
            with self.subTest(dev_type=dev_type):
                self.assertIsNone(device_class_for_type(dev_type))


class TestNameFallback(unittest.TestCase):
    """An entry without a usable stored type still builds from its name."""

    def test_unknown_type_falls_back_to_the_name(self):
        device = build_bluetti_device("Unknown", "AC3001234567890123")

        self.assertIsNotNone(device)
        self.assertEqual(type(device).__name__, "AC300")

    def test_handsfree_names_build_through_their_alias(self):
        """The normalised Handsfree names reach the device table.

        A Handsfree advertises "Handsfree 1<serial>", which stores as
        "H1<serial>". The library only reads that back from v1.0.12-beta.4.
        """
        for advertised, device_class in [
            ("Handsfree 112345678", "Handsfree1"),
            ("Handsfree 212345678", "Handsfree2"),
        ]:
            with self.subTest(advertised=advertised):
                name = entry_name(advertised)
                device = build_bluetti_device(None, name)

                self.assertIsNotNone(device)
                self.assertEqual(type(device).__name__, device_class)

    def test_nothing_recognised_builds_nothing(self):
        self.assertIsNone(build_bluetti_device("Unknown", "PBOX12345678"))
        self.assertIsNone(build_bluetti_device(None, None))


if __name__ == "__main__":
    unittest.main()
