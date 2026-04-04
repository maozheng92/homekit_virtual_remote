import voluptuous as vol
import json
import logging
import time
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .const import *

_LOGGER = logging.getLogger(__name__)

class HKRemoteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    async def async_step_user(self, user_input=None):
        if user_input:
            return self.async_create_entry(title=user_input["name"], data=user_input, options=user_input)
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required("name", default="客厅电视"): selector.TextSelector(),
            vol.Optional(CONF_DEVICE_IP): selector.TextSelector(),
            vol.Required(CONF_MODE, default=MODE_PHICOMM): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[
                    {"label": "斐讯盒子 (8080)", "value": MODE_PHICOMM},
                    {"label": "ADB 模式 (5555)", "value": MODE_ADB},
                    {"label": "纯脚本模式", "value": MODE_ACTION}
                ], mode=selector.SelectSelectorMode.LIST
            ))
        }))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry): 
        return HKRemoteOptionsFlowHandler(config_entry)

class HKRemoteOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self._config_entry = config_entry
        self.options = {**dict(config_entry.data), **dict(config_entry.options)}

    def _fix_encoding(self, text):
        if not text: return "未知"
        try: return text.encode('iso-8859-1').decode('utf-8')
        except: return text

    async def _update_entry(self):
        final_config = {k: (None if v in ["", [], {}] else v) for k, v in self.options.items()}
        self.hass.config_entries.async_update_entry(self._config_entry, data=final_config, options=final_config)
        return self.async_create_entry(title="", data=final_config)

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["basic_config", "pwr_btn", "nav_btn", "media_btn", "source_config"])

    async def async_step_basic_config(self, user_input=None):
        keys = [CONF_DEVICE_IP, CONF_MODE, CONF_POWER_SENSOR, CONF_BINARY_SENSOR]
        if user_input is not None:
            for key in keys: self.options[key] = user_input.get(key)
            return await self._update_entry()
        return self.async_show_form(step_id="basic_config", data_schema=vol.Schema({
            vol.Optional(CONF_DEVICE_IP, description={"suggested_value": self.options.get(CONF_DEVICE_IP)}): selector.TextSelector(),
            vol.Required(CONF_MODE, default=self.options.get(CONF_MODE, MODE_ACTION)): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"label": "自定义", "value": MODE_ACTION}, {"label": "斐讯", "value": MODE_PHICOMM}, {"label": "ADB", "value": MODE_ADB}],
                mode=selector.SelectSelectorMode.LIST
            )),
            vol.Optional(CONF_POWER_SENSOR, description={"suggested_value": self.options.get(CONF_POWER_SENSOR)}): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_BINARY_SENSOR,
                description={"suggested_value": self.options.get(CONF_BINARY_SENSOR)}
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor")
            ),
        }))

    # 按键配置步骤 (通用)
    async def _manage_btns(self, step_id, keys, user_input):
        if user_input is not None:
            for key in keys: self.options[key] = user_input.get(key)
            return await self._update_entry()
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema({
            vol.Optional(k, description={"suggested_value": self.options.get(k)}): selector.ActionSelector() for k in keys
        }))

    async def async_step_pwr_btn(self, user_input=None): return await self._manage_btns("pwr_btn", [CONF_BTN_POWER_ON, CONF_BTN_POWER_OFF], user_input)
    async def async_step_nav_btn(self, user_input=None): return await self._manage_btns("nav_btn", [CONF_BTN_UP, CONF_BTN_DOWN, CONF_BTN_LEFT, CONF_BTN_RIGHT, CONF_BTN_SELECT, CONF_BTN_BACK, CONF_BTN_INFO], user_input)
    async def async_step_media_btn(self, user_input=None): return await self._manage_btns("media_btn", [CONF_BTN_VOL_UP, CONF_BTN_VOL_DOWN, CONF_BTN_MUTE, CONF_BTN_PLAY_PAUSE], user_input)

    # 输入源配置
    async def async_step_source_config(self, user_input=None):
        if user_input:
            act = user_input.get("action")
            if act == "sync": return await self.async_step_do_sync()
            if act == "add": return await self.async_step_source_add()
            if act == "del": return await self.async_step_source_del()
            return await self.async_step_init()
        sources = self.options.get(CONF_SOURCES, []) or []
        names_str = "\n".join([f"· {s[CONF_SOURCE_NAME]}" for s in sources]) if sources else "暂无"
        return self.async_show_form(step_id="source_config", data_schema=vol.Schema({
            vol.Optional("action", default="back"): selector.SelectSelector(selector.SelectSelectorConfig(options=[
                {"label":"⚡ 同步斐讯 App","value":"sync"}, {"label":"➕ 添加自定义动作源","value":"add"},
                {"label":"🗑️ 删除输入源","value":"del"}, {"label":"⬅️ 返回","value":"back"}
            ], mode=selector.SelectSelectorMode.LIST))
        }), description_placeholders={"current_sources": names_str})

    async def async_step_do_sync(self, user_input=None):
        ip = self.options.get(CONF_DEVICE_IP)
        if not ip: return self.async_abort(reason="no_ip")
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(f"http://{ip}:8080/v1/applist", timeout=5) as r:
                if r.status == 200:
                    data = json.loads(await r.text())
                    current = list(self.options.get(CONF_SOURCES, []) or [])
                    ids = {s.get(CONF_SOURCE_ID) for s in current}
                    for item in data.get("apps", []):
                        sid = f"{item.get('package')}/{item.get('activity','')}"
                        if sid not in ids:
                            current.append({CONF_SOURCE_NAME: self._fix_encoding(item.get("name")), CONF_SOURCE_ID: sid, CONF_SOURCE_ICON: "mdi:android"})
                    self.options[CONF_SOURCES] = current
                    return await self._update_entry()
        except: pass
        return self.async_abort(reason="sync_failed")

    async def async_step_source_add(self, user_input=None):
        """【核心修改】添加源时直接配置动作"""
        if user_input:
            name = user_input["name"]
            # 生成唯一ID用来存储动作脚本
            source_key = f"custom_src_{int(time.time())}"
            srcs = list(self.options.get(CONF_SOURCES, []) or [])
            srcs.append({CONF_SOURCE_NAME: name, CONF_SOURCE_ID: source_key, CONF_SOURCE_ICON: "mdi:script-text-outline"})
            self.options[CONF_SOURCES] = srcs
            self.options[source_key] = user_input.get("actions") # 存储动作序列
            return await self._update_entry()
        
        return self.async_show_form(step_id="source_add", data_schema=vol.Schema({
            vol.Required("name"): selector.TextSelector(),
            vol.Optional("actions"): selector.ActionSelector() # 变成动作选择器了！
        }))

    async def async_step_source_del(self, user_input=None):
        if user_input:
            srcs = list(self.options.get(CONF_SOURCES, []) or [])
            idx = int(user_input["idx"])
            target = srcs.pop(idx)
            self.options.pop(target[CONF_SOURCE_ID], None) # 同时删除关联动作
            self.options[CONF_SOURCES] = srcs
            return await self._update_entry()
        sources = self.options.get(CONF_SOURCES, []) or []
        opts = [{"label": s[CONF_SOURCE_NAME], "value": str(i)} for i, s in enumerate(sources)]
        return self.async_show_form(step_id="source_del", data_schema=vol.Schema({vol.Required("idx"): selector.SelectSelector(selector.SelectSelectorConfig(options=opts))}))