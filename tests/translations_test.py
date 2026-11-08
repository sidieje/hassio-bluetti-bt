"""Guards that every shipped translation stays complete and in step with English.

The integration ships three translation files that are maintained by hand. They
drifted before (pt-BR fell 62 keys behind, de lost the exception messages)
because nothing compared them to each other, so a key added to en.json could
ship without ever reaching a user in another language.

These tests replace the two near-identical per-language files that only checked
en and de against the device fields and never looked at pt-BR at all.
"""

import json
import re
import unittest
from pathlib import Path
from typing import Dict, List, Set

from bluetti_bt_lib.base_devices import BluettiDevice
from bluetti_bt_lib.devices import DEVICES
from bluetti_bt_lib.fields import NumberField

from custom_components.bluetti_bt.types.OptionalDeviceConfig import (
    ABORT_REASON_INTERVAL,
    ABORT_REASON_PACK_INTERVAL,
    ABORT_REASON_RETRIES,
    ABORT_REASON_TIMEOUT,
)

TRANSLATION_DIR = Path("custom_components/bluetti_bt/translations")
INTEGRATION_DIR = Path("custom_components/bluetti_bt")

# en.json is the reference: every other language must carry exactly its keys.
REFERENCE_LANGUAGE = "en"
LANGUAGES = ("en", "de", "pt-BR")

# English strings are kept ASCII: the other two languages are full of accents,
# so accented text showing up here means a value was pasted from one of them.
# This is a tripwire only -- it cannot catch a leaked string that happens to be
# ASCII, so wording still needs a reviewer. Anything genuinely needed in an
# English name goes in here.
ALLOWED_NON_ASCII_IN_REFERENCE = set("°")

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
EXCEPTION_KEY_RE = re.compile(r'^\s*translation_key="([a-z_]+)"', re.MULTILINE)


def load_translation(language: str) -> dict:
    with open(
        TRANSLATION_DIR / f"{language}.json", encoding="utf-8"
    ) as translation_file:
        return json.load(translation_file)


def flatten(nested: dict, prefix: str = "") -> Dict[str, str]:
    """Collapse a translation file to dotted leaf keys, ignoring formatting."""
    flat: Dict[str, str] = {}
    for key, value in nested.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten(value, dotted))
        else:
            flat[dotted] = value
    return flat


def entity_keys_by_category(translation: dict) -> Dict[str, Set[str]]:
    entity = translation.get("entity", {})
    return {
        category: set(entity.get(category, {}))
        for category in ("sensor", "binary_sensor", "select", "switch", "number")
    }


def device_field_names_by_category() -> Dict[str, Set[str]]:
    """Collect the translation keys the library can make the platforms use."""
    expected = {
        category: set()
        for category in ("sensor", "binary_sensor", "select", "switch", "number")
    }

    devices: List[BluettiDevice] = []
    for Device in DEVICES.values():
        device = Device()
        if isinstance(device, BluettiDevice):
            devices.append(device)

    for device in devices:
        for field in device.get_sensor_fields() + device.get_select_fields():
            expected["sensor"].add(field.name)
        for field in device.get_bool_fields() + device.get_switch_fields():
            expected["binary_sensor"].add(field.name)
        for field in device.get_select_fields():
            expected["select"].add(field.name)
        for field in device.get_switch_fields():
            expected["switch"].add(field.name)
        for field in device.fields:
            if isinstance(field, NumberField):
                expected["number"].add(field.name)

    return expected


def exception_translation_keys() -> Set[str]:
    """Every translation_key the integration raises on an exception."""
    keys: Set[str] = set()
    for source in INTEGRATION_DIR.rglob("*.py"):
        keys.update(EXCEPTION_KEY_RE.findall(source.read_text(encoding="utf-8")))
    return keys


class TestTranslationsCoverDeviceFields(unittest.TestCase):
    """Each language must name every entity the library can create."""

    def setUp(self):
        self.expected = device_field_names_by_category()

    def test_every_language_covers_device_fields(self):
        for language in LANGUAGES:
            actual = entity_keys_by_category(load_translation(language))
            for category, names in self.expected.items():
                for name in sorted(names):
                    with self.subTest(language=language, category=category, name=name):
                        self.assertIn(
                            name,
                            actual[category],
                            f"Missing translation for {category}: {name}",
                        )


class TestTranslationsMatchReference(unittest.TestCase):
    """Non-English files must carry exactly the reference key set."""

    def setUp(self):
        self.reference = flatten(load_translation(REFERENCE_LANGUAGE))

    def test_no_language_has_missing_or_extra_keys(self):
        for language in LANGUAGES:
            with self.subTest(language=language):
                local = flatten(load_translation(language))
                missing = sorted(set(self.reference) - set(local))
                extra = sorted(set(local) - set(self.reference))
                self.assertEqual(
                    missing,
                    [],
                    f"{language}.json is missing keys present in "
                    f"{REFERENCE_LANGUAGE}.json",
                )
                self.assertEqual(
                    extra,
                    [],
                    f"{language}.json has keys that no longer exist in "
                    f"{REFERENCE_LANGUAGE}.json",
                )

    def test_placeholders_match_the_reference(self):
        for language in LANGUAGES:
            local = flatten(load_translation(language))
            for key, reference_value in self.reference.items():
                if key not in local:
                    continue
                with self.subTest(language=language, key=key):
                    self.assertEqual(
                        set(PLACEHOLDER_RE.findall(reference_value)),
                        set(PLACEHOLDER_RE.findall(local[key])),
                        f"{language}.json placeholder set differs for {key}",
                    )

    def test_no_localised_value_is_empty(self):
        for language in LANGUAGES:
            for key, value in flatten(load_translation(language)).items():
                with self.subTest(language=language, key=key):
                    self.assertNotEqual(value.strip(), "")

    def test_reference_language_stays_ascii(self):
        offenders = {
            key: value
            for key, value in self.reference.items()
            if any(
                ord(char) > 127 and char not in ALLOWED_NON_ASCII_IN_REFERENCE
                for char in value
            )
        }
        self.assertEqual(
            offenders,
            {},
            f"{REFERENCE_LANGUAGE}.json contains non-ASCII text; English values "
            "are kept ASCII so that pasted translations are caught",
        )


class TestAbortReasonsAreTranslated(unittest.TestCase):
    """The options flow aborts, so abort reasons live under options.abort."""

    def test_every_abort_reason_has_a_message(self):
        reasons = {
            ABORT_REASON_INTERVAL,
            ABORT_REASON_TIMEOUT,
            ABORT_REASON_RETRIES,
            ABORT_REASON_PACK_INTERVAL,
        }
        for language in LANGUAGES:
            aborts = load_translation(language)["options"]["abort"]
            for reason in sorted(reasons):
                with self.subTest(language=language, reason=reason):
                    self.assertIn(reason, aborts)


class TestExceptionMessagesAreTranslated(unittest.TestCase):
    """Raised exceptions fall back to their English text when untranslated."""

    def test_every_exception_key_has_a_message(self):
        keys = exception_translation_keys()
        self.assertTrue(keys, "no exception translation_keys found in the source")

        for language in LANGUAGES:
            exceptions = load_translation(language).get("exceptions", {})
            for key in sorted(keys):
                with self.subTest(language=language, key=key):
                    self.assertIn(key, exceptions)
                    self.assertIn("message", exceptions[key])


if __name__ == "__main__":
    unittest.main()
