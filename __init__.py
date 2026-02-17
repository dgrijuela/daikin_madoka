"""Platform for the Daikin AC."""
import asyncio
from datetime import timedelta
import logging

# Bleak dropped the module-level discover() in newer versions.
# Provide a shim so older pymadoka continues to import successfully.
import bleak

if not hasattr(bleak, "discover") and hasattr(bleak, "BleakScanner"):
    async def _discover(*args, **kwargs):
        return await bleak.BleakScanner.discover(*args, **kwargs)

    bleak.discover = _discover
    _LOGGER = logging.getLogger(__name__)
    _LOGGER.debug(
        "Applied bleak.discover shim to BleakScanner.discover for compatibility"
    )

# Monkey-patch pymadoka's Connection class to use bleak-retry-connector.
# pymadoka calls raw BleakClient.connect() which is unreliable on modern HA
# (2025.9+). We replace _select_device() and _connect() to use
# establish_connection(BleakClientWithServiceCache, ...) instead.
import pymadoka.connection as _pmconn
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache


class _Sentinel:
    """Truthy sentinel so start()'s `if self.client` check passes."""
    def __bool__(self):
        return True


async def _patched_select_device(self):
    """Find the BLEDevice in the discovery cache without creating a raw BleakClient."""
    for d in _pmconn.DISCOVERED_DEVICES_CACHE:
        if d.address.upper() == self.address.upper():
            self._ble_device = d
            self.name = d.name
            self.client = _Sentinel()
            return
    self.connection_status = _pmconn.ConnectionStatus.ABORTED
    raise ConnectionAbortedError(
        f"Could not find bluetooth device for the address {self.address}. "
        "Please follow the instructions on device pairing."
    )


async def _patched_connect(self):
    """Connect using bleak-retry-connector for reliable BLE connections."""
    try:
        if isinstance(self.client, _Sentinel) or not self.client.is_connected:
            self.client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self.address,
                disconnected_callback=self.on_disconnect,
            )
        if self.client.is_connected:
            _pmconn.logger.info("Connected to %s", self.address)
            self.connection_status = _pmconn.ConnectionStatus.CONNECTED
            await self.client.start_notify(
                _pmconn.NOTIFY_CHAR_UUID, self.notification_handler,
            )
        else:
            _pmconn.logger.info("Failed to connect to %s", self.address)
    except Exception as e:
        if "Software caused connection abort" not in str(e):
            _pmconn.logger.error(e)
        if not self.reconnect:
            raise
        _pmconn.logger.debug("Reconnecting...")


_pmconn.Connection._select_device = _patched_select_device
_pmconn.Connection._connect = _patched_connect

from pymadoka import Controller, discover_devices, force_device_disconnect
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE,
    CONF_DEVICES,
    CONF_FORCE_UPDATE,
    CONF_SCAN_INTERVAL,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant

from . import config_flow  # noqa: F401
from .const import CONF_CONTROLLER_TIMEOUT, CONTROLLERS, DOMAIN, TIMEOUT

PARALLEL_UPDATES = 0
MIN_TIME_BETWEEN_UPDATES = timedelta(seconds=60)

COMPONENT_TYPES = ["climate", "sensor"]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    vol.All(
        cv.deprecated(DOMAIN),
        {
            DOMAIN: vol.Schema(
                {
                    vol.Required(CONF_DEVICES, default=[]): vol.All(
                        cv.ensure_list, [cv.string]
                    ),
                    vol.Optional(CONF_FORCE_UPDATE, default=True): bool,
                    vol.Optional(CONF_DEVICE, default="hci0"): cv.string,
                    vol.Optional(CONF_SCAN_INTERVAL, default=5): cv.positive_int,
                }
            )
        },
    ),
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    """Set up the component."""

    hass.data.setdefault(DOMAIN, {})

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Pass conf to all the components."""

    controllers = {}
    for device in entry.data[CONF_DEVICES]:
        if entry.data[CONF_FORCE_UPDATE]:
            await force_device_disconnect(device)
        controllers[device] = Controller(device, adapter=entry.data[CONF_DEVICE])

    await discover_devices(
        adapter=entry.data[CONF_DEVICE], timeout=entry.data[CONF_SCAN_INTERVAL]
    )

    controller_timeout = entry.data.get(CONF_CONTROLLER_TIMEOUT, TIMEOUT)

    for device, controller in controllers.items():
        try:
            await asyncio.wait_for(controller.start(), timeout=controller_timeout)
        except (ConnectionAbortedError, asyncio.TimeoutError) as exc:
            error_msg = str(exc)
            if any(s in error_msg.lower() for s in [
                "operation already in progress",
                "br-connection-canceled",
                "dbus",
            ]):
                _LOGGER.debug(
                    "Bluetooth stack issue connecting to %s: %s",
                    device,
                    error_msg,
                )
            else:
                _LOGGER.error(
                    "Could not connect to device %s: %s",
                    device,
                    error_msg,
                )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {CONTROLLERS: controllers}
    await hass.config_entries.async_forward_entry_setups(entry, COMPONENT_TYPES)

    return True


async def async_unload_entry(hass, config_entry):
    """Unload a config entry."""
    await asyncio.wait(
        [
            hass.async_create_task(hass.config_entries.async_forward_entry_unload(config_entry, component))
            for component in COMPONENT_TYPES
        ]
    )
    hass.data[DOMAIN].pop(config_entry.entry_id)

    return True
