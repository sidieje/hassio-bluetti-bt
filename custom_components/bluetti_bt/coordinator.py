"""Coordinator for Bluetti integration."""

from __future__ import annotations
import asyncio
from datetime import timedelta
import logging
from typing import Any
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from bluetti_bt_lib import (
    build_device,
    BluettiDevice,
    DeviceReader,
    DeviceReaderConfig,
    DeviceWriter,
)
from bluetti_bt_lib.bluetooth import DeviceWriterConfig

from .utils import mac_loggable
from .types import FullDeviceConfig

OFFLINE_RETRY_DELAYS = (20, 30, 60)


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
    ):
        """Initialize coordinator."""
        super().__init__(
            hass,
            logging.getLogger(
                f"{__name__}.{mac_loggable(config.address).replace(':', '_')}"
            ),
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
        self._retry_attempt = False
        self._retry_task: asyncio.Task[None] | None = None
        self._unloading = False

        if self.bluetti_device is None:
            self.logger.error("Device is unknown type: %s", config.name)
            return

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
                f"Device not found: {mac_loggable(self.config.address)}"
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
                f"{mac_loggable(self.config.address)}: {err}"
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

        if self._offline:
            self.logger.info(
                "Bluetooth device recovered: %s",
                mac_loggable(self.config.address),
            )
        self._offline = False

        self.logger.debug("Connected to %s", mac_loggable(self.config.address))
        return self.client

    def _mark_offline(self, message: str) -> None:
        """Mark the device offline and ensure a retry task is running."""
        if not self._offline:
            self.logger.warning(message)
        else:
            self.logger.debug("Device remains offline: %s", message)

        self._offline = True

        if self._unloading:
            return

        if self._retry_task is None or self._retry_task.done():
            self._retry_task = self.hass.async_create_task(
                self._async_retry_loop()
            )

    async def _async_retry_loop(self) -> None:
        """Retry discovery independently of the normal polling interval."""
        delay_index = 0

        try:
            while self._offline and not self._unloading:
                await asyncio.sleep(OFFLINE_RETRY_DELAYS[delay_index])

                if not self._offline or self._unloading:
                    break

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

                delay_index = min(
                    delay_index + 1,
                    len(OFFLINE_RETRY_DELAYS) - 1,
                )
        finally:
            self._retry_attempt = False
            self._retry_task = None

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

        connection_lock is held for the whole operation so the offline
        retry loop or an unload cannot drop the shared client while a
        write is in flight. The wait is bounded by the writer timeout.
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

        if result.values is not None and isinstance(self.data, dict):
            updated_data = {**self.data, **result.values}
            self.async_set_updated_data(updated_data)

        return result

    async def async_unload(self) -> None:
        """Close the persistent connection and cancel offline retries."""
        self._unloading = True

        retry_task, self._retry_task = self._retry_task, None
        if retry_task is not None:
            retry_task.cancel()
            try:
                await retry_task
            except asyncio.CancelledError:
                pass

        async with self.connection_lock:
            await self._async_drop_connection("Unloading config entry")

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

        return await self.reader.read()
