from homeassistant.const import EntityCategory
from bluetti_bt_lib import FieldName


# Identity, firmware and hardware detail: useful for support, not for daily
# use. These are the same fields on every power station, so the grouping is
# shared rather than repeated per device.
DIAGNOSTICS = [
    FieldName.DEVICE_SN,
    FieldName.DEVICE_TYPE,
    FieldName.WIFI_NAME,
    FieldName.VER_ARM,
    FieldName.VER_DSP,
    FieldName.VER_BMS,
    FieldName.PACK_CELL_VOLTAGES,
    # Split phase wiring is a one-off install setting, not an everyday control.
    FieldName.CTRL_SPLIT_PHASE,
    FieldName.CTRL_SPLIT_PHASE_MODE,
    # Read-only eco thresholds; the eco toggles themselves live in Controls.
    FieldName.CTRL_ECO_MIN_POWER_AC,
    FieldName.CTRL_ECO_MIN_POWER_DC,
]

# Settings that tune how a station behaves. Kept field-keyed so every device
# that declares the field gets the same layout for free.
CONFIGS = [
    FieldName.BATTERY_SOC_RANGE_END,
    FieldName.BATTERY_SOC_RANGE_START,
    FieldName.CTRL_CHARGING_MODE,
    FieldName.CTRL_DISPLAY_TIMEOUT,
    FieldName.CTRL_ECO_TIME_MODE,
    FieldName.CTRL_ECO_TIME_MODE_AC,
    FieldName.CTRL_ECO_TIME_MODE_DC,
    FieldName.CTRL_GRID_CHARGE_ON,
    FieldName.CTRL_TIME_CONTROL_ON,
    FieldName.CTRL_UPS_MODE,
]


def get_category(field: FieldName) -> EntityCategory | None:
    """Category used to group an entity on the Home Assistant device page.

    Home Assistant puts uncategorized switch/number/select entities in the
    "Controls" section, so the power and eco toggles (CTRL_AC, CTRL_DC,
    CTRL_ECO_AC, CTRL_ECO_DC, CTRL_POWER_LIFTING) deliberately have no
    category here; everything that tunes how a device behaves is CONFIG and
    lands in the "Configuration" section.
    """
    if field in DIAGNOSTICS:
        return EntityCategory.DIAGNOSTIC
    if field in CONFIGS:
        return EntityCategory.CONFIG
    return None
