import json
import os

import nonebot

# QQ 适配器需要 HTTP 客户端 + WebSocket 客户端驱动；
# 不依赖雨云面板是否填了 DRIVER。
os.environ.setdefault("DRIVER", "~aiohttp")
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
nonebot.load_plugin("arcaea")

if __name__ == "__main__":
    nonebot.run()
