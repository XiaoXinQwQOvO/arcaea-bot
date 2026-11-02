FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \n    && python -c "import nonebot, nonebot.adapters.qq, nonebot_plugin_alconna, fastapi, uvicorn, websockets, httpx, jinja2, PIL, playwright; print('deps ok')"

COPY bot.py .
COPY arcaea /app/arcaea

CMD ["python", "bot.py"]
