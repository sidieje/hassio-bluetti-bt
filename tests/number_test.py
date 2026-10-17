"""Tests for the writable number entities (AC300 SOC charge range)."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bluetti_bt_lib import FieldName
from bluetti_bt_lib.fields import NumberField

from custom_components.bluetti_bt.number import BluettiNumber


def soc_range_field(name: FieldName) -> NumberField:
    """The SOC range register as the library defines it for the AC300."""
    return NumberField(name, 3015 if name is FieldName.BATTERY_SOC_RANGE_START else 3016, 1, 0, 100)


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

        await number.async_set_native_value(101)

        number.coordinator.async_write_verified.assert_not_awaited()
        number._logger.warning.assert_called_once()

    async def test_soc_range_start_must_stay_below_end(self):
        number = self.make_number()

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

    async def test_soc_range_end_must_stay_above_start(self):
        number = self.make_number(name=FieldName.BATTERY_SOC_RANGE_END)

        await number.async_set_native_value(30)

        number.coordinator.async_write_verified.assert_not_awaited()
        number._logger.warning.assert_called_once()

    async def test_ordering_skipped_when_counterpart_unknown(self):
        number = self.make_number(name=FieldName.BATTERY_SOC_RANGE_END, data={})

        await number.async_set_native_value(90)

        number.coordinator.async_write_verified.assert_awaited_once_with(
            "soc_range_end", 90
        )

    async def test_value_mismatch_warns(self):
        number = self.make_number(
            result=SimpleNamespace(
                write_succeeded=True,
                readback_succeeded=True,
                verified=False,
                values={"soc_range_start": 30},
            )
        )

        await number.write_to_device(40)

        number._logger.warning.assert_called_once_with(
            "Write value mismatch for %s: requested %s; device reports %s",
            "soc_range_start",
            40,
            30,
        )


if __name__ == "__main__":
    unittest.main()
