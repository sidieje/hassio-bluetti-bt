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

        if self.bluetti_device is None:
            self.logger.error("Device is unknown type: %s", config.name)
            return

        self.client: BleakClient | None = None
        self.reader: DeviceReader | None = None
        self.writer: DeviceWriter | None = None
        self.disconnected = False

    def _on_disconnected(self, _: BleakClient) -> None:
        """Mark the connection as stale so it gets rebuilt on next use."""
        self.logger.info("Bluetooth connection lost")
        self.disconnected = True

    async def _async_get_client(self) -> BleakClient | None:
        """Return the shared connection, opening it when needed."""
        if self.bluetti_device is None:
            return None

        async with self.connection_lock:
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
                self.logger.warning(
                    "Device not found: %s", mac_loggable(self.config.address)
                )
                return None

            try:
                self.client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    device.name or "Unknown Device",
                    disconnected_callback=self._on_disconnected,
                    max_attempts=10,
                )
            except (BleakError, OSError, TimeoutError) as err:
                self.logger.warning(
                    "Unable to connect to %s: %s",
                    mac_loggable(self.config.address),
                    err,
                )
                self.client = None
                return None

            self.disconnected = False

            # Reader and writer share the connection, so they are recreated
            # whenever the connection is rebuilt
            self.reader = DeviceReader(
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
            self.writer = None

            self.logger.debug("Connected to %s", mac_loggable(self.config.address))

            return self.client

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
        """Return the writer sharing the connection with the reader."""
        client = await self._async_get_client()

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
            )
            # The reader owns the notifier and the encryption handshake
            writer.attach(encryption=self.reader.encryption if self.reader else None)
            self.writer = writer

        return self.writer

    async def async_write(self, field: str, value: Any) -> bool:
        """Write a value to the device over the shared connection."""
        writer = await self._async_get_writer()

        if writer is None:
            self.logger.warning("Unable to write %s: no connection", field)
            return False

        if self.config.use_encryption and self.reader is not None:
            if not self.reader.encryption.is_ready_for_commands:
                # The handshake is done by the reader on the shared connection
                await self.reader.read()

            if not self.reader.encryption.is_ready_for_commands:
                self.logger.warning(
                    "Encryption handshake not finished, not writing %s", field
                )
                return False

        return bool(await writer.write(field, value))

    async def async_unload(self) -> None:
        """Close the persistent connection."""
        async with self.connection_lock:
            await self._async_drop_connection("Unloading config entry")

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """

        # Check if device is connected
        if (
            bluetooth.async_address_present(
                self.hass, self.config.address, connectable=True
            )
            is False
        ):
            self.logger.warning("Device not connected")
            self.last_update_success = False
            return None

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
