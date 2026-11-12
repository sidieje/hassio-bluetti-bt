"""Bluetti Bluetooth Config Flow"""

from __future__ import annotations
import re
import logging
from typing import Any
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResult
from bluetti_bt_lib import recognize_device
from bluetti_bt_lib.devices import DEVICES, DEVICE_NAME_RE

from .types import (
    CONF_TYPE,
    CONF_USE_ENCRYPTION,
    InitialDeviceConfig,
    ManufacturerData,
    OptionalDeviceConfig,
)
from .const import DOMAIN
from .utils import device_class_for_type

_LOGGER = logging.getLogger(__name__)

# Models whose bluetooth encryption requirement is known. Their advertised name
# carries both the type and the serial number, so they can be added without
# opening a connection to probe them. Anything else keeps the connection based
# recognition, which checks both the encrypted and the unencrypted channel
# before it decides. The AC500 is left out because later revisions encrypt.
KNOWN_DEVICE_TYPES: dict[str, bool] = {
    "AC200M": False,
    "AC300": False,
    "AP300": True,
    "CHARGER2": True,
    "EL10": True,
    "EL100mini": True,
    "EL100V2": True,
    "EL300": True,
    "EL30V2": True,
    # The Elite 200 V2 is keyed the way it reports itself, spaces included,
    # because that is the string in its type register and in its advertisement.
    "Elite 200 V2": True,
}


def identify_from_bt_name(name: Any) -> tuple[str, str, bool] | None:
    """Identify a known device from the name it advertises.

    Returns the device type, the type with its serial number appended, and
    whether to use encryption. Returns None for anything that is not one of
    the known devices, so the caller falls back to probing it.
    """
    if not isinstance(name, str):
        return None

    match = DEVICE_NAME_RE.match(name)

    if match is None:
        return None

    # The Charger 2 advertises "CHARGER 2…" with a space, but the device table
    # is keyed "CHARGER2". Handsfree devices keep the space in both, so only
    # close up the gap when the advertised spelling is unknown.
    dev_type = match[1] if match[1] in DEVICES else match[1].replace(" ", "")

    if dev_type not in KNOWN_DEVICE_TYPES:
        return None

    # The entry is built by name later on, so the type has to be buildable
    if DEVICES.get(dev_type) is None:
        return None

    return dev_type, dev_type + match[2], KNOWN_DEVICE_TYPES[dev_type]


def manual_device_name(dev_type: str, advertised: Any) -> str:
    """The name to store for a model the user picked by hand.

    A station the recognition missed still advertises its serial number most
    of the time, so it is kept whenever the advertised name can be read: the
    entry title and the serial sensor are both built from it. A name that
    cannot be read leaves the bare type, which is enough, because the entry
    also stores the picked type exactly and that is what the device is built
    from.
    """
    if isinstance(advertised, str):
        match = DEVICE_NAME_RE.match(advertised)

        if match is not None:
            return dev_type + match[2]

    return dev_type


def selectable_device_types() -> list[str]:
    """The models to offer for a manual pick, the known ones first.

    A manual pick is what happens once both the advertised name and a probe
    have failed, so the whole device table is on offer rather than only the
    models with a known channel. Anything the library cannot build on its own
    is left out, since an entry could never be set up from it.
    """
    selectable: list[str] = []

    for dev_type, device_cls in DEVICES.items():
        try:
            device_cls()
        except Exception:  # a model that cannot be built cannot be picked
            continue

        selectable.append(dev_type)

    return sorted(
        selectable,
        key=lambda dev_type: (dev_type not in KNOWN_DEVICE_TYPES, dev_type.lower()),
    )


def has_battery_packs(dev_type: Any) -> bool:
    """Whether a model has battery pack registers to walk.

    Only the tower stations keep their packs behind a selector, so the options
    form should not offer pack polling to a Charger 2 or an EL100 V2, where it
    could only ever do nothing.
    """
    device_cls = device_class_for_type(dev_type)

    if device_cls is None:
        return False

    try:
        device = device_cls()
    except Exception:  # a model that cannot be built has nothing to offer
        return False

    return bool(device.max_packs) and bool(device.pack_fields)


class BluettiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Bluetti BT devices."""

    def __init__(self) -> None:
        _LOGGER.info("Initialize config flow")
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._encryption_assumed = False

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle bluetooth discovery."""
        address = discovery_info.address

        await self.async_set_unique_id(address)

        # The abort is silent in the UI, so say why nothing happened
        if any(entry.unique_id == address for entry in self._async_current_entries()):
            _LOGGER.info("Device %s is already configured", address)

        self._abort_if_unique_id_configured()

        # Kept as soon as it arrives: the manual step reads the serial number
        # out of the advertisement when nothing recognised the station
        self._discovery_info = discovery_info

        # Get device type
        identified = identify_from_bt_name(discovery_info.name)
        self._encryption_assumed = identified is not None

        if identified is not None:
            dev_type, full_name, use_encryption = identified

            _LOGGER.info(
                "Device identified from bluetooth name as %s (using encryption: %s)",
                dev_type,
                use_encryption,
            )
        else:
            recognized = await recognize_device(address, self.hass.loop.create_future)

            if recognized is None:
                # Nothing recognised it, which is no reason to refuse the
                # device. Ask the user what it is instead of giving up.
                return await self.async_step_device_type()

            dev_type = recognized.name
            full_name = recognized.full_name
            use_encryption = recognized.encrypted

            _LOGGER.info(
                "Device identified as %s with iot module version %s (using encryption: %s)",
                dev_type,
                recognized.iot_version,
                use_encryption,
            )

        discovery_info.manufacturer_data = ManufacturerData(
            dev_type, use_encryption
        ).as_dict
        discovery_info.name = full_name
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_user()

    async def async_step_device_type(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user about a station the automatic recognition missed."""
        discovery_info = self._discovery_info

        if discovery_info is None:
            return self.async_abort(reason="no_unconfigured_devices")

        if user_input is None:
            return self.async_show_form(
                step_id="device_type",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_TYPE): vol.In(selectable_device_types()),
                        vol.Required(CONF_USE_ENCRYPTION, default=True): bool,
                    }
                ),
            )

        dev_type = user_input[CONF_TYPE]
        use_encryption = bool(user_input[CONF_USE_ENCRYPTION])

        discovery_info.manufacturer_data = ManufacturerData(
            dev_type, use_encryption
        ).as_dict
        # The advertised name is still the best source of the serial number, so
        # it is read here rather than thrown away
        discovery_info.name = manual_device_name(dev_type, discovery_info.name)
        # The channel is what the user answered, not what a probe found, which
        # is exactly what the flag records
        self._encryption_assumed = True
        self.context["title_placeholders"] = {"name": discovery_info.name}

        _LOGGER.info(
            "Device selected by the user as %s (using encryption: %s)",
            dev_type,
            use_encryption,
        )

        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle user input."""

        # Handle click on "OK" button
        if user_input is not None:
            discovery_info = self._discovery_info
            address = discovery_info.address
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            name = re.sub("[^A-Z0-9]+", "", discovery_info.name)

            manufacturer_data = ManufacturerData.from_dict(
                discovery_info.manufacturer_data
            )

            data = InitialDeviceConfig(
                address,
                name,
                manufacturer_data.dev_type,
                manufacturer_data.use_encryption,
                self._encryption_assumed,
            )

            optional = OptionalDeviceConfig.from_dict({})

            return self.async_create_entry(
                title=name,
                data={
                    **data.as_dict,
                    **optional.as_dict,
                },
            )

        if not self._discovery_info:
            return self.async_abort(reason="no_unconfigured_devices")

        # The input from this is not used, we use the discovered and known working address
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_ADDRESS,
                    default=self._discovery_info.address,
                ): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
        )

    @staticmethod
    def async_get_options_flow(_) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle a option flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        # The checkbox shows what was detected when the device was added, so a
        # device that answered on the plain channel is offered an unticked box.
        stored = bool(self.config_entry.data.get(CONF_USE_ENCRYPTION, False))
        # Options win over the entry data, so the effective pack interval is the
        # merged one. It has to be read here, before the update below overwrites
        # the entry.
        stored_pack_interval = OptionalDeviceConfig.from_dict(
            {**self.config_entry.data, **self.config_entry.options}
        ).pack_poll_interval
        has_packs = has_battery_packs(self.config_entry.data.get(CONF_TYPE))

        if user_input is not None:
            config = OptionalDeviceConfig.from_dict(user_input)

            reason = config.validate()

            if reason is not None:
                return self.async_abort(reason=reason)

            use_encryption = bool(user_input.get(CONF_USE_ENCRYPTION, stored))

            data = {
                **self.config_entry.data,
                **config.as_dict,
                CONF_USE_ENCRYPTION: use_encryption,
            }

            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=data,
            )

            if use_encryption != stored or (config.pack_poll_interval > 0) != (
                stored_pack_interval > 0
            ):
                # The channel is chosen while the entry is set up, so changing
                # it needs a restart. Switching pack polling between off and on
                # needs one for the same reason: while it is off the pack
                # entities are registered disabled, and only platform setup
                # moves them into view and starts watching the slots. The other
                # options are applied to the running coordinator by the entry's
                # update listener.
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )

            return self.async_create_entry(
                title="",
                data=config.as_dict,
            )

        defaults = OptionalDeviceConfig.from_dict(self.config_entry.data)

        schema = defaults.schema_with(
            {
                vol.Required(CONF_USE_ENCRYPTION, default=stored): bool,
            },
            has_packs=has_packs,
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
        )
