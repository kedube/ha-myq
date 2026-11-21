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
]
DEFAULT_POLL_INTERVAL = 60
CONF_TOKENS = "tokens"
CONF_POLL_INTERVAL = "poll_interval"
# Below a minute every full poll's reads double; the app polls only while a screen is open.
MIN_POLL_INTERVAL = 60
CONF_ENABLE_CAMERAS = "enable_cameras"
