import logging
import asyncio
from adb_shell.adb_device import AdbDeviceTcp

_LOGGER = logging.getLogger(__name__)

class AdbHandler:
    def __init__(self, hass, host):
        self.hass = hass
        self.host = host
        self._device = None
        self._available = False
        self._lock = asyncio.Lock()

    async def connect(self):
        """低版本安卓免密直连逻辑"""
        async with self._lock:
            try:
                self._device = AdbDeviceTcp(self.host, 5555, default_transport_timeout_s=5)
                # connect(rsa_keys=None) 强制不发送授权指纹，适应旧安卓
                self._available = await self.hass.async_add_executor_job(
                    self._device.connect, False, None
                )
                if self._available:
                    _LOGGER.info(f"ADB 免密连接成功: {self.host}")
                return self._available
            except Exception as e:
                self._available = False
                _LOGGER.debug(f"ADB 连接失败 {self.host}: {e}")
                return False

    async def shell(self, cmd):
        if not self._available:
            if not await self.connect(): return None
        try:
            return await self.hass.async_add_executor_job(self._device.shell, cmd)
        except:
            self._available = False
            return None