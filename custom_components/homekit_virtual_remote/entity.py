from homeassistant.helpers.entity import Entity
from homeassistant.helpers.device_registry import DeviceInfo
from .const import DOMAIN

class HKVRBaseEntity(Entity):
    """Base entity for HomeKit Virtual Remote."""

    _attr_has_entity_name = True

    def __init__(self, entry):
        self._entry = entry
        self._attr_unique_id = entry.entry_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="HomeKit Virtual Remote",
            model="Virtual TV",
            name=entry.title,
        )
