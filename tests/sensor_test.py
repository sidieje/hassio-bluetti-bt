"""Tests for the state update path of the Bluetti sensor.

availability_test.py drives the shared availability debounce through unbound
methods, but nothing exercised _handle_coordinator_update on this platform: the
six guards that decide whether a reading is published at all ran untested, on
the platform that carries most of the entities. These cover each guard and the
two kinds of accepted reading, numeric and enum.

The incidental debug lines are deliberately not pinned here, so that aligning
the log messages across the five platforms stays a one-file change per platform
rather than a test change as well.

complex is in the accepted-type list but is not exercised. No field parser in the
library produces one, and a complex that did arrive would not survive Home
Assistant: a sensor carrying a unit, state class or device class raises there for
a non-numeric value, and one carrying none of them publishes "(1+2j)" as its
state. A passing test would make that whitelist entry look deliberate, so it
stays uncovered until finding L6 removes it.
"""

import enum
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.bluetti_bt.sensor import BluettiSensor

RESPONSE_KEY = "battery_soc"
UNIQUE_ID = "AABBCCDDEEFF_battery_soc"
LOGABLE_ID = "AABBCCXXXXXXXXXXbattery_soc"


class Colour(enum.Enum):
    """Stand-in for the library's enum-valued fields."""

    GREEN = 1
    GREY = 2


class TestBluettiSensorUpdate(unittest.TestCase):
    """Test how a sensor turns a coordinator cycle into a reading."""

    def make_sensor(self, data, last_update_success: bool = True):
        """Create a sensor without initializing Home Assistant."""
        sensor = object.__new__(BluettiSensor)
        sensor._logger = Mock()
        sensor._response_key = RESPONSE_KEY
        sensor._attr_unique_id = UNIQUE_ID
        sensor._attr_native_value = None
        sensor._attr_available = True
        sensor._attr_extra_state_attributes = {}
        sensor._unavailable_counter = 0
        sensor.coordinator = SimpleNamespace(
            data=data,
            last_update_success=last_update_success,
            consecutive_failures=0,
        )
        # Record what each write would have published, so a value that is only
        # set after the last write is caught rather than trusted.
        sensor.writes: list[object] = []
        sensor.async_write_ha_state = Mock(
            side_effect=lambda: sensor.writes.append(sensor._attr_native_value)
        )
        return sensor

    def assert_unavailable_reported(self, sensor, cause: str) -> None:
        """A rejected cycle should report the cause and never publish a value."""
        self.assertEqual(
            sensor._attr_extra_state_attributes.get("unavailable_cause"), cause
        )
        sensor.async_write_ha_state.assert_called_once_with()
        self.assertEqual(sensor.writes, [None])

    def test_failed_poll_is_not_read_as_fresh_data(self):
        """A failed cycle must not publish, even though data still holds the last good dict."""
        sensor = self.make_sensor({RESPONSE_KEY: 88}, last_update_success=False)

        sensor._handle_coordinator_update()

        self.assert_unavailable_reported(sensor, "Polling failed")

    def test_none_data_reports_data_is_none(self):
        """No dict at all is a distinct cause from an empty one."""
        sensor = self.make_sensor(None)

        sensor._handle_coordinator_update()

        self.assert_unavailable_reported(sensor, "Data is None")
        sensor._logger.debug.assert_called_with("Data from coordinator is None")

    def test_non_dict_data_reports_invalid_data(self):
        """Coordinator data that is not a mapping cannot be indexed by key."""
        sensor = self.make_sensor("not a dict")

        sensor._handle_coordinator_update()

        self.assert_unavailable_reported(sensor, "Invalid data")

    def test_missing_key_reports_no_data(self):
        """A station that answered without this field has no reading to publish."""
        sensor = self.make_sensor({"other_field": 1})

        sensor._handle_coordinator_update()

        self.assert_unavailable_reported(sensor, "No data")

    def test_unreadable_value_names_the_type_it_got(self):
        """The type is the useful part of an invalid reading, so it is logged."""
        sensor = self.make_sensor({RESPONSE_KEY: [1, 2]})

        sensor._handle_coordinator_update()

        self.assert_unavailable_reported(sensor, "Invalid data type")
        sensor._logger.warning.assert_called_once_with(
            "Invalid response data type from coordinator (sensor.%s): %s has type %s",
            LOGABLE_ID,
            [1, 2],
            list,
        )

    def test_int_reading_is_published(self):
        sensor = self.make_sensor({RESPONSE_KEY: 42})

        sensor._handle_coordinator_update()

        self.assertEqual(sensor._attr_native_value, 42)
        self.assertEqual(sensor.writes, [None, 42])

    def test_float_reading_is_published_untouched(self):
        """A cell voltage is a float; rounding it here would lose the reading."""
        sensor = self.make_sensor({RESPONSE_KEY: 3.42})

        sensor._handle_coordinator_update()

        self.assertEqual(sensor._attr_native_value, 3.42)

    def test_decimal_reading_is_published_untouched(self):
        sensor = self.make_sensor({RESPONSE_KEY: Decimal("3.42")})

        sensor._handle_coordinator_update()

        self.assertEqual(sensor._attr_native_value, Decimal("3.42"))

    def test_str_reading_is_published(self):
        """Some fields report text, such as a firmware version."""
        sensor = self.make_sensor({RESPONSE_KEY: "1.2.3"})

        sensor._handle_coordinator_update()

        self.assertEqual(sensor._attr_native_value, "1.2.3")

    def test_enum_reading_is_published_by_name(self):
        """An enum publishes its name, because Home Assistant states are text."""
        sensor = self.make_sensor({RESPONSE_KEY: Colour.GREY})

        sensor._handle_coordinator_update()

        self.assertEqual(sensor._attr_native_value, "GREY")
        self.assertEqual(sensor.writes, [None, "GREY"])

    def test_zero_is_a_reading_and_not_a_missing_value(self):
        """A power of 0 W is real, so it must not fall into the no-data branch."""
        sensor = self.make_sensor({RESPONSE_KEY: 0})

        sensor._handle_coordinator_update()

        self.assertEqual(sensor._attr_native_value, 0)
        self.assertTrue(sensor._attr_available)
        self.assertNotIn("unavailable_cause", sensor._attr_extra_state_attributes)

    def test_a_good_cycle_clears_a_previous_outage(self):
        """The counter from an outage must not keep the entity unavailable."""
        sensor = self.make_sensor({RESPONSE_KEY: 12})
        sensor._unavailable_counter = 3
        sensor._attr_extra_state_attributes = {"unavailable_cause": "No data"}

        sensor._handle_coordinator_update()

        self.assertEqual(sensor._unavailable_counter, 0)
        self.assertEqual(sensor._attr_extra_state_attributes, {})
        self.assertTrue(sensor.available)


if __name__ == "__main__":
    unittest.main()
