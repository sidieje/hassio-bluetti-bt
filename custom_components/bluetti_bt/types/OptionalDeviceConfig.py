from typing import Any, Dict
import voluptuous as vol

CONF_POLLING_INTERVAL = "polling_interval"
CONF_POLLING_TIMEOUT = "polling_timeout"
CONF_MAX_RETRIES = "max_retries"
CONF_PACK_POLL_INTERVAL = "pack_poll_interval"

ABORT_REASON_INTERVAL = "invalid_interval"
ABORT_REASON_TIMEOUT = "invalid_timeout"
ABORT_REASON_RETRIES = "invalid_retries"
ABORT_REASON_PACK_INTERVAL = "invalid_pack_interval"

# Pack polling is off by default: the slot walk is most of a poll cycle and
# its readings are the least reliable part of it. Minutes above zero turn it
# on at that cadence.
DEFAULT_PACK_POLL_INTERVAL = 0


class OptionalDeviceConfig:
    def __init__(
        self,
        polling_interval: int,
        polling_timeout: int,
        max_retries: int,
        pack_poll_interval: int = DEFAULT_PACK_POLL_INTERVAL,
    ):
        self.polling_interval = polling_interval
        self.polling_timeout = polling_timeout
        self.max_retries = max_retries
        self.pack_poll_interval = pack_poll_interval

    @staticmethod
    def from_dict(raw: Dict[str, Any]):
        return OptionalDeviceConfig(
            raw.get(CONF_POLLING_INTERVAL, 20),
            raw.get(CONF_POLLING_TIMEOUT, 45),
            raw.get(CONF_MAX_RETRIES, 5),
            raw.get(CONF_PACK_POLL_INTERVAL, DEFAULT_PACK_POLL_INTERVAL),
        )

    def validate(self) -> str | None:
        if self.polling_interval < 5:
            return ABORT_REASON_INTERVAL
        if self.polling_timeout < 1:
            return ABORT_REASON_TIMEOUT
        if self.max_retries < 1:
            return ABORT_REASON_RETRIES
        if self.pack_poll_interval < 0:
            return ABORT_REASON_PACK_INTERVAL
        return None

    @property
    def as_dict(self) -> Dict[str, Any]:
        return {
            CONF_POLLING_INTERVAL: self.polling_interval,
            CONF_POLLING_TIMEOUT: self.polling_timeout,
            CONF_MAX_RETRIES: self.max_retries,
            CONF_PACK_POLL_INTERVAL: self.pack_poll_interval,
        }

    def schema(self, has_packs: bool = True) -> vol.Schema:
        """The options form.

        Pack polling is only offered where it can do something: the tower
        stations have pack registers behind a selector, the rest do not.
        """
        fields: Dict[Any, Any] = {
            vol.Required(
                CONF_POLLING_INTERVAL,
                default=self.polling_interval,
            ): int,
            vol.Required(
                CONF_POLLING_TIMEOUT,
                default=self.polling_timeout,
            ): int,
            vol.Required(
                CONF_MAX_RETRIES,
                default=self.max_retries,
            ): int,
        }

        if has_packs:
            fields[
                vol.Required(
                    CONF_PACK_POLL_INTERVAL,
                    default=self.pack_poll_interval,
                )
            ] = int

        return vol.Schema(fields)

    def schema_with(self, extra: Dict[Any, Any], has_packs: bool = True) -> vol.Schema:
        """The options form with extra fields appended."""
        return vol.Schema({**self.schema(has_packs).schema, **extra})
