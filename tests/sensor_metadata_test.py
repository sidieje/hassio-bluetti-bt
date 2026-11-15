"""Tests that a sensor's classification does not depend on it having a unit.

Unit of measurement, device class and state class are three independent
properties of a Home Assistant sensor. The station-field loop in
``async_setup_entry`` used to build the entity inside ``if unit is not None``
and drop ``device_class`` and ``state_class`` in the ``else`` branch, so a
unitless field that is still classifiable would silently lose its metadata.

No field in the library is affected today -- every field with a device class or
state class also has a unit -- so there is no reading whose behaviour can pin
this. The per-pack entities have always passed all three unconditionally, which
is the shape the station fields now match. These tests keep it that way: one
asserts the entity model itself, the other that the construction site did not
go back to branching on the unit.
"""

import inspect
import re
import unittest
from unittest.mock import Mock

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.helpers.entity import DeviceInfo

from custom_components.bluetti_bt import sensor as sensor_module
from custom_components.bluetti_bt.const import DOMAIN
from custom_components.bluetti_bt.sensor import BluettiSensor


def make_sensor(**kwargs) -> BluettiSensor:
    """Create a sensor entity without initializing Home Assistant."""
    device_info = DeviceInfo(
        identifiers={(DOMAIN, "AA:BB:CC:DD:EE:FF")},
        name="AC300",
    )

    return BluettiSensor(Mock(), device_info, 99, "battery_soc", **kwargs)


class TestSensorMetadata(unittest.TestCase):
    """A device class or state class is not conditional on a unit."""

    def test_unitless_sensor_keeps_its_device_and_state_class(self):
        sensor = make_sensor(
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
        )

        self.assertIsNone(sensor.native_unit_of_measurement)
        self.assertEqual(sensor.device_class, SensorDeviceClass.POWER)
        self.assertEqual(sensor.state_class, SensorStateClass.MEASUREMENT)

    def test_classified_sensor_does_not_need_a_unit_to_be_classified(self):
        """The three properties are set independently of one another."""
        unitless = make_sensor(device_class=SensorDeviceClass.BATTERY)
        unit_only = make_sensor(unit_of_measurement="V")

        self.assertEqual(unitless.device_class, SensorDeviceClass.BATTERY)
        self.assertIsNone(unitless.state_class)
        self.assertIsNone(unitless.native_unit_of_measurement)
        self.assertEqual(unit_only.native_unit_of_measurement, "V")
        self.assertIsNone(unit_only.device_class)


class TestStationFieldConstruction(unittest.TestCase):
    """The station-field loop must pass all three properties through."""

    def station_fields_source(self) -> str:
        """The part of ``async_setup_entry`` that builds the station entities."""
        source = inspect.getsource(sensor_module.async_setup_entry)
        # Drop the docstring so its wording cannot satisfy the search.
        source = re.sub(r'^\s*""".*?"""\s*', "", source, count=1, flags=re.DOTALL)
        # Stop where the per-pack loop starts; it has always been correct and
        # is covered by sensor_pack_test.py.
        pack_marker = source.find("# Pack fields")

        self.assertGreaterEqual(
            pack_marker,
            0,
            "the per-pack loop no longer starts with the comment this test "
            "uses to find the end of the station-field loop",
        )

        return source[:pack_marker]

    def test_metadata_is_not_built_inside_a_unit_check(self):
        body = self.station_fields_source()

        self.assertNotIn(
            "if unit is not None",
            body,
            "the station-field loop gates entity construction on the unit "
            "again, which drops device_class and state_class for any unitless "
            "field that has them",
        )

    def test_all_three_properties_are_passed_to_the_entity(self):
        body = self.station_fields_source()

        for expected in (
            "unit_of_measurement=unit",
            "device_class=device_class",
            "state_class=state_class",
        ):
            with self.subTest(expected=expected):
                self.assertIn(
                    expected,
                    body,
                    "the station-field loop no longer passes the metadata "
                    "looked up for each field through to the entity",
                )


if __name__ == "__main__":
    unittest.main()
