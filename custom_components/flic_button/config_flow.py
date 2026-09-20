"""Config flow for Flic Button integration."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import TYPE_CHECKING, Any, override

import voluptuous as vol
from bleak import BleakError
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_request_active_scan,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from pyflic_ble import (
    DeviceType,
    FlicAuthenticationError,
    FlicPairingError,
    FlicProtocolError,
    PushTwistMode,
)
from pyflic_ble.const import FLIC_SERVICE_UUID, PAIRING_TIMEOUT, TWIST_SERVICE_UUID

from .client import FlicClient
from .const import (
    CONF_DEVICE_TYPE,
    CONF_PAIRING_ID,
    CONF_PAIRING_KEY,
    CONF_PUSH_TWIST_MODE,
    CONF_SERIAL_NUMBER,
    CONF_SIG_BITS,
    DEVICE_TYPE_MODEL_NAMES,
    DOMAIN,
)

if TYPE_CHECKING:
    from . import FlicButtonConfigEntry

_LOGGER = logging.getLogger(__name__)


class FlicButtonConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Flic Button."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._client: FlicClient | None = None
        self._device_type: DeviceType = DeviceType.FLIC2
        self._discovery_task: asyncio.Task[BluetoothServiceInfoBleak] | None = None
        self._pairing_started: bool = False
        self._pairing_task: asyncio.Task | None = None
        self._pairing_stage = "wait_for_pairing"
        self._pairing_error: str | None = None
        self._pairing_data: dict[str, Any] | None = None
        self._removed = False
        self._cleanup_task: asyncio.Task | None = None

    @callback
    @override
    def async_remove(self) -> None:
        """Clean up BLE client and discovery task when the flow is removed."""
        if self._removed:
            return
        self._removed = True
        self._pairing_started = False
        self._pairing_data = None
        if self._discovery_task and not self._discovery_task.done():
            self._discovery_task.cancel()
        task = self._pairing_task
        self._pairing_task = None
        if task and not task.done():
            task.cancel()
        self._cleanup_task = self.hass.async_create_background_task(
            self._async_cleanup_removed_flow(task),
            name=f"{DOMAIN}_config_flow_cleanup",
        )

    async def _async_cleanup_removed_flow(self, task: asyncio.Task | None) -> None:
        """Also cover cancellation before a stage's coroutine starts running."""
        tasks = [item for item in (task, self._discovery_task) if item is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._client:
            await self._async_stop_client(self._client)
            self._client = None

    async def _async_stop_client(self, client: FlicClient) -> None:
        """Stop a BLE client, logging any failure instead of discarding it.

        stop() and not disconnect(): disconnect() leaves the reconnect task
        running and does not set the stopped flag, so the client keeps
        reconnecting to the button after the flow is gone, occupying the only
        connectable adapter and blocking every later pairing attempt.
        """
        try:
            await client.stop()
        except (BleakError, FlicProtocolError, TimeoutError) as err:
            _LOGGER.debug("Error stopping Flic client during cleanup: %s", err)
        except Exception:
            _LOGGER.exception("Unexpected error stopping Flic client")

    @classmethod
    @callback
    @override
    def async_supports_options_flow(cls, config_entry: FlicButtonConfigEntry) -> bool:
        """Only show options for Twist devices."""
        return config_entry.data.get(CONF_DEVICE_TYPE) == DeviceType.TWIST.value

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: FlicButtonConfigEntry,
    ) -> OptionsFlow:
        """Get the options flow for this handler."""
        return FlicButtonOptionsFlowHandler()

    def _is_unconfigured_flic_device(
        self, service_info: BluetoothServiceInfoBleak
    ) -> bool:
        """Check if a discovered BLE device is a Flic button not yet configured."""
        if not self._is_pairing_advertisement(service_info):
            return False
        return service_info.address not in self._async_current_ids(include_ignore=False)

    @staticmethod
    def _is_pairing_advertisement(service_info: BluetoothServiceInfoBleak) -> bool:
        """Recognize the advertised Flic service, not a physical button press.

        Flic 2/Duo advertise 00420000 only in Public mode. Manufacturer data
        0x030f, type 02, flags bit 1 reports an existing physical connection.
        Missing scan-response data is allowed; authentication is still decisive.
        Twist uses its own service; do not infer Flic 2 flags for that model.
        """
        if not service_info.connectable:
            return False
        service_uuids = [str(uuid).lower() for uuid in service_info.service_uuids]
        if TWIST_SERVICE_UUID.lower() in service_uuids:
            return True
        if FLIC_SERVICE_UUID.lower() not in service_uuids:
            return False
        manufacturer = service_info.manufacturer_data.get(0x030F, b"")
        return not (
            len(manufacturer) >= 5 and manufacturer[0] == 2 and manufacturer[4] & 2
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-initiated setup."""
        # If a discovery task is running or finished, handle it first
        if self._discovery_task:
            if not self._discovery_task.done():
                return self.async_show_progress(
                    step_id="user",
                    progress_action="wait_for_discovery",
                    progress_task=self._discovery_task,
                )

            try:
                self._discovery_info = self._discovery_task.result()
            except TimeoutError:
                self._discovery_task = None
                return self.async_abort(reason="no_devices_found")
            finally:
                self._discovery_task = None

            return self.async_show_progress_done(next_step_id="discovery_done")

        # Already found a device — go straight to pairing
        if self._discovery_info is not None:
            return await self._async_set_device_and_pair(
                self._discovery_info, start_pairing=True
            )

        # No device yet — start waiting for one to appear
        self._discovery_task = self.hass.async_create_task(
            self._async_wait_for_flic_device(), eager_start=False
        )

        return self.async_show_progress(
            step_id="user",
            progress_action="wait_for_discovery",
            progress_task=self._discovery_task,
        )

    async def async_step_discovery_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle transition after discovery progress completes."""
        if self._discovery_info is None:
            return self.async_abort(reason="no_devices_found")
        return await self._async_set_device_and_pair(
            self._discovery_info, start_pairing=True
        )

    async def _async_wait_for_flic_device(
        self, address: str | None = None
    ) -> BluetoothServiceInfoBleak:
        """Wait for fresh pairing advertisements, never a stale discovery cache.

        Poll HA's reception cache because identical advertisements can be
        deduplicated before discovery callbacks. Active scanning is bounded to
        this explicitly requested setup attempt and cancelled on every exit.
        """
        started = monotonic()
        scan = self.hass.async_create_task(
            async_request_active_scan(self.hass, PAIRING_TIMEOUT), eager_start=False
        )
        try:
            async with asyncio.timeout(PAIRING_TIMEOUT):
                while True:
                    if scan.done():
                        scan.result()
                    for info in async_discovered_service_info(self.hass):
                        if (
                            info.time >= started
                            and (address is None or info.address == address)
                            and self._is_unconfigured_flic_device(info)
                        ):
                            return info
                    await asyncio.sleep(0.25)
        finally:
            scan.cancel()
            await asyncio.gather(scan, return_exceptions=True)

    async def _async_set_device_and_pair(
        self,
        info: BluetoothServiceInfoBleak,
        *,
        start_pairing: bool = False,
    ) -> ConfigFlowResult:
        """Set discovery info from a found device and proceed to pairing."""
        self._discovery_info = info
        service_uuids = [str(uuid).lower() for uuid in info.service_uuids]

        if TWIST_SERVICE_UUID.lower() in service_uuids:
            self._device_type = DeviceType.TWIST
        else:
            self._device_type = DeviceType.FLIC2

        await self.async_set_unique_id(info.address, raise_on_progress=False)
        self._abort_if_unique_id_configured()

        self.context["title_placeholders"] = {"name": info.name or info.address}

        # When start_pairing is True, skip showing the form and pair immediately
        return await self.async_step_pair({} if start_pairing else None)

    @override
    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        service_uuids = [str(uuid).lower() for uuid in discovery_info.service_uuids]
        _LOGGER.debug(
            "Discovered Bluetooth device during config flow: %s, service_uuids=%s, connectable: %s",
            discovery_info.address,
            service_uuids,
            discovery_info.connectable,
        )
        if TWIST_SERVICE_UUID.lower() in service_uuids:
            self._device_type = DeviceType.TWIST
        else:
            self._device_type = DeviceType.FLIC2

        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle bluetooth confirmation step."""
        if self._discovery_info is None:
            return self.async_abort(reason="no_devices_found")

        self._abort_if_unique_id_configured()

        if user_input is None:
            self._set_confirm_only()
            name = self._discovery_info.name or self._discovery_info.address
            return self.async_show_form(
                step_id="bluetooth_confirm",
                description_placeholders={"name": name},
            )

        # Keep the selected address even if its cached advertisement expired.
        # The pairing stage waits for a fresh signal from this exact button.
        return await self.async_step_pair()

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Drive real, task-backed progress without blocking the form request."""
        if self._removed or self._discovery_info is None:
            return self.async_abort(reason="no_devices_found")

        if self._pairing_task is None:
            if user_input is None:
                return self._show_pairing_form()
            self._pairing_error = None
            self._pairing_data = None
            self._pairing_started = True
            self._start_pairing_stage("wait_for_pairing")

        # HA re-enters this step when a progress task finishes. Refreshes and
        # duplicate submissions must reuse the current task and BLE client.
        if self._pairing_task.done():
            if self._pairing_task.cancelled():
                self._pairing_error = "cannot_connect"
            else:
                self._pairing_task.result()
            if self._pairing_error or self._pairing_stage == "authenticate":
                return self.async_show_progress_done(next_step_id="pair_finish")
            if self._pairing_stage == "wait_for_pairing":
                self._start_pairing_stage("connect")
            else:
                self._start_pairing_stage("authenticate")

        action = self._pairing_stage
        if self._device_type == DeviceType.TWIST:
            # The Public-mode/connection flag interpretation is Flic 2-specific.
            action = {
                "wait_for_pairing": "wait_for_twist",
                "connect": "connect_twist",
            }.get(action, action)
        return self.async_show_progress(
            step_id="pair",
            progress_action=action,
            progress_task=self._pairing_task,
            description_placeholders={
                "name": self._discovery_info.name or self._discovery_info.address
            },
        )

    @callback
    def _start_pairing_stage(self, stage: str) -> None:
        """Start one phase; HA advances the flow when its task completes."""
        self._pairing_stage = stage
        self._pairing_task = self.hass.async_create_task(
            self._async_run_pairing_stage(stage), eager_start=False
        )

    async def _async_run_pairing_stage(self, stage: str) -> None:
        """Own all BLE work and clean up errors/cancellation in every phase."""
        failed = False
        try:
            if stage == "wait_for_pairing":
                self._discovery_info = await self._async_wait_for_flic_device(
                    self._discovery_info.address
                )
            elif stage == "connect":
                self._client = FlicClient(
                    address=self._discovery_info.address,
                    ble_device=self._discovery_info.device,
                    device_type=self._device_type,
                    pairing_only=True,
                )
                await self._client.connect()
            else:
                (
                    pairing_id,
                    pairing_key,
                    serial_number,
                    _,
                    sig_bits,
                    _,
                    _,
                ) = await asyncio.wait_for(
                    self._client.full_verify_pairing(), timeout=PAIRING_TIMEOUT
                )
                final_device_type = (
                    DeviceType.TWIST
                    if self._device_type == DeviceType.TWIST
                    else DeviceType.from_serial_number(serial_number)
                )
                self._pairing_data = {
                    CONF_ADDRESS: self._discovery_info.address,
                    CONF_PAIRING_ID: pairing_id,
                    CONF_PAIRING_KEY: pairing_key.hex(),
                    CONF_SERIAL_NUMBER: serial_number,
                    CONF_DEVICE_TYPE: final_device_type.value,
                    CONF_SIG_BITS: sig_bits,
                }
        except asyncio.CancelledError:
            failed = True
            raise
        except FlicPairingError:
            self._pairing_error = "pairing_failed"
        except FlicAuthenticationError:
            self._pairing_error = "invalid_signature"
        except TimeoutError:
            self._pairing_error = (
                "pairing_mode_timeout"
                if stage == "wait_for_pairing"
                else "cannot_connect"
            )
        except (BleakError, FlicProtocolError):
            self._pairing_error = "cannot_connect"
        except Exception:
            _LOGGER.exception(
                "Unexpected exception during Flic pairing stage %s", stage
            )
            self._pairing_error = "unknown"
        finally:
            if self._pairing_error:
                _LOGGER.warning(
                    "%s: pairing stage %s failed: %s",
                    self._discovery_info.address,
                    stage,
                    self._pairing_error,
                )
            if failed or self._pairing_error or stage == "authenticate":
                if self._client:
                    await self._async_stop_client(self._client)
                    self._client = None
                if failed or self._pairing_error:
                    self._pairing_data = None

    async def async_step_pair_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a retryable error or create the entry only after authentication."""
        if self._pairing_task is None or not self._pairing_task.done():
            return self.async_abort(reason="no_devices_found")
        self._pairing_task = None
        self._pairing_started = False
        if self._pairing_error:
            return self._show_pairing_form(self._pairing_error)
        if self._pairing_data is None:
            return self._show_pairing_form("unknown")
        data = self._pairing_data
        self._pairing_data = None
        model_name = DEVICE_TYPE_MODEL_NAMES[DeviceType(data[CONF_DEVICE_TYPE])]
        return self.async_create_entry(
            title=f"{model_name} ({data[CONF_SERIAL_NUMBER]})", data=data
        )

    @callback
    def _show_pairing_form(self, error: str | None = None) -> ConfigFlowResult:
        """Ask to start listening; never ask users to guess connection state."""
        return self.async_show_form(
            step_id="pair",
            errors={"base": error} if error else {},
            description_placeholders={
                "name": self._discovery_info.name or self._discovery_info.address
            },
        )


class FlicButtonOptionsFlowHandler(OptionsFlow):
    """Handle options flow for Flic Button integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_mode = self.config_entry.options.get(
            CONF_PUSH_TWIST_MODE, PushTwistMode.DEFAULT
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PUSH_TWIST_MODE, default=current_mode
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                PushTwistMode.DEFAULT.value,
                                PushTwistMode.CONTINUOUS.value,
                                PushTwistMode.SELECTOR.value,
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key=CONF_PUSH_TWIST_MODE,
                        )
                    ),
                }
            ),
        )
