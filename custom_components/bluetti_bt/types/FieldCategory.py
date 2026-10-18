from homeassistant.const import EntityCategory
from bluetti_bt_lib import FieldName


DIAGNOSTICS = [
    FieldName.DEVICE_SN,
    FieldName.DEVICE_TYPE,
    FieldName.VER_ARM,
    FieldName.VER_DSP,
    FieldName.VER_BMS,
    FieldName.PACK_CELL_VOLTAGES,
]

CONFIGS = [
    FieldName.BATTERY_SOC_RANGE_END,
    FieldName.BATTERY_SOC_RANGE_START,
    FieldName.CTRL_CHARGING_MODE,
    FieldName.CTRL_DISPLAY_TIMEOUT,
    FieldName.CTRL_ECO,
    FieldName.CTRL_ECO_AC,
    FieldName.CTRL_ECO_DC,
    FieldName.CTRL_ECO_MIN_POWER_AC,
    FieldName.CTRL_ECO_MIN_POWER_DC,
    FieldName.CTRL_ECO_TIME_MODE,
    FieldName.CTRL_ECO_TIME_MODE_AC,
    FieldName.CTRL_ECO_TIME_MODE_DC,
    FieldName.CTRL_GRID_CHARGE_ON,
    FieldName.CTRL_POWER_LIFTING,
    FieldName.CTRL_SPLIT_PHASE,
    FieldName.CTRL_TIME_CONTROL_ON,
    FieldName.CTRL_UPS_MODE,
]


def get_category(field: FieldName) -> EntityCategory | None:
    """Category used to group an entity on the Home Assistant device page.

    Home Assistant puts uncategorized switch/number/select entities in the
    "Controls" section, so the power toggles (CTRL_AC, CTRL_DC) deliberately
    have no category here; everything that tunes how a device behaves is
    CONFIG and lands in the "Configuration" section.
    """
    if field in DIAGNOSTICS:
        return EntityCategory.DIAGNOSTIC
    if field in CONFIGS:
        return EntityCategory.CONFIG
    return None
