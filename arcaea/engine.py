"""模板渲染 + Playwright 截图。

该模块不依赖 zhenxun / nonebot，供独立渲染服务使用。
调用方只传模板名与已经整理好的上下文数据，服务端负责把模板渲染成图片。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from io import BytesIO
import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from playwright.async_api import Browser, Playwright, async_playwright

logger = logging.getLogger("arcaea_render")

# 只允许渲染白名单中的模板，避免调用方指定任意文件。
TEMPLATES: dict[str, str] = {
    "b50": "b50.html",
    "completion": "completion.html",
    "trend": "trend.html",
    "score": "score.html",
    "history": "history.html",
    "info": "info.html",
    "recent": "recent.html",
}

_FULL_PAGE_VIEWPORT_MAX_WIDTH = 4096
_IMAGE_READY_TIMEOUT_MS = 3_000
_FONT_READY_TIMEOUT_MS = 1_500
_READY_STATE_TIMEOUT_MS = 2_000


class RenderError(Exception):
    """可预期的渲染失败，错误信息会原样返回给调用方。"""


class RenderService:
    """持有浏览器实例，按需渲染模板并返回压缩后的 JPEG。"""

    def __init__(
        self,
        template_dir: Path,
        *,
        concurrency: int = 2,
        screenshot_timeout_ms: int = 120_000,
        max_attempts: int = 2,
        sandbox: bool = False,
    ) -> None:
        self.template_dir = template_dir.resolve()
        self._base_url = self.template_dir.as_uri() + "/"
        self._env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._browser_lock = asyncio.Lock()
        self._screenshot_timeout_ms = screenshot_timeout_ms
        self._max_attempts = max(1, max_attempts)
        self._sandbox = sandbox
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._start_lock = asyncio.Lock()

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        async with self._start_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            if not self.template_dir.is_dir():
                raise RenderError(f"模板目录不存在：{self.template_dir}")
            self._playwright = await async_playwright().start()
            await self._launch_browser()

    async def close(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            with suppress(Exception):
                await browser.close()
        if playwright is not None:
            with suppress(Exception):
                await playwright.stop()

    async def _launch_browser(self) -> None:
        assert self._playwright is not None, "Playwright 尚未启动"
        args = ["--disable-dev-shm-usage", "--font-render-hinting=none"]
        if not self._sandbox:
            # 容器内通常以 root 运行，Chromium 的沙箱无法启用。
            args.append("--no-sandbox")
        self._browser = await self._playwright.chromium.launch(args=args)
        logger.info("Chromium 已启动: %s", self._browser.version)

    async def _ensure_browser(self) -> Browser:
        browser = self._browser
        if browser is not None and browser.is_connected():
            return browser
        async with self._browser_lock:
            browser = self._browser
            if browser is not None and browser.is_connected():
                return browser
            if browser is not None:
                with suppress(Exception):
                    await browser.close()
            await self._launch_browser()
            assert self._browser is not None
            return self._browser

    @property
    def browser_connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    def template_name(self, template: str) -> str:
        name = TEMPLATES.get(template, template)
        if name not in TEMPLATES.values():
            raise RenderError(f"不支持的模板：{template}")
        return name

    # ---------------------------------------------------------------- 渲染入口

    async def render(
        self,
        template: str,
        context: dict[str, Any],
        *,
        width: int = 1700,
        output_width: int = 1280,
        quality: int = 90,
        max_bytes: int = 4_500_000,
        device_scale_factor: int = 1,
        full_page: bool = True,
    ) -> bytes:
        await self.start()
        template_name = self.template_name(template)
        html = self._render_html(template_name, context)

        async with self._semaphore:
            png = await self._capture(
                html,
                width=width,
                device_scale_factor=device_scale_factor,
                full_page=full_page,
            )

        return await asyncio.to_thread(_shrink, png, output_width, quality, max_bytes)

    def _render_html(self, template_name: str, context: dict[str, Any]) -> str:
        try:
            template = self._env.get_template(template_name)
        except Exception as exc:
            raise RenderError(f"模板加载失败：{exc}") from exc
        try:
            return template.render(**context)
        except Exception as exc:
            raise RenderError(f"模板渲染失败：{exc}") from exc

    async def _capture(
        self,
        html: str,
        *,
        width: int,
        device_scale_factor: int,
        full_page: bool,
    ) -> bytes:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            browser = await self._ensure_browser()
            page = None
            try:
                page = await browser.new_page(
                    viewport={"width": width, "height": 10},
                    device_scale_factor=max(1, min(2, device_scale_factor)),
                )
                # 先访问模板目录，让模板内的相对资源（style.css、../resources）
                # 以 file:// 为基准解析，然后再注入渲染后的 HTML。
                await page.goto(self._base_url, wait_until="commit")
                await page.set_content(html, wait_until="domcontentloaded")
                await self._wait_for_assets(page)
                await self._optimize_full_page_capture(page, full_page)
                return await page.screenshot(
                    type="png",
                    full_page=full_page,
                    timeout=self._screenshot_timeout_ms,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "截图失败（第 %s/%s 次）：%s", attempt, self._max_attempts, exc
                )
                if attempt >= self._max_attempts:
                    break
                await asyncio.sleep(0.5)
            finally:
                if page is not None:
                    with suppress(Exception):
                        await page.close()

        raise RenderError(f"截图失败：{last_error}")

    # ---------------------------------------------------------------- 页面等待

    async def _wait_for_assets(self, page: Any) -> None:
        with suppress(Exception):
            await page.wait_for_function(
                "() => document.readyState === 'complete'",
                timeout=_READY_STATE_TIMEOUT_MS,
            )
        with suppress(Exception):
            await page.wait_for_function(
                "() => Array.from(document.images || []).every(img => img.complete)",
                timeout=_IMAGE_READY_TIMEOUT_MS,
            )
        with suppress(Exception):
            await page.evaluate(
                """
                async (timeoutMs) => {
                    if (!document.fonts || !document.fonts.ready) return;
                    await Promise.race([
                        document.fonts.ready,
                        new Promise(resolve => setTimeout(resolve, timeoutMs)),
                    ]);
                }
                """,
                _FONT_READY_TIMEOUT_MS,
            )

    async def _optimize_full_page_capture(self, page: Any, full_page: bool) -> None:
        """按内容高度调整视口，避免超长页面截图被裁切。"""
        if not full_page:
            return
        viewport = page.viewport_size or {}
        width = viewport.get("width")
        height = viewport.get("height")
        if not isinstance(width, int) or width <= 0:
            return
        if not isinstance(height, int) or height <= 0:
            return

        with suppress(Exception):
            content_size = await page.evaluate(
                """
                () => {
                    const body = document.body;
                    const doc = document.documentElement;
                    const bodyWidth = body ? Math.max(
                        body.scrollWidth, body.offsetWidth, body.clientWidth
                    ) : 0;
                    const bodyHeight = body ? Math.max(
                        body.scrollHeight, body.offsetHeight, body.clientHeight
                    ) : 0;
                    const docWidth = doc ? Math.max(
                        doc.scrollWidth, doc.offsetWidth, doc.clientWidth
                    ) : 0;
                    const docHeight = doc ? Math.max(
                        doc.scrollHeight, doc.offsetHeight, doc.clientHeight
                    ) : 0;
                    return {
                        width: Math.ceil(Math.max(bodyWidth, docWidth, 10)),
                        height: Math.ceil(Math.max(bodyHeight, docHeight, 10)),
                    };
                }
                """
            )
        if not isinstance(content_size, dict):
            return
        content_width = content_size.get("width")
        content_height = content_size.get("height")
        if not isinstance(content_width, int) or not isinstance(content_height, int):
            return
        if content_width < 10 or content_height < 10:
            return
        target_width = min(max(width, content_width), _FULL_PAGE_VIEWPORT_MAX_WIDTH)
        target_height = max(height, content_height)
        await page.set_viewport_size({"width": target_width, "height": target_height})


def _resize(image: Image.Image, target_width: int) -> Image.Image:
    if target_width >= image.width:
        return image
    ratio = target_width / image.width
    height = max(1, round(image.height * ratio))
    return image.resize((target_width, height), Image.Resampling.LANCZOS)


def _encode(image: Image.Image, quality: int) -> bytes:
    buffer = BytesIO()
    image.convert("RGB").save(
        buffer, format="JPEG", quality=quality, subsampling=1, progressive=True
    )
    return buffer.getvalue()


def _shrink(
    png_bytes: bytes,
    output_width: int,
    quality: int,
    max_bytes: int,
) -> bytes:
    """缩放到输出宽度，并在超过体积上限时逐步降低 JPEG 质量。"""
    source = Image.open(BytesIO(png_bytes))
    resized = _resize(source, output_width) if output_width < source.width else source
    quality = max(60, min(100, quality))
    data = _encode(resized, quality)
    while len(data) > max_bytes and quality > 60:
        quality -= 10
        data = _encode(resized, quality)
    return data
