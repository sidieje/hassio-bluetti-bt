"""Tests for per-pack sensors being attached to the main device."""

import unittest
from unittest.mock import Mock

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.entity import DeviceInfo

from custom_components.bluetti_bt import get_unique_id
from custom_components.bluetti_bt.const import DOMAIN
from custom_components.bluetti_bt.sensor import BluettiSensor


class TestPackSensors(unittest.TestCase):
    """Per-pack readings live on the main device, not on pack devices."""

    def make_sensor(self, pack_num: int, cell_num: int | None = None, device_class=None):
        coordinator = Mock()
        main = DeviceInfo(
            identifiers={(DOMAIN, "AA:BB:CC:DD:EE:FF")},
            name="AC300",
        )

        return BluettiSensor(
            coordinator,
            main,
            99,
            "battery_soc",
            device_class=device_class,
            pack_num=pack_num,
            cell_num=cell_num,
        )

    def test_sensor_is_on_main_device(self):
        sensor = self.make_sensor(2)

        self.assertEqual(
            sensor.device_info["identifiers"],
            {(DOMAIN, "AA:BB:CC:DD:EE:FF")},
        )

    def test_response_key_is_pack_scoped(self):
        sensor = self.make_sensor(2)

        self.assertEqual(sensor._response_key, "pack_2_battery_soc")
        self.assertEqual(sensor._attr_translation_key, "pack_battery_soc")
        self.assertEqual(sensor._attr_translation_placeholders, {"pack_num": 2})

    def test_unique_id_keeps_the_pack_label(self):
        """The old pack-device name stays in the unique id so existing
        entities are reused instead of orphaned."""
        sensor = self.make_sensor(2)

        self.assertEqual(
            sensor._attr_unique_id,
            get_unique_id("AC300 Battery Pack 2 battery_soc"),
        )

    def test_pack_soc_does_not_take_the_battery_device_class(self):
        """Otherwise the device page battery badge shows pack 1 (sorted first
        by name) instead of the station's total SOC."""
        sensor = self.make_sensor(1, device_class=SensorDeviceClass.BATTERY)

        self.assertIsNone(sensor.device_class)

    def test_total_soc_keeps_the_battery_device_class(self):
        coordinator = Mock()
        main = DeviceInfo(
            identifiers={(DOMAIN, "AA:BB:CC:DD:EE:FF")},
            name="AC300",
        )

        sensor = BluettiSensor(
            coordinator,
            main,
            43,
            "total_battery_percent",
            device_class=SensorDeviceClass.BATTERY,
        )

        self.assertEqual(sensor.device_class, SensorDeviceClass.BATTERY)


if __name__ == "__main__":
    unittest.main()
