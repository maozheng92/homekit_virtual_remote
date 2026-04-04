# --- START OF FILE __init__.py ---
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """安装集成入口"""
    await hass.config_entries.async_forward_entry_setups(entry, ["media_player"])
    # 注册更新监听器：当用户在选项页修改配置后，会触发 reload
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """配置更新时重载集成"""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载集成"""
    return await hass.config_entries.async_forward_entry_unload(entry, "media_player")