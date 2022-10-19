"""Charge service"""
import logging
from datetime import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change
from homeassistant.helpers.event import async_track_utc_time_change

from ..battery.battery_controller import BatteryController
from ..common.unload_controller import UnloadController
from ..forecast.forecast_controller import ForecastController
from ..fox.fox_cloud_service import FoxCloudService

_LOGGER = logging.getLogger(__name__)


class ChargeService(UnloadController):
    """Charge service"""

    def __init__(
        self,
        hass: HomeAssistant,
        battery_controller: BatteryController,
        forecast_controller: ForecastController,
        fox: FoxCloudService,
        eco_start_time: time,
        eco_end_time: time,
        battery_soc: str,
        original_soc: int,
    ) -> None:
        """Init charge service"""
        UnloadController.__init__(self)
        self._hass = hass
        self._battery_controller = battery_controller
        self._forecast_controller = forecast_controller
        self._fox = fox
        self._eco_start_time = eco_start_time
        self._eco_end_time = eco_end_time
        self._battery_soc = battery_soc
        self._original_soc = original_soc
        self._cancel_listeners = []
        self._charge_active = False
        self._perc_target = 0

        # Setup trigger to start when eco period starts
        eco_start = async_track_utc_time_change(
            self._hass,
            self._set_target_soc,
            hour=self._eco_start_time.hour,
            minute=self._eco_start_time.minute - 5,
            second=0,
            local=True,
        )
        self._unload_listeners.append(eco_start)

        # Setup trigger to stop when eco period ends
        eco_end = async_track_utc_time_change(
            self._hass,
            self._stop_hold,
            hour=self._eco_end_time.hour,
            minute=self._eco_end_time.minute,
            second=0,
            local=True,
        )
        self._unload_listeners.append(eco_end)

    async def _set_target_soc(self, *args) -> None:  # pylint: disable=unused-argument
        """Set target SoC"""
        _LOGGER.debug("Calculating optimal battery SoC")

        await self._forecast_controller.async_refresh()

        current_capacity = self._battery_controller.battery_capacity_remaining()
        target_capacity = self._battery_controller.charge_total()

        self._perc_target = self._battery_controller.charge_to_perc()

        if target_capacity > current_capacity:
            _LOGGER.debug(f"Setting force charge to start at {self._eco_start_time}")
            start_charge = async_track_utc_time_change(
                self._hass,
                self._start_force_charge,
                hour=self._eco_start_time.hour,
                minute=self._eco_start_time.minute,
                second=0,
                local=True,
            )
            self._cancel_listeners.append(start_charge)
            self._unload_listeners.append(start_charge)
        else:
            _LOGGER.debug(
                f"Allowing battery to continue discharge until {self._perc_target}"
            )

        # Setup trigger to stop charge when target percentage is met
        track_charge = async_track_state_change(
            self._hass,
            self._battery_soc,
            self._stop_and_hold_charge,
            None,
            str(int(self._perc_target)),
        )
        self._cancel_listeners.append(track_charge)
        self._unload_listeners.append(track_charge)

    async def _start_force_charge(
        self, *args
    ) -> None:  # pylint: disable=unused-argument
        """Initiate force charging"""
        _LOGGER.debug(f"Starting force charge to {self._perc_target}")
        self._charge_active = True
        await self._fox.start_force_charge()

    async def _stop_and_hold_charge(
        self, *args
    ) -> None:  # pylint: disable=unused-argument
        """Battery SoC has met desired percentage"""
        # Stop charging if active
        if self._charge_active:
            _LOGGER.debug("Stopping force charge")
            self._charge_active = False
            await self._fox.stop_force_charge()

        _LOGGER.debug(f"Holding charge at {self._perc_target}")
        await self._fox.set_min_soc(self._perc_target)

        # Reset boost/full status for the next day
        self._battery_controller.set_boost(False)
        self._battery_controller.set_full(False)

    async def _stop_hold(self, *args) -> None:  # pylint: disable=unused-argument
        """Stop holding SoC"""
        # Stop listening for updates
        for listener in self._cancel_listeners:
            listener()

        _LOGGER.debug("Releasing SoC hold")

        await self._fox.set_min_soc(self._original_soc * 100)
