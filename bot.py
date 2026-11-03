import json
import os

# 必须在 import nonebot 之前设置：平台上残留的 DRIVER 空值/旧值
# 会让 nonebot 在导入阶段就落到 NoneDriver，之后再改都无效。
os.environ["DRIVER"] = "~aiohttp"
os.environ.setdefault("HOST", "0.0.0.0")
os.environ.setdefault("PORT", "443")

import nonebot
import httpx
from nonebot.adapters.qq import Adapter as QQAdapter
from nonebot.log import logger

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
            ip = httpx.get(url, timeout=5, trust_env=True).text.strip()
        except Exception:
            continue
        if ip:
            logger.info(f"公网出口 IP：{ip} —— 请加入 QQ 开放平台 IP 白名单")
            return
    logger.warning("获取公网出口 IP 失败，请手动查询并加入 QQ 开放平台白名单")


if __name__ == "__main__":
    _log_public_ip()
    nonebot.run()
