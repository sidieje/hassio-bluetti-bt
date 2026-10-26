import fnmatch
import json
from typing import List
import unittest

from bluetti_bt_lib.devices import DEVICES

# Home Assistant indexes a local name matcher by its first three characters and
# refuses one that has a wildcard or a character class inside that window,
# because it would then be checked against every advertisement it sees. A
# matcher that passes this file but not that rule kills the bluetooth
# integration, and every integration that depends on it, at setup.
# See homeassistant/components/bluetooth/match.py::_local_name_to_index_key.
LOCAL_NAME_MIN_MATCH_LENGTH = 3


class TestBTNames(unittest.TestCase):
    def setUp(self):
        with open("./custom_components/bluetti_bt/manifest.json") as f:
            self.manifest = json.load(f)

        self.device_names: List[str] = []
        for name in DEVICES.keys():
            self.device_names.append(name)

    def local_name_matchers(self) -> List[str]:
        matchers: List[str] = []

        for entry in self.manifest["bluetooth"]:
            local_name = entry.get("local_name")
            if isinstance(local_name, str):
                matchers.append(local_name)

        return matchers

    def test_bt_matcher_length(self):
        for local_name in self.local_name_matchers():
            head = local_name[:LOCAL_NAME_MIN_MATCH_LENGTH]

            self.assertNotIn(
                "*",
                head,
                f"Home Assistant would reject {local_name}: a wildcard in the "
                f"first {LOCAL_NAME_MIN_MATCH_LENGTH} characters matches too "
                "broadly to be indexed",
            )
            self.assertNotIn(
                "[",
                head,
                f"Home Assistant would reject {local_name}: a character class "
                f"in the first {LOCAL_NAME_MIN_MATCH_LENGTH} characters matches "
                "too broadly to be indexed",
            )

    def test_device_names(self):
        matchers = self.local_name_matchers()

        for name in self.device_names:
            # A station advertises its type followed by its serial number, and a
            # matcher needs three literal characters before its wildcard, so a
            # two letter type like the FridgePower's "FP" is covered by FP0*
            # through FP9* rather than by a single FP* entry.
            advertised = [f"{name}{digit}000000000000" for digit in "0123456789"]

            found = [
                matcher
                for matcher in matchers
                if any(
                    fnmatch.fnmatchcase(sample.lower(), matcher.lower())
                    for sample in advertised
                )
            ]
            self.assertGreaterEqual(
                len(found), 1, f"Missing BT matcher for device: {name}"
            )
