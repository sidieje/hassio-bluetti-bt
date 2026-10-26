"""Coordinator for Bluetti integration."""

from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
import logging
from time import monotonic
from typing import Any
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from bluetti_bt_lib import (
    build_device,
    BluettiDevice,
    DeviceReader,
    DeviceReaderConfig,
    DeviceWriter,
    FieldName,
)
from bluetti_bt_lib.bluetooth import DeviceWriterConfig

from .const import DOMAIN
from .utils import mac_loggable
from .types import FullDeviceConfig

OFFLINE_RETRY_DELAYS = (20, 30, 60)

# Adapters and remote proxies are still coming up while config entries load, so
# the first connection attempts after a restart usually fail. The deadline is
# kept per Home Assistant run rather than per coordinator because a failed
# first refresh tears this coordinator down and the setup retry builds a new
# one, which would otherwise reset the grace and leave a device that is really
# gone unreported.
STARTUP_GRACE_SECONDS = 120
STARTUP_GRACE_KEY = "startup_grace_until"

# A pack walk that answers fewer packs than the station reports is retried on
# the following cycles before its readings are used anyway.
PACK_WALK_RETRIES = 2


class PollingCoordinator(DataUpdateCoordinator):
    """Polling coordinator.

    Keeps a single bluetooth connection per config entry alive for as long as
    the entry is loaded. The reader and the writer share that connection, so
    only one connection per device is opened and no connection is left behind
    when the entry is unloaded.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config: FullDeviceConfig,
        lock: asyncio.Lock,
        config_entry: ConfigEntry,
    ):
        """Initialize coordinator."""
        super().__init__(
            hass,
            logging.getLogger(
                f"{__name__}.{mac_loggable(config.address).replace(':', '_')}"
            ),
            config_entry=config_entry,
            name="Bluetti polling coordinator",
            update_interval=timedelta(seconds=config.polling_interval),
        )

        self.config = config
        self.lock = lock

        # Guards the connection lifecycle, not the BLE traffic itself
        self.connection_lock = asyncio.Lock()

        self.logger.info("Creating client for %s", config.name)
        self.bluetti_device: BluettiDevice | None = build_device(config.name)

        self.client: BleakClient | None = None
        self.reader: DeviceReader | None = None
        self.writer: DeviceWriter | None = None
        self.disconnected = False
        self._offline = False
        # Whether the current outage has been warned about yet. The startup
        # grace defers the warning instead of dropping it, so the first attempt
        # after the grace runs out still reports it.
        self._offline_reported = False
        self._ever_connected = False
        self._startup_grace_until = hass.data.setdefault(DOMAIN, {}).setdefault(
            STARTUP_GRACE_KEY, monotonic() + STARTUP_GRACE_SECONDS
        )
        self._retry_attempt = False
        # Offline retries are one-shot timers rather than a long-lived task:
        # Home Assistant's bootstrap waits for every task created during the
        # setup phase, so a retry loop started by an offline entry at startup
        # holds up the whole system until bootstrap times out.
        self._retry_delay_index = 0
        self._retry_unsub: CALLBACK_TYPE | None = None
        self._retry_attempt_task: asyncio.Task[None] | None = None
        self._unloading = False

        # Walking the battery pack slots costs a selector write and a settle
        # wait per slot, which is most of a poll cycle on a tower station, and
        # a pack that does not answer leaves a hole in the readings. So pack
        # polling is off unless the user asks for it, and when it is on the
        # walk runs on a slower clock while the values from the last complete
        # walk are reused in between.
        self._pack_interval = config.pack_poll_interval * 60
        self._pack_due_at = 0.0
        self._pack_values: dict[str, Any] = {}
        self._pack_num: int | None = None
        self._pack_prefixes: tuple[str, ...] = ()
        self._pack_retries = 0

        if self.bluetti_device is None:
            self.logger.error("Device is unknown type: %s", config.name)
            return

        self._pack_prefixes = tuple(
            f"pack_{slot}_" for slot in range(1, self.bluetti_device.max_packs + 1)
        )

    def _on_disconnected(self, _: BleakClient) -> None:
        """Mark the connection as stale so it gets rebuilt on next use."""
        self.logger.info("Bluetooth connection lost")
        self.disconnected = True

    async def _async_get_client(self) -> BleakClient | None:
        """Return the shared connection, opening it when needed."""
        async with self.connection_lock:
            return await self._async_get_client_unlocked()

    async def _async_get_client_unlocked(self) -> BleakClient | None:
        """Connection setup. The caller must hold connection_lock.

        Split out because asyncio.Lock is not reentrant: the write path
        holds connection_lock across the whole write, so it needs the
        setup logic without reacquiring the lock.
        """
        if self.bluetti_device is None:
            return None

        # Regular polling must not bypass the offline retry backoff.
        if self._offline and not self._retry_attempt:
            return None

        if (
            self.client is not None
            and self.client.is_connected
            and not self.disconnected
        ):
            return self.client

        if self.client is not None:
            await self._async_drop_connection("Connection lost")

        device = await BleakScanner.find_device_by_address(
            self.config.address, timeout=5
        )

        if device is None:
            self._mark_offline(
                f"{mac_loggable(self.config.address)} is not advertising: it is "
                "probably powered off, asleep or out of range. Polling will retry."
            )
            return None

        try:
            self.client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                device.name or "Unknown Device",
                disconnected_callback=self._on_disconnected,
                max_attempts=self.config.max_retries,
            )
        except (BleakError, OSError, TimeoutError) as err:
            self.client = None
            self._mark_offline(
                f"Unable to connect to "
                f"{mac_loggable(self.config.address)}: {self._offline_reason(err)}",
                detail=self._hide_address(err),
            )
            return None

        self.disconnected = False

        # Reader and writer share the connection, so they are recreated
        # whenever the connection is rebuilt.
        try:
            reader = DeviceReader(
                self.config.address,
                self.bluetti_device,
                self.hass.loop.create_future,
                DeviceReaderConfig(
                    self.config.polling_timeout,
                    self.config.use_encryption,
                ),
                self.lock,
                ble_client=self.client,
            )
        except Exception as err:
            await self._async_drop_connection("Reader initialization failed")
            self._mark_offline(
                f"Unable to initialize reader for "
                f"{mac_loggable(self.config.address)}: {err}"
            )
            return None

        self.reader = reader
        self.writer = None

        # A fresh connection walks the slots on its first cycle, so the pack
        # entities and the slot watcher start from what the device reports now.
        self._pack_due_at = 0.0
        self._pack_retries = 0

        if self._offline:
            self.logger.info(
                "Bluetooth device recovered: %s",
                mac_loggable(self.config.address),
            )
        self._offline = False
        self._offline_reported = False
        self._ever_connected = True

        # Back online, so the next outage starts at the short end of the
        # backoff again and no retry is left waiting to fire.
        self._retry_delay_index = 0
        self._cancel_retry_timer()

        self.logger.debug("Connected to %s", mac_loggable(self.config.address))
        return self.client

    def _in_startup_grace(self) -> bool:
        """Whether an offline report is still too early to warn about."""
        return not self._ever_connected and monotonic() < self._startup_grace_until

    def _offline_reason(self, err: BaseException) -> str:
        """What a failed connection most likely means, on one line.

        bleak_retry_connector's own text is several lines of scanner and
        adapter internals that only make sense with debug logging turned on, so
        the warning carries the guess the user can act on and the detail goes to
        DEBUG. Running out of connection slots is the exception: it names
        something the user can actually fix, so its advice stays on the warning.
        """
        if "connection slot" in str(err):
            return (
                "the proxy or adapter is out of connection slots or the device "
                "is no longer reachable; add additional proxies "
                "(https://esphome.io/projects/?type=bluetooth) near this device. "
                "Polling will retry."
            )

        return "it is probably powered off, asleep or out of range. Polling will retry."

    def _hide_address(self, err: BaseException) -> str:
        """bleak's text with this device's address masked out.

        bleak_retry_connector quotes the full address back at us, and the
        privacy helper applies to every line we log, debug included.
        """
        return str(err).replace(self.config.address, mac_loggable(self.config.address))

    def _mark_offline(self, message: str, detail: str | None = None) -> None:
        """Mark the device offline and ensure a retry timer is armed.

        An outage is warned about once, whether the warning came from the first
        failed attempt or from the first one after the startup grace ran out.
        """
        if self._offline_reported:
            self.logger.debug("Device remains offline: %s", message)
        elif self._in_startup_grace():
            self.logger.debug("Offline during startup grace: %s", message)
        else:
            self.logger.warning(message)
            self._offline_reported = True

        if detail is not None:
            self.logger.debug("Connection failure detail: %s", detail)

        self._offline = True

        if self._unloading:
            return

        self._arm_retry_timer()

    def _arm_retry_timer(self) -> None:
        """Arm one retry attempt at the current backoff delay.

        A failed attempt re-arms the timer itself, so a pending timer means
        the next retry is already scheduled and cannot be stacked.
        """
        if self._retry_unsub is not None:
            return

        self._retry_unsub = async_call_later(
            self.hass,
            OFFLINE_RETRY_DELAYS[self._retry_delay_index],
            self._async_retry_attempt,
        )

    def _cancel_retry_timer(self) -> None:
        """Cancel a pending retry timer, if one is armed."""
        if self._retry_unsub is None:
            return

        unsub, self._retry_unsub = self._retry_unsub, None
        unsub()

    async def _async_retry_attempt(self, _now: datetime | None = None) -> None:
        """Make a single connection attempt, then re-arm the next timer.

        async_call_later runs this as a short-lived task when the timer
        fires, which is after startup has wrapped up, so an entry that was
        offline at startup cannot hold up bootstrap the way a long-lived
        retry task did.
        """
        self._retry_unsub = None
        self._retry_attempt_task = asyncio.current_task()

        try:
            if not self._offline or self._unloading:
                return

            # The delay that just elapsed was for this attempt, so the next
            # one is already a step further along the backoff.
            self._retry_delay_index = min(
                self._retry_delay_index + 1,
                len(OFFLINE_RETRY_DELAYS) - 1,
            )

            self._retry_attempt = True
            try:
                # Attempt the connection directly so a coalesced
                # coordinator refresh cannot skip the retry.
                client = await self._async_get_client()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.debug("Offline retry failed: %s", err)
                client = None
            finally:
                self._retry_attempt = False

            if client is not None and self.reader is not None:
                try:
                    await self.async_request_refresh()
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    self.logger.debug(
                        "Refresh after reconnect failed: %s", err
                    )

            # A failed attempt leaves the device offline, and the connection
            # attempt itself may already have armed the next timer.
            if self._offline and not self._unloading:
                self._arm_retry_timer()
        finally:
            self._retry_attempt = False
            self._retry_attempt_task = None

    async def _async_drop_connection(self, reason: str) -> None:
        """Close the shared connection. Requires connection_lock to be held."""
        self.logger.debug("Dropping bluetooth connection (%s)", reason)

        writer, self.writer = self.writer, None
        reader, self.reader = self.reader, None
        client, self.client = self.client, None
        self.disconnected = False

        # The reader owns the notifier, so it goes first and takes the shared
        # connection down with it
        for target in (reader, writer):
            if target is None:
                continue
            try:
                await target.disconnect()
            except Exception as err:  # pylint: disable=broad-except
                self.logger.debug("Error while disconnecting: %s", err)

        if client is not None:
            try:
                await client.disconnect()
            except Exception as err:  # pylint: disable=broad-except
                self.logger.debug("Error while disconnecting client: %s", err)

    async def _async_get_writer(self) -> DeviceWriter | None:
        """Return the writer sharing the connection with the reader.

        The caller must hold connection_lock: the returned writer is only
        valid while the shared connection cannot be torn down underneath
        it, which is why the write methods hold the lock across the whole
        write.
        """
        client = await self._async_get_client_unlocked()

        if client is None or self.bluetti_device is None:
            return None

        if self.writer is None or self.writer.client is not client:
            writer = DeviceWriter(
                client,
                self.bluetti_device,
                DeviceWriterConfig(
                    timeout=30,
                    use_encryption=self.config.use_encryption,
                ),
                self.lock,
                reader=self.reader,
            )
            # The reader owns the notifier and the encryption handshake
            writer.attach(encryption=self.reader.encryption if self.reader else None)
            self.writer = writer

        return self.writer

    async def async_write(self, field: str, value: Any) -> bool:
        """Write a value to the device over the shared connection.

        connection_lock is held for the whole operation so an offline retry
        attempt or an unload cannot drop the shared client while a write is
        in flight. The wait is bounded by the writer timeout.
        """
        async with self.connection_lock:
            writer = await self._async_get_writer()

            if writer is None:
                self.logger.warning("Unable to write %s: no connection", field)
                return False

            if self.config.use_encryption and self.reader is not None:
                if not self.reader.encryption.is_ready_for_commands:
                    # Handshake is done by the reader on the shared connection
                    await self.reader.read()

                if not self.reader.encryption.is_ready_for_commands:
                    self.logger.warning(
                        "Encryption handshake not finished, not writing %s",
                        field,
                    )
                    return False

            return bool(await writer.write(field, value))

    async def async_write_verified(self, field: str, value: Any) -> Any:
        """Write a value and return the device's verified state.

        The write and its verification read-back are one lifecycle
        operation: connection_lock is held across both so a reconnect
        cannot interrupt between the write and the read-back.

        Only a verified read-back is merged into the cached data; see the
        comment below for why an unverified one is dropped.
        """
        async with self.connection_lock:
            writer = await self._async_get_writer()

            if writer is None:
                self.logger.warning(
                    "Unable to write %s: no connection",
                    field,
                )
                return None

            if self.config.use_encryption and self.reader is not None:
                if not self.reader.encryption.is_ready_for_commands:
                    await self.reader.read()

                if not self.reader.encryption.is_ready_for_commands:
                    self.logger.warning(
                        "Encryption handshake not finished, not writing %s",
                        field,
                    )
                    return None

            result = await writer.write_verified(field, value)

        # A write that did not verify only means the read-back disagreed: the
        # station can hand back the pre-write value because its BLE copy of
        # the register refreshes some time after the write. Merging that stale
        # read-back would push the old value to the entities and the UI would
        # flip back even though the device changed. Leave the cached data
        # alone instead, so the entities keep their last known value and the
        # next poll replaces it with what the device actually holds.
        if (
            result.verified
            and result.values is not None
            and isinstance(self.data, dict)
        ):
            updated_data = {**self.data, **result.values}
            self.async_set_updated_data(updated_data)

        return result

    async def async_unload(self) -> None:
        """Close the persistent connection and cancel offline retries."""
        self._unloading = True

        self._cancel_retry_timer()

        attempt_task, self._retry_attempt_task = self._retry_attempt_task, None
        if attempt_task is not None and not attempt_task.done():
            attempt_task.cancel()
            try:
                await attempt_task
            except asyncio.CancelledError:
                pass

        async with self.connection_lock:
            await self._async_drop_connection("Unloading config entry")

    async def apply_options(self, config: FullDeviceConfig) -> None:
        """Adopt changed entry options without restarting the entry."""

        previous = self.config
        self.config = config

        # The next run is scheduled from this value at the end of every
        # refresh, so the new cadence takes effect from the refresh below.
        self.update_interval = timedelta(seconds=config.polling_interval)

        pack_interval = config.pack_poll_interval * 60

        if pack_interval != self._pack_interval:
            self._pack_interval = pack_interval
            # Turning pack polling on should walk on the next cycle rather than
            # wait out an interval inherited from when it was off. The walk
            # re-arms the deadline itself, so this cannot schedule two walks.
            self._pack_due_at = 0.0

        # The reader is built from this config when a connection opens, so a
        # changed timeout or retry count only reaches the device through a new
        # connection. connection_lock is the lock the polling and write paths
        # take, so nothing can be mid-flight when it is dropped.
        if (
            previous.polling_timeout != config.polling_timeout
            or previous.max_retries != config.max_retries
        ):
            async with self.connection_lock:
                await self._async_drop_connection("Polling options changed")

        self.logger.debug(
            "Polling options updated for %s: interval %ss, timeout %ss, "
            "retries %s, pack interval %s",
            mac_loggable(config.address),
            config.polling_interval,
            config.polling_timeout,
            config.max_retries,
            config.pack_poll_interval,
        )

        if self._unloading:
            return

        # Poll now, so the changed settings are in use immediately rather than
        # after the previous interval has run out.
        await self.async_request_refresh()

    def _packs_due(self) -> bool:
        """Whether this cycle should walk the battery pack slots."""
        if self._pack_interval <= 0:
            return False

        return monotonic() >= self._pack_due_at

    def _walk_complete(self, data: dict[str, Any]) -> bool:
        """Whether a walk answered every pack the station says it has.

        A pack that fails to answer leaves its slot out of pack_slots, so a
        short walk is a flaky read rather than a pack that was removed. The
        station's own pack count is read every cycle, which is what makes the
        walk checkable.
        """
        pack_num = data.get(FieldName.PACK_NUM.value)
        slots = data.get(FieldName.PACK_SLOTS.value)

        if not isinstance(pack_num, int) or not isinstance(slots, list):
            return False

        return len(slots) >= pack_num

    def _note_packs(self, data: dict[str, Any], walked: bool) -> None:
        """Track what the cycle said about the battery packs.

        A walk replaces the cached pack values and starts the next interval.
        The pack count is a station register read every cycle, so a change is
        noticed even when the slots were not walked, and the next cycle walks.
        """
        if walked:
            self._pack_values = {
                key: value
                for key, value in data.items()
                if key == FieldName.PACK_SLOTS.value
                or key.startswith(self._pack_prefixes)
            }
            self._pack_due_at = monotonic() + self._pack_interval

        pack_num = data.get(FieldName.PACK_NUM.value)

        if self._pack_num is not None and pack_num != self._pack_num:
            self._pack_due_at = 0.0

        self._pack_num = pack_num

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """

        if self.bluetti_device is None:
            self.logger.error(
                "Reader not initialized - device type may be unsupported: %s",
                self.config.name,
            )
            self.last_update_success = False
            return None

        client = await self._async_get_client()

        if client is None or self.reader is None:
            self.last_update_success = False
            return None

        include_packs = self._packs_due()
        data = await self.reader.read(include_packs=include_packs)

        if data is None:
            return None

        if include_packs:
            if self._walk_complete(data) or self._pack_retries >= PACK_WALK_RETRIES:
                self._pack_retries = 0
                self._note_packs(data, walked=True)
            else:
                # A walk that lost a pack is a flaky read, so keep the last
                # complete walk rather than showing a pack that is still
                # there, and try again on the next cycle.
                self._pack_retries += 1
                self._pack_due_at = monotonic()
                data.update(self._pack_values)
                self.logger.debug(
                    "Pack walk answered %s of %s packs, trying again next cycle",
                    len(data.get(FieldName.PACK_SLOTS.value) or []),
                    data.get(FieldName.PACK_NUM.value),
                )
        else:
            data.update(self._pack_values)
            self._note_packs(data, walked=False)

        return data
