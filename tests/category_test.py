"""Tests for how entities are grouped on the Home Assistant device page.

Home Assistant puts uncategorized switch/number/select entities under
"Controls" and EntityCategory.CONFIG entities under "Configuration", so the
category of each writable field decides which section the user sees.
"""

import unittest

from homeassistant.const import EntityCategory

from bluetti_bt_lib import FieldName, build_device

from custom_components.bluetti_bt.types import get_category


class TestDevicePageGrouping(unittest.TestCase):
    """Controls should hold the power toggles, Configuration the UPS options."""

    def setUp(self):
        self.device = build_device("AC3001234")

    def writable_names(self) -> list[str]:
        return [field.name for field in self.device.fields if field.is_writeable()]

    def test_controls_only_hold_the_power_toggles(self):
        controls = sorted(
            name
            for name in self.writable_names()
            if get_category(FieldName(name)) is None
        )

        self.assertEqual(controls, ["ctrl_ac", "ctrl_dc"])

    def test_ups_options_are_configuration(self):
        for name in (
            "ctrl_grid_charge_on",
            "ctrl_time_control_on",
            "ctrl_ups_mode",
            "soc_range_start",
            "soc_range_end",
        ):
            self.assertEqual(
                get_category(FieldName(name)),
                EntityCategory.CONFIG,
                f"{name} belongs in the Configuration section",
            )

    def test_diagnostics_are_unchanged(self):
        self.assertEqual(
            get_category(FieldName.DEVICE_SN), EntityCategory.DIAGNOSTIC
        )


if __name__ == "__main__":
    unittest.main()
