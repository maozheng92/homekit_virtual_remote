import json
import logging
import time

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import *

_LOGGER = logging.getLogger(__name__)

MODE_LABELS = {
    MODE_ACTION: "自定义",
    MODE_PHICOMM: "斐讯",
    MODE_ADB: "ADB",
}

def _entry_title(config):
    name = (config.get("name") or "客厅电视").strip()
    mode = config.get(CONF_MODE, MODE_ACTION)
    mode_label = MODE_LABELS.get(mode, mode)
    return f"{name}（{mode_label}）" if mode_label else name

def _normalize_text(value):
    return value.strip() if isinstance(value, str) else value

def _mode_requires_device_ip(mode):
    return mode in {MODE_PHICOMM, MODE_ADB}

def _validate_device_ip(mode, device_ip):
    return not _mode_requires_device_ip(mode) or bool(_normalize_text(device_ip))


class HKRemoteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input:
            user_input = dict(user_input)
            user_input["name"] = _normalize_text(user_input.get("name")) or "客厅电视"
            user_input[CONF_DEVICE_IP] = _normalize_text(user_input.get(CONF_DEVICE_IP))
            mode = user_input.get(CONF_MODE, MODE_ACTION)

            if not _validate_device_ip(mode, user_input.get(CONF_DEVICE_IP)):
                errors["base"] = "device_ip_required"
            else:
                return self.async_create_entry(
                    title=_entry_title(user_input),
                    data=user_input,
                    options=user_input,
                )

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("name", default="客厅电视"): selector.TextSelector(),
                    vol.Optional(CONF_DEVICE_IP): selector.TextSelector(),
                    vol.Required(CONF_MODE, default=MODE_PHICOMM): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"label": "斐讯盒子 (8080)", "value": MODE_PHICOMM},
                                {"label": "ADB 模式 (5555)", "value": MODE_ADB},
                                {"label": "纯脚本模式", "value": MODE_ACTION},
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HKRemoteOptionsFlowHandler(config_entry)


class HKRemoteOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self._config_entry = config_entry
        self.options = {**dict(config_entry.data), **dict(config_entry.options)}

    def _fix_encoding(self, text):
        if not text:
            return "未知"
        try:
            return text.encode("iso-8859-1").decode("utf-8")
        except Exception:
            return text

    async def async_step_source_edit(self, user_input=None):
        idx = self._edit_index
        srcs = list(self.options.get(CONF_SOURCES, []) or [])
        target = srcs[idx]
        sid = target[CONF_SOURCE_ID]

        if user_input is not None:
            # 保存动作
            self.options[sid] = user_input.get("actions")
            return await self._update_entry()

        return self.async_show_form(
            step_id="source_edit",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "actions",
                        description={"suggested_value": self.options.get(sid)}
                    ): selector.ActionSelector()
                }
            ),
            description_placeholders={"name": target[CONF_SOURCE_NAME]},
        )

    async def async_step_source_edit_list(self, user_input=None):
        if user_input is not None:
            # 把 UI 字段（HDMI1）映射回 source_id（custom_src_HDMI1）
            for src in self.options.get(CONF_SOURCES, []):
                name = src[CONF_SOURCE_NAME]
                sid = src[CONF_SOURCE_ID]
                if name in user_input:
                    self.options[sid] = user_input[name]

            # 用户点击提交 → 保存
            return await self._update_entry()

        sources = self.options.get(CONF_SOURCES, []) or []

        # 构建动态 schema：每个输入源一个 ActionSelector
        schema = {}

        for src in sources:
            sid = src[CONF_SOURCE_ID]
            name = src[CONF_SOURCE_NAME]

            schema[vol.Optional(
                name,                          # UI 字段 key = HDMI1
                description={"suggested_value": self.options.get(sid)}
            )] = selector.ActionSelector()

        return self.async_show_form(
            step_id="source_edit_list",
            data_schema=vol.Schema(schema),
        )

    async def _update_entry(self):
        final_config = {
            k: (None if v in ["", [], {}] else v)
            for k, v in self.options.items()
        }
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            title=_entry_title(final_config),
            data=final_config,
            options=final_config,
        )
        return self.async_create_entry(title="", data=final_config)

    async def async_step_init(self, user_input=None):
        menu = ["basic_config", "source_config"]
        if self.options.get(CONF_MODE, MODE_ACTION) == MODE_ACTION:
            menu[1:1] = ["pwr_btn", "nav_btn", "media_btn"]
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_basic_config(self, user_input=None):
        current_mode = self.options.get(CONF_MODE, MODE_ACTION)
        current_sub = self.options.get(CONF_SUBMODE, SUBMODE_POWER)

        keys = [
            CONF_DEVICE_IP,
            CONF_POWER_ON_ENTITY,
            CONF_MODE,
            CONF_SUBMODE,
            CONF_POWER_SENSOR,
            CONF_BINARY_SENSOR,
            CONF_INPUT_SELECT_SOURCE,
        ]

        errors = {}

        # ========== 用户提交 ==========
        if user_input is not None:
            user_input = dict(user_input)
            device_ip = _normalize_text(user_input.get(CONF_DEVICE_IP))
            next_mode = user_input.get(CONF_MODE, current_mode)
            next_sub = user_input.get(CONF_SUBMODE, current_sub)

            if not _validate_device_ip(next_mode, device_ip):
                errors["base"] = "device_ip_required"
            else:
                user_input[CONF_DEVICE_IP] = device_ip

                # 一级模式不是自定义 → 清空所有传感器
                if next_mode != MODE_ACTION:
                    user_input[CONF_SUBMODE] = None
                    user_input[CONF_POWER_SENSOR] = None
                    user_input[CONF_BINARY_SENSOR] = None
                else:
                    # 自定义模式下的二选一逻辑
                    if next_sub == SUBMODE_POWER:
                        user_input[CONF_BINARY_SENSOR] = None
                    elif next_sub == SUBMODE_BINARY:
                        user_input[CONF_POWER_SENSOR] = None

                for key in keys:
                    self.options[key] = user_input.get(key)

                return await self._update_entry()

        # ========== 构建 UI ==========
        schema = {
            vol.Optional(
                CONF_DEVICE_IP,
                description={"suggested_value": self.options.get(CONF_DEVICE_IP)},
            ): selector.TextSelector(),

            vol.Optional(
                CONF_POWER_ON_ENTITY,
                description={"suggested_value": self.options.get(CONF_POWER_ON_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["switch", "script", "scene", "input_boolean"]
                )
            ),

            vol.Optional(
                CONF_INPUT_SELECT_SOURCE,
                description={"suggested_value": self.options.get(CONF_INPUT_SELECT_SOURCE)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="input_select")
            ),

            vol.Required(CONF_MODE, default=current_mode): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"label": "自定义", "value": MODE_ACTION},
                        {"label": "斐讯", "value": MODE_PHICOMM},
                        {"label": "ADB", "value": MODE_ADB},
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }

        # 一级模式 = 自定义 → 显示子模式
        if current_mode == MODE_ACTION:

            schema[vol.Required(
                CONF_SUBMODE,
                default=current_sub
            )] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"label": "功率传感器（可选）", "value": SUBMODE_POWER},
                        {"label": "传感器（可选）", "value": SUBMODE_BINARY},
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            )

            # 子模式 = 功率传感器
            if current_sub == SUBMODE_POWER:
                schema[vol.Optional(
                    CONF_POWER_SENSOR,
                    description={"suggested_value": self.options.get(CONF_POWER_SENSOR)},
                )] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                )

            # 子模式 = binary_sensor
            if current_sub == SUBMODE_BINARY:
                schema[vol.Optional(
                    CONF_BINARY_SENSOR,
                    description={"suggested_value": self.options.get(CONF_BINARY_SENSOR)},
                )] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                )

        return self.async_show_form(
            step_id="basic_config",
            errors=errors,
            data_schema=vol.Schema(schema),
        )

    # 按键配置
    async def _manage_btns(self, step_id, keys, user_input):
        if user_input is not None:
            for key in keys:
                self.options[key] = user_input.get(key)
            return await self._update_entry()

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        k, description={"suggested_value": self.options.get(k)}
                    ): selector.ActionSelector()
                    for k in keys
                }
            ),
        )

    async def async_step_pwr_btn(self, user_input=None):
        return await self._manage_btns(
            "pwr_btn", [CONF_BTN_POWER_ON, CONF_BTN_POWER_OFF], user_input
        )

    async def async_step_nav_btn(self, user_input=None):
        return await self._manage_btns(
            "nav_btn",
            [
                CONF_BTN_UP,
                CONF_BTN_DOWN,
                CONF_BTN_LEFT,
                CONF_BTN_RIGHT,
                CONF_BTN_SELECT,
                CONF_BTN_BACK,
                CONF_BTN_INFO,
            ],
            user_input,
        )

    async def async_step_media_btn(self, user_input=None):
        return await self._manage_btns(
            "media_btn",
            [CONF_BTN_VOL_UP, CONF_BTN_VOL_DOWN, CONF_BTN_MUTE, CONF_BTN_PLAY_PAUSE],
            user_input,
        )

    # 输入源配置
    async def async_step_source_config(self, user_input=None):
        if user_input:
            act = user_input.get("action")
            if act == "sync":
                return await self.async_step_do_sync()
            if act == "sync_input_select":
                return await self.async_step_sync_input_select()
            if act == "add":
                return await self.async_step_source_add()
            if act == "del":
                return await self.async_step_source_del()
            return await self.async_step_init()

        sources = self.options.get(CONF_SOURCES, []) or []
        names_str = "\n".join([f"· {s[CONF_SOURCE_NAME]}" for s in sources]) if sources else "暂无"

        return self.async_show_form(
            step_id="source_config",
            data_schema=vol.Schema(
                {
                    vol.Optional("action", default="back"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"label": "⚡ 同步斐讯 App", "value": "sync"},
                                {"label": "🔄 同步输入选择", "value": "sync_input_select"},
                                {"label": "➕ 添加自定义动作源", "value": "add"},
                                {"label": "🗑️ 删除输入源", "value": "del"},
                                {"label": "⬅️ 返回", "value": "back"},
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"current_sources": names_str},
        )

    async def async_step_do_sync(self, user_input=None):
        ip = self.options.get(CONF_DEVICE_IP)
        if not ip:
            return self.async_abort(reason="no_ip")
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(f"http://{ip}:8080/v1/applist", timeout=5) as r:
                if r.status == 200:
                    data = json.loads(await r.text())
                    current = list(self.options.get(CONF_SOURCES, []) or [])
                    ids = {s.get(CONF_SOURCE_ID) for s in current}
                    for item in data.get("apps", []):
                        sid = f"{item.get('package')}/{item.get('activity', '')}"
                        if sid not in ids:
                            current.append(
                                {
                                    CONF_SOURCE_NAME: self._fix_encoding(item.get("name")),
                                    CONF_SOURCE_ID: sid,
                                    CONF_SOURCE_ICON: "mdi:android",
                                }
                            )
                    self.options[CONF_SOURCES] = current
                    return await self._update_entry()
        except Exception:
            pass
        return self.async_abort(reason="sync_failed")

    async def async_step_sync_input_select(self, user_input=None):
        if user_input is None:
            # 第一次进入：显示一个空表单（必须有 UI）
            return self.async_show_form(
                step_id="sync_input_select",
                data_schema=vol.Schema({}),
            )

        # 第二次进入：执行同步逻辑
        input_select = self.options.get(CONF_INPUT_SELECT_SOURCE)
        if not input_select:
            return self.async_abort(reason="no_input_select")

        state = self.hass.states.get(input_select)
        if not state:
            return self.async_abort(reason="input_select_not_found")

        options = state.attributes.get("options", [])
        current = list(self.options.get(CONF_SOURCES, []) or [])

        new_indexes = []

        for opt in options:
            sid = f"custom_src_{opt}"
            if not any(s.get(CONF_SOURCE_NAME) == opt for s in current):
                current.append({
                    CONF_SOURCE_NAME: opt,
                    CONF_SOURCE_ID: sid,
                    CONF_SOURCE_ICON: "mdi:script-text-outline"
                })
                new_indexes.append(len(current) - 1)

        self.options[CONF_SOURCES] = current

        return await self.async_step_source_edit_list()


    async def async_step_source_add(self, user_input=None):
        if user_input:
            name = user_input["name"]
            source_key = f"custom_src_{int(time.time())}"
            srcs = list(self.options.get(CONF_SOURCES, []) or [])
            srcs.append(
                {
                    CONF_SOURCE_NAME: name,
                    CONF_SOURCE_ID: source_key,
                    CONF_SOURCE_ICON: "mdi:script-text-outline",
                }
            )
            self.options[CONF_SOURCES] = srcs
            self.options[source_key] = user_input.get("actions")
            return await self._update_entry()

        return self.async_show_form(
            step_id="source_add",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): selector.TextSelector(),
                    vol.Optional("actions"): selector.ActionSelector(),
                }
            ),
        )

    async def async_step_source_del(self, user_input=None):
        if user_input:
            srcs = list(self.options.get(CONF_SOURCES, []) or [])
            idx = int(user_input["idx"])
            target = srcs.pop(idx)
            self.options.pop(target[CONF_SOURCE_ID], None)
            self.options[CONF_SOURCES] = srcs
            return await self._update_entry()

        sources = self.options.get(CONF_SOURCES, []) or []
        opts = [{"label": s[CONF_SOURCE_NAME], "value": str(i)} for i, s in enumerate(sources)]

        return self.async_show_form(
            step_id="source_del",
            data_schema=vol.Schema(
                {
                    vol.Required("idx"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=opts)
                    )
                }
            ),
        )
