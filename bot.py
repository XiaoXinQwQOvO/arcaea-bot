import json
import os

import nonebot
import httpx
from nonebot.log import logger

# QQ 适配器需要 HTTP 客户端 + WebSocket 客户端驱动；
# 不依赖雨云面板是否填了 DRIVER。
if not os.getenv("DRIVER", "").strip():
    os.environ["DRIVER"] = "~aiohttp"
from nonebot.adapters.qq import Adapter as QQAdapter

# QQ 官方机器人只填 AppID + AppSecret 两个短环境变量，
# 避免雨云环境变量值 100 字符限制。
_appid = os.getenv("QQ_APPID", "").strip()
_secret = os.getenv("QQ_SECRET", "").strip()
if _appid and _secret and not os.getenv("QQ_BOTS"):
    os.environ["QQ_BOTS"] = json.dumps(
        [
            {
                "id": _appid,
                "secret": _secret,
                "intent": {"c2c_group_at_messages": True},
            }
        ],
        separators=(",", ":"),
    )

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(QQAdapter)
try:
    nonebot.load_plugin("arcaea")
except Exception:
    logger.exception("arcaea 插件加载失败")
    raise

def _log_public_ip() -> None:
    for url in (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://ipv4.icanhazip.com",
    ):
        try:
            ip = httpx.get(url, timeout=5, trust_env=False).text.strip()
        except Exception:
            continue
        if ip:
            logger.info(f"公网出口 IP：{ip} —— 请加入 QQ 开放平台 IP 白名单")
            return
    logger.warning("获取公网出口 IP 失败，请手动查询并加入 QQ 开放平台白名单")


if __name__ == "__main__":
    _log_public_ip()
    nonebot.run()
