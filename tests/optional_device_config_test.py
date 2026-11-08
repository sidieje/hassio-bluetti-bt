"""Tests for the optional polling settings behind the Configure dialog.

validate() is the only thing standing between the form and a coordinator
configured with an interval of zero or a negative retry count, and it decides
which abort reason the user is shown. Nothing exercised it, so a changed
threshold or a renamed reason would have surfaced as a bad config on a live
station rather than as a failure here.
"""

import unittest

from custom_components.bluetti_bt.types import (
    ABORT_REASON_INTERVAL,
    ABORT_REASON_PACK_INTERVAL,
    ABORT_REASON_RETRIES,
    ABORT_REASON_TIMEOUT,
    CONF_MAX_RETRIES,
    CONF_PACK_POLL_INTERVAL,
    CONF_POLLING_INTERVAL,
    CONF_POLLING_TIMEOUT,
    DEFAULT_PACK_POLL_INTERVAL,
    OptionalDeviceConfig,
)


def config(
    polling_interval: int = 20,
    polling_timeout: int = 45,
    max_retries: int = 5,
    pack_poll_interval: int = DEFAULT_PACK_POLL_INTERVAL,
) -> OptionalDeviceConfig:
    return OptionalDeviceConfig(
        polling_interval, polling_timeout, max_retries, pack_poll_interval
    )


class TestValidate(unittest.TestCase):
    """Test the bounds the Configure dialog aborts on."""

    def test_defaults_are_valid(self):
        self.assertIsNone(config().validate())

    def test_lowest_accepted_values(self):
        """The thresholds are inclusive: the boundary itself must pass."""
        self.assertIsNone(
            config(
                polling_interval=5, polling_timeout=1, max_retries=1, pack_poll_interval=0
            ).validate()
        )

    def test_just_below_the_thresholds(self):
        for bad in (
            (config(polling_interval=4), ABORT_REASON_INTERVAL),
            (config(polling_timeout=0), ABORT_REASON_TIMEOUT),
            (config(max_retries=0), ABORT_REASON_RETRIES),
            (config(pack_poll_interval=-1), ABORT_REASON_PACK_INTERVAL),
        ):
            with self.subTest(bad=bad[0].__dict__):
                self.assertEqual(bad[1], bad[0].validate())

    def test_first_failure_is_the_one_reported(self):
        """One reason is shown at a time, so the order is the reported one."""
        broken = config(polling_interval=0, polling_timeout=0, max_retries=0)

        self.assertEqual(ABORT_REASON_INTERVAL, broken.validate())

    def test_a_negative_interval_is_not_mistaken_for_a_timeout(self):
        """The interval is checked first, so it owns its own reason."""
        self.assertEqual(
            ABORT_REASON_INTERVAL, config(polling_interval=-1).validate()
        )


class TestStoredSettings(unittest.TestCase):
    """Test how a stored entry becomes a config and back again."""

    def test_an_empty_entry_falls_back_to_the_defaults(self):
        """An entry added before an option existed still has a value for it."""
        stored = OptionalDeviceConfig.from_dict({})

        self.assertEqual(20, stored.polling_interval)
        self.assertEqual(45, stored.polling_timeout)
        self.assertEqual(5, stored.max_retries)
        self.assertEqual(DEFAULT_PACK_POLL_INTERVAL, stored.pack_poll_interval)
        self.assertIsNone(stored.validate())

    def test_pack_polling_is_off_until_it_is_asked_for(self):
        """The slot walk is most of a cycle, so it stays off by default."""
        self.assertEqual(0, DEFAULT_PACK_POLL_INTERVAL)

    def test_a_stored_entry_survives_a_round_trip(self):
        original = config(polling_interval=30, pack_poll_interval=10)

        self.assertEqual(
            original.as_dict,
            OptionalDeviceConfig.from_dict(original.as_dict).as_dict,
        )

    def test_as_dict_carries_every_option(self):
        self.assertEqual(
            {
                CONF_POLLING_INTERVAL,
                CONF_POLLING_TIMEOUT,
                CONF_MAX_RETRIES,
                CONF_PACK_POLL_INTERVAL,
            },
            set(config().as_dict),
        )


class TestSchema(unittest.TestCase):
    """Test which fields the Configure dialog offers."""

    def test_pack_interval_only_appears_where_it_can_do_something(self):
        with_packs = config().schema(has_packs=True)
        without = config().schema(has_packs=False)

        self.assertIn(CONF_PACK_POLL_INTERVAL, {str(k) for k in with_packs.schema})
        self.assertNotIn(CONF_PACK_POLL_INTERVAL, {str(k) for k in without.schema})

    def test_the_form_starts_at_what_is_stored(self):
        stored = config(polling_interval=30, pack_poll_interval=10)

        defaults = {
            str(key): key.default() if callable(key.default) else key.default
            for key in stored.schema(has_packs=True).schema
        }

        self.assertEqual(30, defaults[CONF_POLLING_INTERVAL])
        self.assertEqual(10, defaults[CONF_PACK_POLL_INTERVAL])

    def test_extra_fields_keep_the_ones_already_there(self):
        """The flow appends the encryption toggle to the same form."""
        schema = config().schema_with({"use_encryption": bool}, has_packs=True)
        names = {str(key) for key in schema.schema}

        self.assertIn("use_encryption", names)
        self.assertIn(CONF_PACK_POLL_INTERVAL, names)


if __name__ == "__main__":
    unittest.main()
