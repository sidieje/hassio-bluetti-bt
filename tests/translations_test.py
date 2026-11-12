"""Guards that the shipped translations match the entities that really exist.

The integration ships three translation files that are maintained by hand. They
drifted before (pt-BR fell 62 keys behind, de lost the exception messages)
because nothing compared them to each other, so a key added to en.json could
ship without ever reaching a user in another language.

Two directions are checked against the entity inventory:

* every entity the platforms create has a name in every language, and
* every entity name in a translation file belongs to an entity that can
  actually be created, unless it is deliberately allowlisted.

The inventory is not a list of library fields. It is built by running each
platform's real ``async_setup_entry`` for every model the library knows about,
and reading the translation key off the entities that come back. That keeps the
check honest about things a field list cannot see: fields the platforms skip,
the extra mirrors an encrypted station gets, and pack entities, which only
exist for models that can actually poll a pack.
"""

import importlib
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Set
from unittest.mock import Mock

from homeassistant.helpers import entity_registry as er

from bluetti_bt_lib.base_devices import BluettiDevice
from bluetti_bt_lib.devices import DEVICES

from custom_components.bluetti_bt.config_flow import has_battery_packs
from custom_components.bluetti_bt.const import DATA_COORDINATOR, DOMAIN
from custom_components.bluetti_bt.coordinator import PollingCoordinator
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

# The entity platforms, and the translation category each one writes into.
CATEGORIES = ("sensor", "binary_sensor", "select", "switch", "number")
PLATFORMS = {
    category: importlib.import_module(f"custom_components.bluetti_bt.{category}")
    for category in CATEGORIES
}

# Entity names that sit in a translation file without belonging to an entity
# the platforms create today. Every entry needs a reason to be there, and an
# entry that no longer applies has to go -- which
# test_allowlisted_orphans_are_still_orphans enforces.
ALLOWED_ORPHAN_KEYS: Dict[str, Set[str]] = {category: set() for category in CATEGORIES}

# Models that declare pack registers but leave max_packs at 0, so no pack
# entity is ever created and their pack fields need no names. The library keeps
# those registers on purpose rather than deleting them: EP600's are documented
# in bluetti_bt_lib/devices/ep600.py as untested and waiting on a pack selector
# register that has never been confirmed on hardware.
#
# This is a tripwire, not a permission slip. If a model's max_packs stops being
# zero, the platforms start creating pack_* entities, the inventory grows, and
# test_every_generated_entity_is_translated fails until real translations are
# added. Drop the model from this set in the same change.
DORMANT_PACK_DEVICE_TYPES = {"EP600"}

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
    """The entity names each category claims, failing if a category appeared."""
    entity = translation.get("entity", {})
    unexpected = sorted(set(entity) - set(CATEGORIES))
    if unexpected:
        raise AssertionError(
            f"translation file has an unknown entity category: {unexpected}"
        )
    return {category: set(entity.get(category, {})) for category in CATEGORIES}


def exception_translation_keys() -> Set[str]:
    """Every translation_key the integration raises on an exception."""
    keys: Set[str] = set()
    for source in INTEGRATION_DIR.rglob("*.py"):
        keys.update(EXCEPTION_KEY_RE.findall(source.read_text(encoding="utf-8")))
    return keys


def library_device(dev_type: str) -> BluettiDevice | None:
    device = DEVICES[dev_type]()
    return device if isinstance(device, BluettiDevice) else None


def library_field_names() -> Set[str]:
    """Every field name the library defines on any model.

    Used to tell a pack entity apart from an ordinary sensor whose field
    happens to be called pack_num: only the former gets a pack_ prefix.
    """
    names: Set[str] = set()
    for dev_type in DEVICES:
        device = library_device(dev_type)
        if device is not None:
            names |= {field.name for field in device.fields}
    return names


class _HassStub:
    """Home Assistant stand-in.

    Hashable on purpose: the entity registry helper keys some state by the
    hass object, and a SimpleNamespace is unhashable.
    """

    def __init__(self, data: dict) -> None:
        self.data = data


def _entry(dev_type: str, use_encryption: bool) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="entry-1",
        title=dev_type,
        options={},
        data={
            "address": "AA:BB:CC:DD:EE:FF",
            "name": f"{dev_type}1234567",
            "type": dev_type,
            "use_encryption": use_encryption,
            "polling_interval": 20,
            "polling_timeout": 45,
            "max_retries": 5,
            "pack_poll_interval": 0,
        },
    )


async def _collect_generated_keys() -> Dict[str, Set[str]]:
    """Run every platform against every model and collect the keys it uses."""
    generated = {category: set() for category in CATEGORIES}

    for dev_type in DEVICES:
        # An encrypted station gets an extra read-only mirror of every select
        # register, so both settings have to be walked or those names would
        # look orphaned.
        for use_encryption in (False, True):
            registry = Mock()
            # Report "not registered yet" so pack visibility handling stops
            # without needing a real registry.
            registry.async_get_entity_id.side_effect = lambda *args, **kwargs: None
            hass = _HassStub(
                {
                    DOMAIN: {
                        "entry-1": {DATA_COORDINATOR: Mock(spec=PollingCoordinator)}
                    },
                    er.DATA_REGISTRY: registry,
                }
            )
            entry = _entry(dev_type, use_encryption)

            for category, platform in PLATFORMS.items():
                created: list = []
                await platform.async_setup_entry(hass, entry, created.extend)
                for entity in created:
                    key = getattr(entity, "_attr_translation_key", None)
                    if key:
                        generated[category].add(key)

    return generated


_GENERATED: Dict[str, Set[str]] | None = None


async def generated_entity_keys() -> Dict[str, Set[str]]:
    """Cached inventory; the walk is pure, so one run serves every test."""
    global _GENERATED
    if _GENERATED is None:
        _GENERATED = await _collect_generated_keys()
    return _GENERATED


class TestTranslationsCoverGeneratedEntities(unittest.IsolatedAsyncioTestCase):
    """Each language must name every entity the platforms actually create."""

    async def test_inventory_is_not_silently_empty(self):
        generated = await generated_entity_keys()
        for category in CATEGORIES:
            self.assertTrue(
                generated[category],
                f"the {category} inventory is empty, so the checks below "
                "would pass without proving anything",
            )

    async def test_every_generated_entity_is_translated(self):
        generated = await generated_entity_keys()
        for language in LANGUAGES:
            actual = entity_keys_by_category(load_translation(language))
            for category, keys in generated.items():
                for key in sorted(keys):
                    with self.subTest(language=language, category=category, key=key):
                        self.assertIn(
                            key,
                            actual[category],
                            f"Missing translation for {category}: {key}",
                        )


class TestTranslationsHaveNoOrphans(unittest.IsolatedAsyncioTestCase):
    """Nothing may be named in a translation file that cannot be created."""

    async def test_no_translation_is_orphaned(self):
        generated = await generated_entity_keys()
        for language in LANGUAGES:
            actual = entity_keys_by_category(load_translation(language))
            for category, keys in actual.items():
                orphans = sorted(
                    keys - generated[category] - ALLOWED_ORPHAN_KEYS[category]
                )
                with self.subTest(language=language, category=category):
                    self.assertEqual(
                        orphans,
                        [],
                        f"{language}.json names {category} entities that no "
                        "platform creates; delete them or allowlist them with "
                        "a reason",
                    )

    async def test_allowlisted_orphans_are_still_orphans(self):
        """An allowlist entry that became a real entity is dead weight."""
        generated = await generated_entity_keys()
        for category, allowed in ALLOWED_ORPHAN_KEYS.items():
            stale = sorted(key for key in allowed if key in generated[category])
            self.assertEqual(
                stale,
                [],
                f"these keys are generated again and no longer need the "
                f"allowlist: {stale}",
            )

    async def test_allowlisted_orphans_are_still_translated(self):
        """An allowlisted key must exist in every language, not just en."""
        for language in LANGUAGES:
            actual = entity_keys_by_category(load_translation(language))
            for category, allowed in ALLOWED_ORPHAN_KEYS.items():
                for key in sorted(allowed):
                    with self.subTest(language=language, category=category, key=key):
                        self.assertIn(key, actual[category])


class TestPackSupportIsDeliberate(unittest.IsolatedAsyncioTestCase):
    """Pack names exist only for models that can really poll a pack."""

    def expected_pack_keys(self) -> Set[str]:
        keys: Set[str] = set()
        for dev_type in DEVICES:
            device = library_device(dev_type)
            if device is None or not has_battery_packs(dev_type):
                continue
            keys |= {f"pack_{field.name}" for field in device.pack_fields}
        return keys

    def actual_pack_keys(self, generated: Dict[str, Set[str]]) -> Set[str]:
        fields = library_field_names()
        return {
            key
            for key in generated["sensor"]
            if key.startswith("pack_") and key not in fields
        }

    def test_only_documented_models_have_dormant_pack_fields(self):
        dormant = sorted(
            dev_type
            for dev_type in DEVICES
            if (device := library_device(dev_type))
            and device.pack_fields
            and not has_battery_packs(dev_type)
        )
        self.assertEqual(
            dormant,
            sorted(DORMANT_PACK_DEVICE_TYPES),
            "a model that declares pack registers but cannot poll them is "
            "dormant on purpose; either give it translations and drop it from "
            "DORMANT_PACK_DEVICE_TYPES, or remove the registers",
        )

    async def test_pack_names_match_the_models_that_can_poll_packs(self):
        generated = await generated_entity_keys()
        self.assertEqual(
            self.actual_pack_keys(generated),
            self.expected_pack_keys(),
            "the pack entities that get created are not the ones the pack "
            "translations were written for",
        )

    async def test_no_names_for_packs_that_cannot_be_polled(self):
        """A dormant model must not collect speculative pack translations."""
        expected = self.expected_pack_keys()
        fields = library_field_names()
        for language in LANGUAGES:
            names = entity_keys_by_category(load_translation(language))["sensor"]
            speculative = sorted(
                key
                for key in names
                if key.startswith("pack_") and key not in expected and key not in fields
            )
            with self.subTest(language=language):
                self.assertEqual(
                    speculative,
                    [],
                    f"{language}.json names pack entities for a model that "
                    "cannot poll packs",
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
