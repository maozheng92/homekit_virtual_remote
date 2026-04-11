import asyncio
import logging
import os
import time

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.components.media_player.browse_media import (
    BrowseMedia,
    MediaClass,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.script import Script

from .adb_utils import AdbHandler
from .const import *

_LOGGER = logging.getLogger(__name__)

BOOT_GRACE_SECONDS = 120
ACTIVE_KEY_GRACE_SECONDS = 45
ONLINE_WAIT_TIMEOUT_SECONDS = 120
ONLINE_WAIT_INTERVAL_SECONDS = 2

# 标准安卓按键映射
KEY_MAP = {
    CONF_BTN_UP: 19,
    CONF_BTN_DOWN: 20,
    CONF_BTN_LEFT: 21,
    CONF_BTN_RIGHT: 22,
    CONF_BTN_SELECT: 23,
    CONF_BTN_BACK: 4,
    CONF_BTN_INFO: 82,
    CONF_BTN_VOL_UP: 24,
    CONF_BTN_VOL_DOWN: 25,
    CONF_BTN_MUTE: 164,
    CONF_BTN_PLAY_PAUSE: 85,
    CONF_BTN_POWER_ON: 26,
    CONF_BTN_POWER_OFF: 26,
}

# HomeKit 事件映射
HK_KEY_MAP = {
    "arrow_up": CONF_BTN_UP,
    "arrow_down": CONF_BTN_DOWN,
    "arrow_left": CONF_BTN_LEFT,
    "arrow_right": CONF_BTN_RIGHT,
    "select": CONF_BTN_SELECT,
    "back": CONF_BTN_BACK,
    "information": CONF_BTN_INFO,
    "play_pause": CONF_BTN_PLAY_PAUSE,
}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HKVirtualRemote(hass, entry)])


class HKVirtualRemote(RestoreEntity, MediaPlayerEntity):
    """完整 TV 版虚拟遥控器 / 电视实体"""

    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.PLAY_MEDIA
    )

    def __init__(self, hass, entry):
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_has_entity_name = True
        self._attr_name = None

        self._state: MediaPlayerState = MediaPlayerState.OFF
        self._current_source: str | None = None

        self._adb: AdbHandler | None = None
        self._optimistic_until = 0

        self._config: dict = {}
        self._sources: list[dict] = []

        self._ip: str | None = None
        self._mode: str = MODE_ACTION
        self._power_on_entity: str | None = None
        self._power_sensor: str | None = None
        self._binary_sensor: str | None = None

        self._scripts: dict[str, Script] = {}

        self._reload()

    # ========== 媒体信息属性 ==========
    @property
    def media_title(self):
        """媒体标题 = 当前输入源"""
        return self._current_source

    @property
    def media_content_id(self):
        """媒体内容 ID = 当前输入源"""
        return self._current_source

    @property
    def media_content_type(self):
        return "channel"

    @property
    def media_playback_state(self):
        """媒体状态 = playing / idle / off"""
        if self._state == MediaPlayerState.OFF:
            return MediaPlayerState.OFF
        if not self._current_source:
            return MediaPlayerState.IDLE
        return MediaPlayerState.PLAYING

    # ========== 源列表 ==========
    @property
    def source_list(self):
        return [s[CONF_SOURCE_NAME] for s in self._sources]

    @property
    def source(self):
        return self._current_source

    # ========== 设备信息 ==========
    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=self._entry.title,
            manufacturer="HomeKit Virtual Remote",
            model="Virtual TV",
        )

    # ========== 内部加载配置 ==========
    def _reload(self):
        """重新加载配置与脚本"""
        self._config = {**self._entry.data, **self._entry.options}
        self._ip = self._config.get(CONF_DEVICE_IP)
        self._mode = self._config.get(CONF_MODE, MODE_ACTION)
        self._sources = self._config.get(CONF_SOURCES, []) or []
        self._power_on_entity = self._config.get(CONF_POWER_ON_ENTITY)
        self._power_sensor = self._config.get(CONF_POWER_SENSOR)
        self._binary_sensor = self._config.get(CONF_BINARY_SENSOR)

        # ADB 初始化
        if self._mode == MODE_ADB and self._ip:
            self._adb = AdbHandler(self.hass, self._ip)
        else:
            self._adb = None

        self._scripts = {}

        # 1. 加载标准按键脚本（Action 模式）
        for key in KEY_MAP.keys():
            actions = self._config.get(key, [])
            if actions:
                self._scripts[key] = Script(
                    self.hass, actions, f"{self._attr_unique_id}_{key}", DOMAIN
                )

        # 2. 加载自定义输入源的脚本
        for src in self._sources:
            sid = src.get(CONF_SOURCE_ID)
            actions = self._config.get(sid)
            if actions and sid:
                self._scripts[sid] = Script(
                    self.hass, actions, f"{self._attr_unique_id}_{sid}", DOMAIN
                )

    def _script_context(self):
        return getattr(self, "_context", None)

    def _set_boot_grace(self, seconds=BOOT_GRACE_SECONDS):
        self._optimistic_until = max(self._optimistic_until, time.time() + seconds)
        self._state = MediaPlayerState.ON
        self.async_write_ha_state()

    # ========== 状态轮询 ==========
    async def async_update(self):
        now = time.time()

        # 同步 input_select 状态
        input_select = self._config.get(CONF_INPUT_SELECT_SOURCE)
        if input_select:
            state = self.hass.states.get(input_select)
            if state:
                value = state.state
                if value in [s[CONF_SOURCE_NAME] for s in self._sources]:
                    self._current_source = value

        # ADB 自动重连
        if self._mode == MODE_ADB and self._adb:
            if not getattr(self._adb, "_available", False):
                await self._adb.connect()

        actual_on = await self._is_device_online()

        # 乐观锁逻辑
        if now < self._optimistic_until:
            self._state = MediaPlayerState.ON
            return

        self._state = MediaPlayerState.ON if actual_on else MediaPlayerState.OFF

    async def _is_device_online(self):
        """多维在线检测"""
        # 1. 功率传感器
        if self._power_sensor:
            p_state = self.hass.states.get(self._power_sensor)
            try:
                if p_state and float(p_state.state) > 1.5:
                    return True
            except (TypeError, ValueError):
                pass

        # 2. 二进制传感器
        if self._binary_sensor:
            b_state = self.hass.states.get(self._binary_sensor)
            if b_state and b_state.state.lower() == "on":
                return True

        # 3. 斐讯 API
        if self._mode == MODE_PHICOMM and self._ip:
            try:
                session = async_get_clientsession(self.hass)
                async with session.get(
                    f"http://{self._ip}:8080/v1/status", timeout=0.5
                ) as r:
                    return r.status == 200
            except Exception as err:
                _LOGGER.debug("斐讯状态检测失败 %s: %s", self._ip, err)

        # 4. ADB 状态
        elif self._mode == MODE_ADB and self._adb:
            return getattr(self._adb, "_available", False)

        # 5. Ping 兜底
        elif self._ip:
            res = await self.hass.async_add_executor_job(
                os.system, f"ping -c 1 -W 0.5 {self._ip} > /dev/null 2>&1"
            )
            return res == 0

        return False

    async def _async_wait_until_online(self, timeout=ONLINE_WAIT_TIMEOUT_SECONDS):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await self._is_device_online():
                return True
            await asyncio.sleep(ONLINE_WAIT_INTERVAL_SECONDS)
        return await self._is_device_online()

    # ========== 按键执行 ==========
    async def _run(self, key):
        if not key:
            return

        # 1. Action 脚本
        if key in self._scripts:
            await self._scripts[key].async_run(context=self._script_context())
            return

        code = KEY_MAP.get(key)
        if not code:
            return

        if key not in {CONF_BTN_POWER_ON, CONF_BTN_POWER_OFF} and self._mode in {
            MODE_PHICOMM,
            MODE_ADB,
        }:
            if not await self._async_wait_until_online():
                _LOGGER.warning(
                    "设备未上线，跳过按键发送: %s key=%s", self._entry.title, key
                )
                return

        # 2. 斐讯 API
        if self._mode == MODE_PHICOMM and self._ip:
            try:
                session = async_get_clientsession(self.hass)
                payload = {"keycode": code, "longclick": False}
                await session.post(
                    f"http://{self._ip}:8080/v1/keyevent", json=payload, timeout=2
                )
            except Exception as err:
                _LOGGER.warning(
                    "斐讯按键发送失败 %s key=%s err=%s", self._ip, key, err
                )

        # 3. ADB
        elif self._mode == MODE_ADB and self._adb:
            await self._adb.shell(f"input keyevent {code}")

    async def _async_turn_on_power_entity(self):
        if not self._power_on_entity:
            return False

        state = self.hass.states.get(self._power_on_entity)
        if state is None:
            _LOGGER.warning("开机实体不存在: %s", self._power_on_entity)
            return False

        try:
            await self.hass.services.async_call(
                "homeassistant",
                "turn_on",
                {"entity_id": self._power_on_entity},
                blocking=True,
                context=self._script_context(),
            )
            return True
        except Exception as err:
            _LOGGER.warning("开机实体调用失败 %s: %s", self._power_on_entity, err)
            return False

    # ========== 输入源切换 ==========
    async def async_select_source(self, source):
        """核心：切换输入源（包含 App 和 自定义动作）"""
        src = next((s for s in self._sources if s[CONF_SOURCE_NAME] == source), None)
        if not src:
            return

        sid = src.get(CONF_SOURCE_ID)
        self._set_boot_grace(ACTIVE_KEY_GRACE_SECONDS)

        # 自定义脚本源
        if sid in self._scripts:
            _LOGGER.debug("执行自定义源动作: %s", source)
            await self._scripts[sid].async_run(context=self._script_context())

        # 应用 / 包名源
        elif self._ip and sid:
            if self._mode in {MODE_PHICOMM, MODE_ADB} and not await self._async_wait_until_online():
                _LOGGER.warning(
                    "设备未上线，跳过输入源切换: %s source=%s", self._entry.title, source
                )
                return

            if self._mode == MODE_PHICOMM:
                pkg, act = sid.split("/", 1) if "/" in sid else (sid, "")
                try:
                    session = async_get_clientsession(self.hass)
                    payload = {"package": pkg, "activity": act}
                    await session.post(
                        f"http://{self._ip}:8080/v1/application",
                        json=payload,
                        timeout=5,
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "斐讯启动应用失败 %s sid=%s err=%s", self._ip, sid, err
                    )
            elif self._mode == MODE_ADB and self._adb:
                pkg = sid.split("/")[0]
                await self._adb.shell(
                    f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1"
                )

        self._current_source = source
        self.async_write_ha_state()

    # ========== 开关 ==========
    async def async_turn_on(self):
        actual_on = await self._is_device_online()
        self._set_boot_grace()
        if not actual_on and await self._async_turn_on_power_entity():
            return
        await self._run(CONF_BTN_POWER_ON)

    async def async_turn_off(self):
        self._optimistic_until = 0
        await self._run(CONF_BTN_POWER_OFF)
        self._state = MediaPlayerState.OFF
        self.async_write_ha_state()

    # ========== 截图 ==========
    async def async_get_media_image(self):
        """斐讯模式支持截图"""
        if self._state != MediaPlayerState.ON or self._mode != MODE_PHICOMM or not self._ip:
            return None, None
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(f"http://{self._ip}:8080/v1/screenshot", timeout=3) as r:
                if r.status == 200:
                    return await r.read(), "image/jpeg"
        except Exception as err:
            _LOGGER.debug("截图获取失败 %s: %s", self._ip, err)
        return None, None

    # ========== 音量 / 播放控制 ==========
    async def async_volume_up(self):
        self._set_boot_grace(ACTIVE_KEY_GRACE_SECONDS)
        await self._run(CONF_BTN_VOL_UP)

    async def async_volume_down(self):
        self._set_boot_grace(ACTIVE_KEY_GRACE_SECONDS)
        await self._run(CONF_BTN_VOL_DOWN)

    async def async_mute_volume(self, mute):
        self._set_boot_grace(ACTIVE_KEY_GRACE_SECONDS)
        await self._run(CONF_BTN_MUTE)

    async def async_media_play(self):
        self._set_boot_grace(ACTIVE_KEY_GRACE_SECONDS)
        await self._run(CONF_BTN_PLAY_PAUSE)

    async def async_media_pause(self):
        self._set_boot_grace(ACTIVE_KEY_GRACE_SECONDS)
        await self._run(CONF_BTN_PLAY_PAUSE)

    # ========== 媒体浏览 / 播放 ==========
    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """最简 BrowseMedia：列出所有 source"""
        children = []
        for src in self._sources:
            name = src.get(CONF_SOURCE_NAME)
            if not name:
                continue
            children.append(
                BrowseMedia(
                    title=name,
                    media_class=MediaClass.CHANNEL,
                    media_content_id=name,
                    media_content_type="channel",
                    can_play=True,
                    can_expand=False,
                )
            )

        return BrowseMedia(
            title=self._entry.title,
            media_class=MediaClass.APP,
            media_content_id="root",
            media_content_type="root",
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def async_play_media(self, media_type, media_id, **kwargs):
        """播放媒体：把 media_id 当作 source 名称处理"""
        if media_type not in ("channel", "source"):
            _LOGGER.debug("不支持的 media_type: %s", media_type)
            return
        await self.async_select_source(media_id)

    # ========== 状态 / 事件 ==========
    @property
    def state(self):
        return self._state

    @callback
    async def _handle_hk_event(self, event):
        """处理 HomeKit 物理遥控按键"""
        if event.data.get("entity_id") != self.entity_id:
            return
        self.hass.async_create_task(self._async_handle_hk_event(event))

    async def _async_handle_hk_event(self, event):
        key_name = event.data.get("key_name")
        config_key = HK_KEY_MAP.get(key_name)
        if not config_key:
            return
        self._set_boot_grace(ACTIVE_KEY_GRACE_SECONDS)
        await self._run(config_key)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state:
            try:
                self._state = MediaPlayerState(last_state.state)
            except ValueError:
                pass

            restored_source = last_state.attributes.get("source")
            if restored_source in self.source_list:
                self._current_source = restored_source

        self.async_on_remove(
            self.hass.bus.async_listen(
                "homekit_tv_remote_key_pressed", self._handle_hk_event
            )
        )
        self.async_write_ha_state()
        self.hass.async_create_task(self.async_update())
