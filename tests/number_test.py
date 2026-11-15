"""Tests for the writable number entities (AC300 SOC charge range).

Covers both directions: writing a value to the device, and turning a polled
value into a state. availability_test.py drives the shared availability debounce
through unbound methods, but nothing exercised _handle_coordinator_update on this
platform: the six guards that decide whether a reading is published at all ran
untested.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bluetti_bt_lib import FieldName
from bluetti_bt_lib.fields import NumberField
from homeassistant.exceptions import ServiceValidationError

from custom_components.bluetti_bt.number import BluettiNumber

RESPONSE_KEY = "soc_range_start"
UNIQUE_ID = "AABBCCDDEEFF_soc_range_start"
LOGABLE_ID = "AABBCCXXXXXXXXXXsoc_range_start"


def soc_range_field(name: FieldName) -> NumberField:
    """The SOC range register as the library defines it for the AC300."""
    address = 3015 if name is FieldName.BATTERY_SOC_RANGE_START else 3016
    return NumberField(name, address, 1, 5, 100, 1)


class TestBluettiNumberWrite(unittest.IsolatedAsyncioTestCase):
    """Test number writes, range checks and SOC range ordering."""

    def make_number(
        self,
        name: FieldName = FieldName.BATTERY_SOC_RANGE_START,
        result=None,
        data: dict | None = None,
    ):
        """Create a number entity without initializing Home Assistant."""
        number = object.__new__(BluettiNumber)
        number._logger = Mock()
        number._address = "AA:BB:CC:DD:EE:FF"
        number._field = soc_range_field(name)
        number._response_key = number._field.name
        number.coordinator = Mock()
        number.coordinator.data = {"soc_range_start": 30, "soc_range_end": 80}
        if data is not None:
            number.coordinator.data = data
        number.coordinator.async_write_verified = AsyncMock(return_value=result)
        return number

    async def test_value_written_as_int(self):
        """The slider hands over a float; the register takes an int."""
        number = self.make_number(
            result=SimpleNamespace(
                write_succeeded=True,
                readback_succeeded=True,
                verified=True,
                values={"soc_range_start": 40},
            )
        )

        await number.async_set_native_value(40.0)

        number.coordinator.async_write_verified.assert_awaited_once_with(
            "soc_range_start", 40
        )

    async def test_out_of_range_value_is_not_written(self):
        number = self.make_number()

        with self.assertRaises(ServiceValidationError) as raised:
            await number.async_set_native_value(101)

        number.coordinator.async_write_verified.assert_not_awaited()
        number._logger.warning.assert_called_once()
        self.assertIn("from 5 to 100", str(raised.exception))

    async def test_below_the_app_floor_is_not_written(self):
        """The app will not take a UPS threshold under 5%."""
        number = self.make_number()

        with self.assertRaises(ServiceValidationError) as raised:
            await number.async_set_native_value(4)

        number.coordinator.async_write_verified.assert_not_awaited()
        self.assertIn("from 5 to 100", str(raised.exception))

    async def test_whole_unit_value_is_written(self):
        """The sliders move in single units, so any whole value is accepted."""
        number = self.make_number(
            result=SimpleNamespace(
                write_succeeded=True,
                readback_succeeded=True,
                verified=True,
                values={"soc_range_start": 32},
            )
        )

        await number.async_set_native_value(32)

        number.coordinator.async_write_verified.assert_awaited_once_with(
            "soc_range_start", 32
        )
        number._logger.warning.assert_not_called()

    async def test_soc_range_start_must_stay_below_end(self):
        number = self.make_number()

        with self.assertRaises(ServiceValidationError) as raised:
            await number.async_set_native_value(80)

        number.coordinator.async_write_verified.assert_not_awaited()
        number._logger.warning.assert_called_once_with(
            "Refusing %s=%s: it must stay %s %s (%s)",
            "soc_range_start",
            80,
            "below",
            "soc_range_end",
            80,
        )
        self.assertEqual(
            str(raised.exception),
            "soc_range_start must stay below soc_range_end (80)",
        )

    async def test_soc_range_end_must_stay_above_start(self):
        number = self.make_number(name=FieldName.BATTERY_SOC_RANGE_END)

        with self.assertRaises(ServiceValidationError):
            await number.async_set_native_value(30)

        number.coordinator.async_write_verified.assert_not_awaited()
        number._logger.warning.assert_called_once()

    async def test_ordering_skipped_when_counterpart_unknown(self):
        number = self.make_number(
            name=FieldName.BATTERY_SOC_RANGE_END,
            data={},
            result=SimpleNamespace(
                write_succeeded=True,
                readback_succeeded=True,
                verified=True,
                values={"soc_range_end": 90},
            ),
        )

        await number.async_set_native_value(90)

        number.coordinator.async_write_verified.assert_awaited_once_with(
            "soc_range_end", 90
        )

    async def test_value_mismatch_raises(self):
        number = self.make_number(
            result=SimpleNamespace(
                write_succeeded=True,
                readback_succeeded=True,
                verified=False,
                values={"soc_range_start": 30},
            )
        )

        with self.assertRaises(ServiceValidationError) as raised:
            await number.write_to_device(40)

        number._logger.warning.assert_called_once_with(
            "Write value mismatch for %s: requested %s; device reports %s",
            "soc_range_start",
            40,
            30,
        )
        self.assertIn("soc_range_start=30, not 40", str(raised.exception))

    async def test_rejected_write_raises(self):
        number = self.make_number(
            result=SimpleNamespace(
                write_succeeded=False,
                readback_succeeded=False,
                verified=False,
                values=None,
            )
        )

        with self.assertRaises(ServiceValidationError):
            await number.write_to_device(40)

        number._logger.error.assert_called_once_with(
            "Write failed for %s; device state was not confirmed",
            "soc_range_start",
        )

    async def test_readback_failure_raises(self):
        number = self.make_number(
            result=SimpleNamespace(
                write_succeeded=True,
                readback_succeeded=False,
                verified=False,
                values=None,
            )
        )

        with self.assertRaises(ServiceValidationError):
            await number.write_to_device(40)

        number._logger.warning.assert_called_once_with(
            "Write sent for %s, but read-back failed; not retrying",
            "soc_range_start",
        )


class TestBluettiNumberUpdate(unittest.TestCase):
    """Test how a number entity turns a coordinator cycle into a state."""

    def make_number(self, data, last_update_success: bool = True):
        """Create a number entity without initializing Home Assistant."""
        number = object.__new__(BluettiNumber)
        number._logger = Mock()
        number._response_key = RESPONSE_KEY
        number._field = soc_range_field(FieldName.BATTERY_SOC_RANGE_START)
        number._attr_unique_id = UNIQUE_ID
        number._attr_native_value = None
        number._attr_available = True
        number._attr_extra_state_attributes = {}
        number._unavailable_counter = 0
        number.coordinator = SimpleNamespace(
            data=data,
            last_update_success=last_update_success,
            consecutive_failures=0,
        )
        # Record what each write would have published, so a value that is only
        # set after the last write is caught rather than trusted.
        number.writes: list[int | None] = []
        number.async_write_ha_state = Mock(
            side_effect=lambda: number.writes.append(number._attr_native_value)
        )
        return number

    def assert_unavailable_reported(self, number, cause: str) -> None:
        """A rejected cycle should report the cause and never publish a state."""
        self.assertEqual(
            number._attr_extra_state_attributes.get("unavailable_cause"), cause
        )
        number.async_write_ha_state.assert_called_once_with()
        self.assertEqual(number.writes, [None])

    def test_failed_poll_is_not_read_as_fresh_data(self):
        """A failed cycle must not publish, even though data still holds the last good dict."""
        number = self.make_number({RESPONSE_KEY: 80}, last_update_success=False)

        number._handle_coordinator_update()

        self.assert_unavailable_reported(number, "Polling failed")

    def test_none_data_reports_data_is_none(self):
        """No dict at all is a distinct cause from an empty one."""
        number = self.make_number(None)

        number._handle_coordinator_update()

        self.assert_unavailable_reported(number, "Data is None")
        number._logger.debug.assert_called_with("Data from coordinator is None")

    def test_non_dict_data_reports_invalid_data(self):
        """Coordinator data that is not a mapping cannot be indexed by key."""
        number = self.make_number("not a dict")

        number._handle_coordinator_update()

        self.assert_unavailable_reported(number, "Invalid data")

    def test_missing_key_reports_no_data(self):
        """A station that answered without this register has no value to publish."""
        number = self.make_number({"other_field": 80})

        number._handle_coordinator_update()

        self.assert_unavailable_reported(number, "No data")

    def test_bool_is_rejected_even_though_it_is_an_int(self):
        """True would pass an int check, but a flag is not a setpoint."""
        number = self.make_number({RESPONSE_KEY: True})

        number._handle_coordinator_update()

        self.assert_unavailable_reported(number, "Invalid data type")
        number._logger.warning.assert_called_once_with(
            "Invalid response data type from coordinator (number.%s): %s",
            LOGABLE_ID,
            True,
        )

    def test_float_is_rejected(self):
        """Documents current behaviour: only int is accepted on this platform.

        Every NumberField the library exposes for these devices is a whole
        register, so this has not been seen in practice. If a fractional
        register is ever added, this test is the one to change.
        """
        number = self.make_number({RESPONSE_KEY: 80.5})

        number._handle_coordinator_update()

        self.assert_unavailable_reported(number, "Invalid data type")

    def test_int_reading_is_published(self):
        number = self.make_number({RESPONSE_KEY: 80})

        number._handle_coordinator_update()

        self.assertEqual(number._attr_native_value, 80)
        self.assertTrue(number._attr_available)
        self.assertEqual(number._unavailable_counter, 0)
        self.assertEqual(number._attr_extra_state_attributes, {})
        # _set_available writes once before the value is known and the update
        # writes again after it, so the last write is what Home Assistant keeps.
        self.assertEqual(number.writes, [None, 80])

    def test_zero_is_a_real_value_not_missing_data(self):
        """A setpoint of 0 is falsy, so it must not be treated as an absent reading."""
        number = self.make_number({RESPONSE_KEY: 0})

        number._handle_coordinator_update()

        self.assertEqual(number._attr_native_value, 0)
        self.assertTrue(number._attr_available)
        self.assertNotIn("unavailable_cause", number._attr_extra_state_attributes)

    def test_a_good_cycle_clears_a_previous_outage(self):
        """The counter from an outage must not keep the entity unavailable."""
        number = self.make_number({RESPONSE_KEY: 80})
        number._unavailable_counter = 3
        number._attr_extra_state_attributes = {"unavailable_cause": "No data"}

        number._handle_coordinator_update()

        self.assertEqual(number._unavailable_counter, 0)
        self.assertEqual(number._attr_extra_state_attributes, {})
        self.assertTrue(number.available)


if __name__ == "__main__":
    unittest.main()
