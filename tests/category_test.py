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

    def test_identity_fields_are_diagnostic(self):
        """Serial number, model, WiFi name and firmware versions are support
        detail, and the same fields on every power station."""
        for name in (
            FieldName.DEVICE_SN,
            FieldName.DEVICE_TYPE,
            FieldName.WIFI_NAME,
            FieldName.VER_BMS,
            FieldName.VER_ARM,
            FieldName.VER_DSP,
        ):
            self.assertEqual(
                get_category(name), EntityCategory.DIAGNOSTIC, f"{name}"
            )

    def test_split_phase_is_diagnostic(self):
        """Split phase wiring is an install-time setting, not a control."""
        for name in (FieldName.CTRL_SPLIT_PHASE, FieldName.CTRL_SPLIT_PHASE_MODE):
            self.assertEqual(get_category(name), EntityCategory.DIAGNOSTIC, f"{name}")

    def test_eco_and_power_lifting_toggles_are_controls(self):
        """Toggles the user flips day to day sit under Controls."""
        for name in (
            FieldName.CTRL_ECO,
            FieldName.CTRL_ECO_AC,
            FieldName.CTRL_ECO_DC,
            FieldName.CTRL_POWER_LIFTING,
        ):
            self.assertEqual(get_category(name), None, f"{name} belongs in Controls")

    def test_eco_thresholds_are_diagnostic(self):
        """The eco thresholds are read-only, so they are not settings."""
        for name in (FieldName.CTRL_ECO_MIN_POWER_AC, FieldName.CTRL_ECO_MIN_POWER_DC):
            self.assertEqual(get_category(name), EntityCategory.DIAGNOSTIC, f"{name}")

    def test_pack_count_is_diagnostic(self):
        """The pack count register describes the installed hardware, so it is
        support detail rather than something the user tunes."""
        self.assertEqual(get_category(FieldName.PACK_NUM), EntityCategory.DIAGNOSTIC)

    def test_eco_timer_selects_stay_in_configuration(self):
        for name in (
            FieldName.CTRL_ECO_TIME_MODE_AC,
            FieldName.CTRL_ECO_TIME_MODE_DC,
        ):
            self.assertEqual(get_category(name), EntityCategory.CONFIG, f"{name}")


if __name__ == "__main__":
    unittest.main()
