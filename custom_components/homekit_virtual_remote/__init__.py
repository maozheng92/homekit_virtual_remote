# --- START OF FILE __init__.py ---
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.const import Platform

from .const import DOMAIN

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.REMOTE]

async def _async_cleanup_stale_registry(hass: HomeAssistant) -> None:
    """清理已删除配置项遗留的实体和设备"""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    valid_entry_ids = {
        entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)
    }

    # 清理无效实体
    stale_entities = [
        entry.entity_id
        for entry in entity_registry.entities.values()
        if entry.platform == DOMAIN and entry.config_entry_id not in valid_entry_ids
    ]
    for entity_id in stale_entities:
        entity_registry.async_remove(entity_id)

    # 清理无效设备
    stale_devices = [
        device.id
        for device in device_registry.devices.values()
        if any(identifier[0] == DOMAIN for identifier in device.identifiers)
        and not (set(device.config_entries) & valid_entry_ids)
    ]
    for device_id in stale_devices:
        device_registry.async_remove_device(device_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """安装集成入口"""
    await _async_cleanup_stale_registry(hass)

    # 加载 media_player + remote
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 配置更新监听器
    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """配置更新时重载集成"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载集成"""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
