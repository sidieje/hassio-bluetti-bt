"""Constants for the Bluetti BT integration."""

DOMAIN = "bluetti_bt"
MANUFACTURER = "Bluetti"

CONF_OPTIONS = "options"

DATA_COORDINATOR = "coordinator"

# How many consecutive failed poll cycles an entity waits before it goes
# unavailable. There is one value for all five platforms because it used to be
# a literal 5 written down ten times: five initialisers and five comparisons,
# and four of the initialisers drifted to 5 instead of 0, so only sensors
# actually debounced while the other platforms went unavailable on the first
# failed cycle.
#
# The window is deliberately counted in cycles rather than in seconds, so the
# detection latency is 5 x polling_interval: about 100s at the 20s default and
# 25s at the 5s floor, but longer if the interval is raised. Timing it in
# seconds instead would need the coordinator to record its own last success,
# because Home Assistant only keeps last_update_success_time on
# TimestampDataUpdateCoordinator, which this integration does not use.
#
# The counter is cleared by _set_available() on any successful cycle, so a
# station that answers one poll in five is never marked unavailable. That blind
# spot is the anti-flap tradeoff rather than a bug: an intermittent station
# would otherwise flicker its entities every other cycle. Lower this constant
# for all platforms if the latency is what matters more than the flapping.
UNAVAILABLE_AFTER_FAILED_CYCLES = 5
