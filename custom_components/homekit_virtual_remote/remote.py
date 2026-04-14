import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.components.remote import RemoteEntity, ATTR_NUM_REPEATS
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.components.remote import RemoteEntityFeature
from .entity import HKVRBaseEntity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
):
    """Set up the remote platform."""
    tv = hass.data[DOMAIN][entry.entry_id]["tv"]
    remote = HKVirtualRemoteRemote(tv)
    async_add_entities([remote])

class HKVirtualRemoteRemote(HKVRBaseEntity, RemoteEntity):
    """HomeKit TV Remote (official style)."""

    _attr_name = None  # 官方 remote 都不显示名字
    _attr_supported_features = RemoteEntityFeature.ACTIVITY

    def __init__(self, tv_entity):
        super().__init__(tv_entity._entry)
        self._tv = tv_entity
        self._attr_unique_id = f"{tv_entity._attr_unique_id}_remote"
    
    @property
    def activity_list(self):
        return [
            "arrow_up",
            "arrow_down",
            "arrow_left",
            "arrow_right",
            "select",
            "back",
            "information",
            "play_pause",
        ]

    @property
    def should_poll(self):
        return False

    @property
    def is_on(self):
        """Remote 的开关状态 = TV 的开关状态"""
        return self._tv.state != MediaPlayerState.OFF
        
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                self._tv.entity_id,
                self._handle_tv_state_change
            )
        )

    async def _handle_tv_state_change(self, event):
        self.async_write_ha_state()

    async def async_turn_on_activity(self, activity, **kwargs):
        await self.async_send_command([activity])

    async def async_turn_on(self, **kwargs):
        """遥控器本身不能开机，但 TV 可以"""
        try:
            await self._tv.async_turn_on()
        except Exception as err:
            _LOGGER.debug("Remote turn_on ignored: %s", err)

    async def async_turn_off(self, **kwargs):
        """遥控器本身不能关机，但 TV 可以"""
        try:
            await self._tv.async_turn_off()
        except Exception as err:
            _LOGGER.debug("Remote turn_off ignored: %s", err)

    async def async_send_command(self, command, **kwargs):
        """HomeKit / HA 遥控器按键入口"""
        repeats = kwargs.get(ATTR_NUM_REPEATS, 1)
        for _ in range(repeats):
            await self._tv.async_send_command(command)