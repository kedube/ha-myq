"""Home Assistant integration constants."""

from homeassistant.const import Platform

DOMAIN = "myq"
PLATFORMS = [
    Platform.COVER,
    Platform.CAMERA,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.LIGHT,
    Platform.SELECT,
    Platform.LOCK,
    Platform.CLIMATE,
    Platform.BUTTON,
    Platform.UPDATE,
    Platform.EVENT,
    Platform.MEDIA_PLAYER,
]
DEFAULT_POLL_INTERVAL = 60
CONF_TOKENS = "tokens"
CONF_POLL_INTERVAL = "poll_interval"
# Below a minute every full poll's reads double; the app polls only while a screen is open.
MIN_POLL_INTERVAL = 60
CONF_ENABLE_CAMERAS = "enable_cameras"
CONF_PANEL_ENABLED = "panel_enabled"
CONF_PANEL_TITLE = "panel_title"
CONF_PANEL_ICON = "panel_icon"
CONF_PANEL_REQUIRE_ADMIN = "panel_require_admin"
DEFAULT_PANEL_TITLE = "MyQ"
DEFAULT_PANEL_ICON = "mdi:garage-variant"
PANEL_OPTION_KEYS = frozenset(
    {CONF_PANEL_ENABLED, CONF_PANEL_TITLE, CONF_PANEL_ICON, CONF_PANEL_REQUIRE_ADMIN}
)
