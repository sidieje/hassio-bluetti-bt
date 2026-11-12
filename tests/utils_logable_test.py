"""Tests for the helpers that keep identifiers out of the logs.

Both run on the paths that report a problem, so a helper that raises while
masking replaces the message that was being written with a traceback.
"""

import unittest

from custom_components.bluetti_bt import get_unique_id
from custom_components.bluetti_bt.utils import mac_loggable, unique_id_logable

# What the config flow stores as the entry title: the advertised name with the
# separators stripped, so the type and the serial run together.
TITLE = "AC300AABBCCDDEEFF"


def entity_unique_id(response_key: str) -> str:
    """The unique id an entity of this station ends up with."""
    return get_unique_id(f"{TITLE} {response_key}")


class TestUniqueIdLogable(unittest.TestCase):
    """Test the unique id that ends up in a log line."""

    def test_device_serial_is_masked_and_field_kept(self):
        """The serial is the part to hide, the field is the part that tells two
        lines apart."""
        self.assertEqual(
            unique_id_logable(entity_unique_id("ac_voltage")),
            "ac300aXXXXXXXXXXac_voltage",
        )

    def test_serial_never_survives(self):
        for response_key in ("ac_voltage", "battery_soc", "ac_output_power"):
            with self.subTest(response_key=response_key):
                logable = unique_id_logable(entity_unique_id(response_key))

                self.assertNotIn("aabbccddeeff", logable)
                self.assertNotIn("bbccddee", logable)

    def test_pack_ids_keep_their_field(self):
        """A pack entity carries the pack label in its id, and only the first
        separator is the boundary between the device and the field."""
        unique_id = get_unique_id(f"{TITLE} Battery Pack 2 pack_2_soc")

        self.assertEqual(
            unique_id_logable(unique_id),
            "ac300aXXXXXXXXXXbattery_pack_2_pack_2_soc",
        )

    def test_id_without_a_separator_is_still_masked(self):
        """An id the entity was not built with can still reach a log line."""
        for unique_id in ("", "noseparator", TITLE, "AC300 AABB CC DD EE FF"):
            with self.subTest(unique_id=unique_id):
                logable = unique_id_logable(unique_id)

                self.assertEqual(logable, unique_id[:6] + "XXXXXXXXXX")
                self.assertNotIn("aabbccddeeff", logable.lower())

    def test_shorter_than_the_prefix_is_still_masked(self):
        for unique_id in ("x", "ab_cd"):
            with self.subTest(unique_id=unique_id):
                self.assertIn("XXXXXXXXXX", unique_id_logable(unique_id))


class TestMacLoggable(unittest.TestCase):
    """Test the address that ends up in a logger name."""

    def test_only_the_last_octet_survives(self):
        self.assertEqual(mac_loggable("AA:BB:CC:DD:EE:FF"), "XX:XX:XX:XX:XX:FF")

    def test_shorter_addresses_keep_their_last_octet(self):
        self.assertEqual(mac_loggable("CC:DD:EE:FF"), "XX:XX:XX:XX:XX:FF")


if __name__ == "__main__":
    unittest.main()
