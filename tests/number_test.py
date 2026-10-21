"""Tests for the writable number entities (AC300 SOC charge range)."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bluetti_bt_lib import FieldName
from bluetti_bt_lib.fields import NumberField
from homeassistant.exceptions import ServiceValidationError

from custom_components.bluetti_bt.number import BluettiNumber


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


if __name__ == "__main__":
    unittest.main()
