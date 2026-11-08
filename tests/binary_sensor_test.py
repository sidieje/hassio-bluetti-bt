"""Tests for the state update path of the Bluetti binary sensor.

availability_test.py drives the shared availability debounce through unbound
methods, but nothing exercised _handle_coordinator_update on this platform: the
six guards that decide whether a reading is published at all ran untested.
These cover each guard and the two good-value outcomes, so the difference
between "no reading" and "a reading of False" cannot be lost.
"""

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.bluetti_bt.binary_sensor import BluettiBinarySensor

RESPONSE_KEY = "charging"
UNIQUE_ID = "AABBCCDDEEFF_charging"
LOGABLE_ID = "AABBCCXXXXXXXXXXcharging"


class TestBluettiBinarySensorUpdate(unittest.TestCase):
    """Test how a binary sensor turns a coordinator cycle into state."""

    def make_sensor(self, data, last_update_success: bool = True):
        """Create a binary sensor without initializing Home Assistant."""
        sensor = object.__new__(BluettiBinarySensor)
        sensor._logger = Mock()
        sensor._response_key = RESPONSE_KEY
        sensor._attr_unique_id = UNIQUE_ID
        sensor._attr_is_on = None
        sensor._attr_available = True
        sensor._attr_extra_state_attributes = {}
        sensor._unavailable_counter = 0
        sensor.coordinator = SimpleNamespace(
            data=data,
            last_update_success=last_update_success,
            consecutive_failures=0,
        )
        # Record what each write would have published, so a state that is only
        # set after the last write is caught rather than trusted.
        sensor.writes: list[bool | None] = []
        sensor.async_write_ha_state = Mock(
            side_effect=lambda: sensor.writes.append(sensor._attr_is_on)
        )
        return sensor

    def assert_unavailable_reported(self, sensor, cause: str) -> None:
        """A rejected cycle should report the cause and never publish a state."""
        self.assertEqual(
            sensor._attr_extra_state_attributes.get("unavailable_cause"), cause
        )
        sensor.async_write_ha_state.assert_called_once_with()
        self.assertEqual(sensor.writes, [None])

    def test_failed_poll_is_not_read_as_fresh_data(self):
        """A failed cycle must not publish, even though data still holds the last good dict."""
        sensor = self.make_sensor({RESPONSE_KEY: True}, last_update_success=False)

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
        sensor = self.make_sensor({"other_field": True})

        sensor._handle_coordinator_update()

        self.assert_unavailable_reported(sensor, "No data")

    def test_non_bool_value_is_rejected_not_coerced(self):
        """A register that answers 1 is not a bool, so it must not switch the sensor on."""
        sensor = self.make_sensor({RESPONSE_KEY: 1})

        sensor._handle_coordinator_update()

        self.assert_unavailable_reported(sensor, "Invalid data type")
        sensor._logger.warning.assert_called_once_with(
            "Invalid response data type from coordinator (binary_sensor.%s): %s",
            LOGABLE_ID,
            1,
        )

    def test_true_reading_turns_the_sensor_on(self):
        """A bool reading should make the entity available and on."""
        sensor = self.make_sensor({RESPONSE_KEY: True})

        sensor._handle_coordinator_update()

        self.assertIs(sensor._attr_is_on, True)
        self.assertTrue(sensor._attr_available)
        self.assertEqual(sensor._unavailable_counter, 0)
        self.assertEqual(sensor._attr_extra_state_attributes, {})
        # _set_available writes once before the value is known and the update
        # writes again after it, so the last write is what Home Assistant keeps.
        self.assertEqual(sensor.writes, [None, True])

    def test_false_reading_turns_the_sensor_off(self):
        """A reading of False is a real state, not a missing reading."""
        sensor = self.make_sensor({RESPONSE_KEY: False})

        sensor._handle_coordinator_update()

        self.assertIs(sensor._attr_is_on, False)
        self.assertTrue(sensor._attr_available)
        self.assertNotIn("unavailable_cause", sensor._attr_extra_state_attributes)
        self.assertEqual(sensor.writes, [None, False])

    def test_a_good_cycle_clears_a_previous_outage(self):
        """The counter from an outage must not keep the entity unavailable."""
        sensor = self.make_sensor({RESPONSE_KEY: True})
        sensor._unavailable_counter = 3
        sensor._attr_extra_state_attributes = {"unavailable_cause": "No data"}

        sensor._handle_coordinator_update()

        self.assertEqual(sensor._unavailable_counter, 0)
        self.assertEqual(sensor._attr_extra_state_attributes, {})
        self.assertTrue(sensor.available)


class TestBluettiBinarySensorAvailability(unittest.TestCase):
    """The entity counts failed cycles before it gives up on the station."""

    def make_sensor(self) -> BluettiBinarySensor:
        sensor = object.__new__(BluettiBinarySensor)
        sensor._logger = logging.getLogger("bluetti_bt.test")
        sensor._response_key = RESPONSE_KEY
        sensor._attr_unique_id = UNIQUE_ID
        sensor._attr_available = True
        sensor._attr_extra_state_attributes = {}
        sensor._unavailable_counter = 0
        sensor.coordinator = SimpleNamespace(
            data={}, last_update_success=True, consecutive_failures=0
        )
        sensor.async_write_ha_state = Mock()
        return sensor

    def test_entity_survives_a_single_failed_cycle(self):
        """One dropped poll is normal at 15 s intervals, so it is not worth an outage."""
        sensor = self.make_sensor()
        sensor.coordinator.consecutive_failures = 1

        sensor._handle_coordinator_update()

        self.assertTrue(sensor.available)


if __name__ == "__main__":
    unittest.main()
