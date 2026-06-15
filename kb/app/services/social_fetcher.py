"""Social media content fetcher using Playwright + anti-detection.

Supports:
    - Xiaohongshu (小红书) — Playwright with playwright-stealth + browserforge
    - Weibo (微博) — httpx JSON API first, Playwright fallback

Architecture:
    SocialFetcher is initialized once at startup (in main.py lifespan) and
    holds a long-lived Playwright browser instance. It is NOT thread-safe;
    concurrent requests are handled via Python's async I/O.

Cookie management:
    Cookies are provided as colon-delimited strings via environment variables.
    They are injected into each browser context before navigation.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any

import httpx

from app.models import FetchStatus, SocialContent, SocialImage, SocialPlatform

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL detection helpers
# ---------------------------------------------------------------------------

_XHS_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/(\w+)"),
    re.compile(r"(?:https?://)?xhslink\.com/(\w+)"),
]

_WEIBO_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?weibo\.com/(?:detail/(\d+)|(\d+/\w+))"),
    re.compile(r"(?:https?://)?m\.weibo\.cn/(?:status|detail)/(\d+)"),
]


def detect_social_url(text: str) -> tuple[SocialPlatform | None, str]:
    """Detect and extract a social media URL from arbitrary text.

    Returns (platform, url) or (None, "") if no social URL found.
    """
    for pattern in _XHS_PATTERNS:
        m = pattern.search(text)
        if m:
            return SocialPlatform.XIAOHONGSHU, m.group(0)
    for pattern in _WEIBO_PATTERNS:
        m = pattern.search(text)
        if m:
            return SocialPlatform.WEIBO, m.group(0)
    return None, ""


# ---------------------------------------------------------------------------
# Cookie parsing
# ---------------------------------------------------------------------------


def _parse_cookie_string(cookie_str: str) -> list[dict[str, Any]]:
    """Parse a 'key1=val1; key2=val2' cookie string into Playwright cookie format.

    Each cookie gets a minimal set of domain/path defaults — the domain is
    patched in per-platform before injection.
    """
    if not cookie_str or not cookie_str.strip():
        return []
    result: list[dict[str, Any]] = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            result.append({
                "name": key,
                "value": value,
                "domain": "",   # set per-platform
                "path": "/",
                "httpOnly": False,
                "secure": False,
            })
    return result


# ---------------------------------------------------------------------------
# SocialFetcher
# ---------------------------------------------------------------------------


class SocialFetcher:
    """Playwright-based social media content fetcher.

    Usage:
        async with SocialFetcher(...) as fetcher:
            content = await fetcher.fetch(url, platform)
    """

    def __init__(
        self,
        cookies_xhs: str = "",
        cookies_weibo: str = "",
        fetch_timeout: int = 60,
    ) -> None:
        self._cookies_xhs = _parse_cookie_string(cookies_xhs)
        self._cookies_weibo = _parse_cookie_string(cookies_weibo)
        self._fetch_timeout = fetch_timeout * 1000  # Playwright uses ms
        self._playwright: Any = None
        self._browser: Any = None

    async def start(self) -> None:
        """Start Playwright Chromium (headless, with stealth patches)."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError(
                "playwright is required. Install with: pip install playwright && playwright install chromium"
            )

        self._playwright = await async_playwright().start()
        launch_args: list[str] = [
            "--no-sandbox",
            "--disable-dev-shm-usage",       # ECS/Docker without /dev/shm
            "--disable-blink-features=AutomationControlled",
        ]
        try:
            # Try to use playwright-stealth if available
            import playwright_stealth  # noqa: F401 — patches are auto-applied

            # playwright-stealth patches the browser context automatically;
            # we still launch with automation args suppressed.
            launch_args.append("--disable-features=TranslateUI")
            launch_args.append("--disable-component-extensions-with-background-pages")
        except ImportError:
            logger.warning("playwright-stealth not installed — anti-detection strength reduced")

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=launch_args,
        )
        logger.info("Playwright Chromium started (headless)")

    async def shutdown(self) -> None:
        """Close browser and stop Playwright."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        logger.info("Playwright shut down")

    async def __aenter__(self) -> SocialFetcher:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.shutdown()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(self, url: str, platform: SocialPlatform) -> SocialContent:
        """Fetch content from a social media URL.

        Dispatches to platform-specific fetchers.
        """
        if platform == SocialPlatform.XIAOHONGSHU:
            return await self._fetch_xiaohongshu(url)
        elif platform == SocialPlatform.WEIBO:
            return await self._fetch_weibo(url)
        else:
            return SocialContent(
                url=url, platform=platform,
                fetch_status=FetchStatus.FAILED,
                error=f"Unsupported platform: {platform}",
            )

    # ------------------------------------------------------------------
    # Xiaohongshu
    # ------------------------------------------------------------------

    async def _fetch_xiaohongshu(self, url: str) -> SocialContent:
        """Fetch a Xiaohongshu note."""
        content = SocialContent(url=url, platform=SocialPlatform.XIAOHONGSHU)

        if not self._browser:
            content.fetch_status = FetchStatus.FAILED
            content.error = "Browser not started"
            return content

        # Set domain on cookies
        cookies = [{**c, "domain": ".xiaohongshu.com"} for c in self._cookies_xhs]

        context = None
        page = None
        try:
            context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            if cookies:
                await context.add_cookies(cookies)

            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=self._fetch_timeout)

            # Wait for the note content to render
            try:
                await page.wait_for_selector("#detail-desc", timeout=15000)
            except Exception:
                # Some notes may not have #detail-desc; try alternative selectors
                try:
                    await page.wait_for_selector(".note-content", timeout=10000)
                except Exception:
                    pass

            # Extract title
            try:
                title_el = await page.query_selector("#detail-title")
                if title_el:
                    content.title = (await title_el.inner_text()).strip()
            except Exception as exc:
                logger.debug("XHS title extraction failed: %s", exc)

            # Extract body text
            try:
                desc_el = await page.query_selector("#detail-desc")
                if desc_el:
                    content.text = (await desc_el.inner_text()).strip()
                else:
                    # Fallback: try .note-text or .content
                    text_el = await page.query_selector(".note-text, .note-content .content")
                    if text_el:
                        content.text = (await text_el.inner_text()).strip()
            except Exception as exc:
                logger.debug("XHS body extraction failed: %s", exc)

            # Extract tags
            try:
                tag_els = await page.query_selector_all("#detail-desc a[href*='tag']")
                for tag_el in tag_els:
                    tag_text = (await tag_el.inner_text()).strip().lstrip("#")
                    if tag_text:
                        content.tags.append(tag_text)
            except Exception:
                pass

            # Extract interaction data
            try:
                for label in ("点赞", "收藏", "评论", "分享"):
                    el = await page.query_selector(f"text={label}")
                    if el:
                        # The number is usually in a sibling element
                        parent = await el.evaluate("el => el.closest('.interactions, .like-wrapper')")
                        if parent:
                            pass
                # Simplified: look for numeric spans near known labels
                nums = await page.evaluate("""() => {
                    const items = document.querySelectorAll('.interact-button .count');
                    const result = {};
                    const labels = ['likes', 'collects', 'comments'];
                    items.forEach((el, i) => {
                        if (i < labels.length) result[labels[i]] = parseInt(el.textContent) || 0;
                    });
                    return result;
                }""")
                if isinstance(nums, dict):
                    content.interaction.update(nums)
            except Exception as exc:
                logger.debug("XHS interaction extraction failed: %s", exc)

            # Extract images
            try:
                img_els = await page.query_selector_all(".swiper-slide img, .note-image img")
                for img_el in img_els[:20]:  # limit to 20 images
                    src = await img_el.get_attribute("src")
                    if src and src.startswith("http"):
                        # Download image through the page (with cookies)
                        try:
                            img_bytes = await page.evaluate("""async (src) => {
                                const resp = await fetch(src);
                                const blob = await resp.blob();
                                return Array.from(new Uint8Array(await blob.arrayBuffer()));
                            }""", src)
                            if img_bytes:
                                b64 = base64.b64encode(bytes(img_bytes)).decode()
                                content.images.append(SocialImage(
                                    url=src, base64=b64,
                                ))
                        except Exception:
                            # Image download failed; store URL only
                            content.images.append(SocialImage(url=src))
            except Exception as exc:
                logger.debug("XHS image extraction failed: %s", exc)

            # Extract author
            try:
                author_el = await page.query_selector(".username, .author .name")
                if author_el:
                    content.author_name = (await author_el.inner_text()).strip()
            except Exception:
                pass

            content.fetch_status = FetchStatus.DONE

        except asyncio.TimeoutError:
            content.fetch_status = FetchStatus.FAILED
            content.error = f"Timeout after {self._fetch_timeout}ms"
        except Exception as exc:
            logger.exception("XHS fetch failed for %s", url)
            content.fetch_status = FetchStatus.FAILED
            content.error = str(exc)
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

        return content

    # ------------------------------------------------------------------
    # Weibo
    # ------------------------------------------------------------------

    async def _fetch_weibo(self, url: str) -> SocialContent:
        """Fetch Weibo content. Tries JSON API first, Playwright as fallback."""
        content = SocialContent(url=url, platform=SocialPlatform.WEIBO)

        # --- Path 1: Try mobile JSON API ---
        weibo_id = self._extract_weibo_id(url)
        if weibo_id:
            try:
                api_result = await self._fetch_weibo_api(weibo_id)
                if api_result is not None:
                    return api_result
            except Exception as exc:
                logger.debug("Weibo JSON API failed: %s, falling back to Playwright", exc)

        # --- Path 2: Playwright fallback ---
        if self._browser:
            return await self._fetch_weibo_playwright(url)

        content.fetch_status = FetchStatus.FAILED
        content.error = "Both API and Playwright unavailable"
        return content

    @staticmethod
    def _extract_weibo_id(url: str) -> str:
        """Extract weibo ID from URL."""
        for pattern in _WEIBO_PATTERNS:
            m = pattern.search(url)
            if m:
                for g in m.groups():
                    if g:
                        return str(g)
        return ""

    async def _fetch_weibo_api(self, weibo_id: str) -> SocialContent | None:
        """Fetch via m.weibo.cn JSON API."""
        api_url = f"https://m.weibo.cn/statuses/show?id={weibo_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 Mobile/15E148"
            ),
            "Accept": "application/json",
            "Cookie": self._cookies_weibo[0]["value"] if self._cookies_weibo else "",
        }
        # Convert cookie list to header string
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in self._cookies_weibo
        )
        if cookie_header:
            headers["Cookie"] = cookie_header

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(api_url, headers=headers)
            if resp.status_code != 200:
                return None
            try:
                data = resp.json()
            except Exception:
                return None

        if data.get("ok") != 1:
            return None

        status = data.get("data", {})
        content = SocialContent(
            url=f"https://m.weibo.cn/status/{weibo_id}",
            platform=SocialPlatform.WEIBO,
            fetch_status=FetchStatus.DONE,
        )

        # Text
        raw_text = status.get("text", "")
        # Strip HTML tags
        content.text = re.sub(r"<[^>]+>", "", raw_text).strip()

        # Author
        user = status.get("user", {})
        if isinstance(user, dict):
            content.author_name = user.get("screen_name", "")

        # Time
        created = status.get("created_at", "")
        if created:
            content.publish_time = str(created)

        # Tags from text
        content.tags = re.findall(r"#([^#]+)#", raw_text)

        # Images
        pics = status.get("pics", [])
        if isinstance(pics, list):
            for pic in pics:
                if isinstance(pic, dict) and pic.get("url"):
                    content.images.append(SocialImage(url=pic["url"]))

        # Interaction
        content.interaction = {
            "likes": int(status.get("attitudes_count", 0)),
            "comments": int(status.get("comments_count", 0)),
            "reposts": int(status.get("reposts_count", 0)),
        }

        return content

    async def _fetch_weibo_playwright(self, url: str) -> SocialContent:
        """Fetch Weibo via Playwright (JS-rendering fallback)."""
        content = SocialContent(url=url, platform=SocialPlatform.WEIBO)

        cookies = [{**c, "domain": ".weibo.com"} for c in self._cookies_weibo]
        context = None
        page = None
        try:
            context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 Mobile/15E148"
                ),
            )
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()

            # Use m.weibo.cn for lighter page
            if "m.weibo.cn" not in url:
                weibo_id = self._extract_weibo_id(url)
                if weibo_id:
                    url = f"https://m.weibo.cn/status/{weibo_id}"

            await page.goto(url, wait_until="networkidle", timeout=self._fetch_timeout)

            # Extract text
            try:
                text_el = await page.query_selector(".weibo-text, .card-text, .WB_text")
                if text_el:
                    content.text = (await text_el.inner_text()).strip()
            except Exception:
                pass

            # Extract author
            try:
                author_el = await page.query_selector(".m-text-box .m-text-cut-off, .WB_detail .W_f14")
                if author_el:
                    content.author_name = (await author_el.inner_text()).strip()
            except Exception:
                pass

            content.fetch_status = FetchStatus.DONE

        except asyncio.TimeoutError:
            content.fetch_status = FetchStatus.FAILED
            content.error = f"Timeout after {self._fetch_timeout}ms"
        except Exception as exc:
            logger.exception("Weibo Playwright fetch failed for %s", url)
            content.fetch_status = FetchStatus.FAILED
            content.error = str(exc)
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

        return content
