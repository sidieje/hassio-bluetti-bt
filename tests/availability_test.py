"""Tests for the availability debounce the five platforms share.

Every entity in this integration overrides Home Assistant's own
CoordinatorEntity.available, so the debounce over the coordinator's failed
cycles is applied by debounced_available() instead. It reads the coordinator
rather than a counter the entity keeps itself, because an entity counting its
own notifications stops counting at the first failure.

The platform classes are driven through their own unbound methods, so a
divergence in any one of them fails here. That happened once already: the
threshold was written out five times in five files and four of them drifted.
"""
import inspect
import logging
import unittest

from custom_components.bluetti_bt.binary_sensor import BluettiBinarySensor
from custom_components.bluetti_bt.const import UNAVAILABLE_AFTER_FAILED_CYCLES
from custom_components.bluetti_bt.number import BluettiNumber
from custom_components.bluetti_bt.select import BluettiSelect
from custom_components.bluetti_bt.sensor import BluettiSensor
from custom_components.bluetti_bt.switch import BluettiSwitch

PLATFORMS = [
    ("sensor", BluettiSensor),
    ("binary_sensor", BluettiBinarySensor),
    ("select", BluettiSelect),
    ("switch", BluettiSwitch),
    ("number", BluettiNumber),
]


class FakeCoordinator:
    """The two attributes the availability rule reads."""

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.last_update_success = True


class ProbeEntity:
    """Just enough of an entity for the availability methods to run."""

    def __init__(self, coordinator: FakeCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_available = True
        self._attr_extra_state_attributes = {}
        self._unavailable_counter = 0
        self._attr_unique_id = "AA:BB:CC:DD:EE:FF_battery_soc"
        self._logger = logging.getLogger("bluetti_bt.test")
        self.writes: list[tuple[bool, dict]] = []

    def async_write_ha_state(self) -> None:
        self.writes.append((self.available, dict(self._attr_extra_state_attributes)))


def entity_class(platform: type) -> type:
    """A probe that runs one platform's own availability methods."""

    # available is a property on the platform classes, so it has to be read
    # through its getter rather than called.
    available = platform.available.fget

    class Probe(ProbeEntity):
        @property
        def available(self) -> bool:
            return available(self)

        def _set_available(self) -> None:
            platform._set_available(self)

        def _set_unavailable(self, cause: str = "Unknown") -> None:
            platform._set_unavailable(self, cause)

    Probe.__name__ = f"Probe{platform.__name__}"
    return Probe


class TestAvailabilityLadder(unittest.TestCase):
    """An entity goes unavailable after the shared number of failed cycles."""

    def probe(self, platform: type) -> tuple[FakeCoordinator, ProbeEntity]:
        coordinator = FakeCoordinator()
        return coordinator, entity_class(platform)(coordinator)

    def test_the_ladder_is_the_same_on_every_platform(self):
        """Every platform flips on the same cycle, no earlier or later."""
        for name, platform in PLATFORMS:
            with self.subTest(platform=name):
                coordinator, entity = self.probe(platform)

                for cycle in range(1, UNAVAILABLE_AFTER_FAILED_CYCLES + 1):
                    coordinator.consecutive_failures = cycle
                    entity._set_unavailable("Polling failed")

                    expected = cycle >= UNAVAILABLE_AFTER_FAILED_CYCLES
                    self.assertEqual(
                        not expected,
                        entity.available,
                        f"{name} went unavailable on cycle {cycle}",
                    )

    def test_availability_does_not_need_a_notification(self):
        """The rule is read from the coordinator, not from the notifications.

        Home Assistant stops notifying the entities while a refresh keeps
        failing, so an entity that only reacted to being told would sit there
        presenting frozen readings for the rest of the outage.
        """
        for name, platform in PLATFORMS:
            with self.subTest(platform=name):
                coordinator, entity = self.probe(platform)

                coordinator.consecutive_failures = UNAVAILABLE_AFTER_FAILED_CYCLES
                coordinator.last_update_success = False

                self.assertFalse(entity.available)

    def test_the_entities_own_reason_still_wins(self):
        """An empty pack slot is unavailable whatever the coordinator says."""
        for name, platform in PLATFORMS:
            with self.subTest(platform=name):
                _, entity = self.probe(platform)

                entity._attr_available = False

                self.assertFalse(entity.available)

    def test_a_successful_cycle_clears_the_ladder(self):
        """Recovery is immediate, and the counter starts over."""
        for name, platform in PLATFORMS:
            with self.subTest(platform=name):
                coordinator, entity = self.probe(platform)

                for _ in range(UNAVAILABLE_AFTER_FAILED_CYCLES):
                    coordinator.consecutive_failures += 1
                    entity._set_unavailable("Polling failed")
                self.assertFalse(entity.available)

                coordinator.consecutive_failures = 0
                coordinator.last_update_success = True
                entity._set_available()

                self.assertTrue(entity.available)
                self.assertEqual(0, entity._unavailable_counter)
                self.assertEqual({}, entity._attr_extra_state_attributes)


class TestFailedCycleAttributes(unittest.TestCase):
    """An outage stops rewriting the entities once they are unavailable."""

    def test_the_attributes_stop_moving_at_the_threshold(self):
        """The counter is capped, so Home Assistant can skip the write.

        Home Assistant drops a state write whose state and attributes match
        the last one. A counter that kept climbing changed the attributes on
        every cycle of an outage, which meant every entity of a tower station
        was rewritten for as long as the station stayed asleep.
        """
        for name, platform in PLATFORMS:
            with self.subTest(platform=name):
                coordinator = FakeCoordinator()
                entity = entity_class(platform)(coordinator)

                published = []
                for _ in range(UNAVAILABLE_AFTER_FAILED_CYCLES * 2):
                    coordinator.consecutive_failures += 1
                    entity._set_unavailable("Polling failed")
                    published.append(entity._attr_extra_state_attributes)

                self.assertEqual(
                    len(set(str(a) for a in published)),
                    UNAVAILABLE_AFTER_FAILED_CYCLES,
                    f"{name} kept changing its attributes once unavailable",
                )

    def test_the_cause_is_still_reported(self):
        """Capping the counter does not swallow why the entity is unavailable."""
        coordinator = FakeCoordinator()
        entity = entity_class(BluettiSensor)(coordinator)

        for _ in range(UNAVAILABLE_AFTER_FAILED_CYCLES + 2):
            coordinator.consecutive_failures += 1
            entity._set_unavailable("Polling failed")

        self.assertEqual(
            "Polling failed", entity._attr_extra_state_attributes["unavailable_cause"]
        )


class TestSharedThreshold(unittest.TestCase):
    """The threshold lives in one place."""

    def test_the_platforms_all_read_the_constant(self):
        """No platform may write the number out again.

        The threshold used to be a literal five in ten places, five initialisers
        and five comparisons, and four of the comparisons had drifted apart.
        """
        for name, platform in PLATFORMS:
            with self.subTest(platform=name):
                source = inspect.getsource(platform._set_unavailable)
                self.assertNotIn(">= 5", source)
                self.assertIn("UNAVAILABLE_AFTER_FAILED_CYCLES", source)


if __name__ == "__main__":
    unittest.main()
