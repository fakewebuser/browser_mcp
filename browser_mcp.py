"""
browser_mcp.py — A Python MCP server that gives an LLM client (e.g. Claude)
full local agent capabilities:

  BROWSER      — launch Chrome, navigate, click, type, scroll, screenshot
  FILESYSTEM   — read, write, list, delete, move, search files
  SHELL        — run any local command or script
  HUMAN INPUT  — type with realistic human-like timing and typos
  EMAIL        — send email via SMTP (Gmail, Outlook, etc.)
  SQLITE       — persistent structured storage, query with SQL
  MEMORY       — key/value store that persists across sessions
  SCHEDULER    — run tools on a cron schedule, unattended
  NOTIFY       — desktop notifications (macOS/Linux/Windows)

Together these form a full local AI agent: browser finds data, filesystem
stores it, shell processes it, email sends results, scheduler runs it all
on autopilot.

Requirements:
    pip install mcp playwright pypdf apscheduler

    playwright install chrome

Wire into Claude Desktop (Settings → Developer → Edit Config):
    {
      "mcpServers": {
        "browser": {
          "command": "python",
          "args": ["/absolute/path/to/browser_mcp.py"]
        }
      }
    }
"""

import asyncio
import fnmatch
import json
import os
import re
import shutil
import smtplib
import sqlite3
import subprocess
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

try:
    from mcp.server.mcpserver import MCPServer as _MCPServerClass, Image
except ImportError:
    from mcp.server.fastmcp import FastMCP as _MCPServerClass, Image

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

import sentry_sdk

sentry_sdk.init(
      dsn="https://bd695c407e0f008db113d4abc9cd6115@o4512065343782912.ingest.us.sentry.io/4512065354792960",
      send_default_pii=True,
)

mcp = _MCPServerClass("browser-control")

# ---------------------------------------------------------------------------
# Paths — everything lives under ~/.browser_mcp/
# ---------------------------------------------------------------------------
BASE_DIR      = Path.home() / ".browser_mcp"
DOWNLOAD_DIR  = BASE_DIR / "downloads"
MEMORY_FILE   = BASE_DIR / "memory.json"
DB_PATH       = BASE_DIR / "agent.db"


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ensure_download_dir() -> Path:
    return _ensure_dir(DOWNLOAD_DIR)


def _unique_destination(directory: Path, filename: str) -> Path:
    """Avoid clobbering existing files by appending a short random suffix."""
    dest = directory / filename
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    return directory / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


# ---------------------------------------------------------------------------
# Global browser state
# ---------------------------------------------------------------------------
class BrowserState:
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.pages: list[Page] = []
        self.active_index: int = -1
        self.connected_via_cdp: bool = False

    @property
    def page(self) -> Optional[Page]:
        if 0 <= self.active_index < len(self.pages):
            return self.pages[self.active_index]
        return None

    def is_running(self) -> bool:
        return self.page is not None and not self.page.is_closed()

    def prune_closed_pages(self) -> None:
        alive = [p for p in self.pages if not p.is_closed()]
        if len(alive) != len(self.pages):
            active_page = self.page
            self.pages = alive
            if active_page is not None and not active_page.is_closed() and active_page in self.pages:
                self.active_index = self.pages.index(active_page)
            else:
                self.active_index = len(self.pages) - 1


state = BrowserState()


def _require_page() -> Page:
    state.prune_closed_pages()
    if not state.is_running():
        raise RuntimeError("No browser is running. Call launch_browser first.")
    return state.page


# ===========================================================================
# SECTION 1 — BROWSER LIFECYCLE
# ===========================================================================

@mcp.tool()
async def launch_browser(headless: bool = False, use_real_chrome: bool = True) -> str:
    """Launch a Chrome browser instance. Call this before any other browser tool.

    Args:
        headless: If True, Chrome runs invisibly. Default False so you can watch.
        use_real_chrome: If True, uses your installed Chrome. Falls back to
            Playwright's bundled Chromium if Chrome isn't found.
    """
    if state.is_running():
        return "Browser already running. Call close_browser first to restart."

    state.playwright = await async_playwright().start()
    launch_kwargs: dict = {"headless": headless}
    if use_real_chrome:
        launch_kwargs["channel"] = "chrome"

    try:
        state.browser = await state.playwright.chromium.launch(**launch_kwargs)
    except Exception:
        launch_kwargs.pop("channel", None)
        state.browser = await state.playwright.chromium.launch(**launch_kwargs)

    state.context = await state.browser.new_context(viewport={"width": 1440, "height": 900})
    first_page = await state.context.new_page()
    state.pages = [first_page]
    state.active_index = 0
    state.connected_via_cdp = False
    return "Browser launched and ready."


@mcp.tool()
async def connect_to_existing_browser(cdp_url: str = "http://localhost:9222") -> str:
    """Attach to an already-running Chrome instead of launching a new one, so
    you can see and control tabs you already have open — your real profile,
    your logins, your current tab.

    Chrome has to be started with remote debugging enabled first (it won't
    turn this on for a Chrome that's already running without the flag —
    quit Chrome completely first, then relaunch it):

        macOS:   open -a "Google Chrome" --args --remote-debugging-port=9222
        Linux:   google-chrome --remote-debugging-port=9222
        Windows: chrome.exe --remote-debugging-port=9222

    Closing the session afterward (close_browser) disconnects from Chrome
    without quitting it — your browser and tabs stay open exactly as they
    were.

    Args:
        cdp_url: The Chrome DevTools Protocol endpoint. Defaults to the
            standard local debugging port.
    """
    if state.is_running():
        return "A browser session is already active. Call close_browser first if you want to reconnect."

    state.playwright = await async_playwright().start()
    try:
        state.browser = await state.playwright.chromium.connect_over_cdp(cdp_url)
    except Exception as e:
        await state.playwright.stop()
        state.playwright = None
        return (
            f"Could not connect to {cdp_url}: {e}\n"
            "Make sure Chrome was fully quit and relaunched with "
            "--remote-debugging-port=9222 (a Chrome that was already running "
            "before you added the flag won't pick it up)."
        )

    contexts = state.browser.contexts
    state.context = contexts[0] if contexts else await state.browser.new_context()
    state.pages = list(state.context.pages) or [await state.context.new_page()]
    state.active_index = len(state.pages) - 1  # most likely the tab you're looking at
    state.connected_via_cdp = True

    return (
        f"Connected to your existing Chrome at {cdp_url}. "
        f"Found {len(state.pages)} open tab(s) — call list_tabs to see them, "
        f"or get_page_info to check what's currently active."
    )


@mcp.tool()
async def close_browser() -> str:
    """Close the browser session. If this was a launched browser, quits it.
    If this was attached via connect_to_existing_browser, just disconnects —
    your actual Chrome window and tabs stay open."""
    was_cdp = state.connected_via_cdp
    if state.browser:
        await state.browser.close()
    if state.playwright:
        await state.playwright.stop()
    state.playwright = None
    state.browser = None
    state.context = None
    state.pages = []
    state.active_index = -1
    state.connected_via_cdp = False
    return "Disconnected from your Chrome (it's still open)." if was_cdp else "Browser closed."


# ===========================================================================
# SECTION 2 — NAVIGATION
# ===========================================================================

@mcp.tool()
async def navigate(url: str) -> str:
    """Go to a URL. Adds https:// if no scheme is provided.

    Args:
        url: Address to visit, e.g. "wikipedia.org" or "https://example.com".
    """
    page = _require_page()
    if not re.match(r"^\w+://", url):
        url = "https://" + url
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(500)
    title = await page.title()
    return f"Navigated to {page.url} (title: {title!r})"


@mcp.tool()
async def go_back() -> str:
    """Go back in browser history."""
    page = _require_page()
    await page.go_back(wait_until="domcontentloaded")
    return f"Went back. Now at {page.url}"


@mcp.tool()
async def go_forward() -> str:
    """Go forward in browser history."""
    page = _require_page()
    await page.go_forward(wait_until="domcontentloaded")
    return f"Went forward. Now at {page.url}"


@mcp.tool()
async def reload_page() -> str:
    """Reload the current page."""
    page = _require_page()
    await page.reload(wait_until="domcontentloaded")
    return f"Reloaded {page.url}"


@mcp.tool()
async def get_page_info() -> str:
    """Get the current page's URL and title."""
    page = _require_page()
    return f"URL: {page.url}\nTitle: {await page.title()}"


# ===========================================================================
# SECTION 3 — TAB MANAGEMENT
# ===========================================================================

@mcp.tool()
async def new_tab(url: Optional[str] = None) -> str:
    """Open a new browser tab and make it active.

    Args:
        url: Optional URL to load. Opens a blank tab if omitted.
    """
    if state.context is None:
        raise RuntimeError("No browser running. Call launch_browser first.")
    state.prune_closed_pages()
    page = await state.context.new_page()
    state.pages.append(page)
    state.active_index = len(state.pages) - 1
    if url:
        if not re.match(r"^\w+://", url):
            url = "https://" + url
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(500)
    title = await page.title()
    return f"Opened new tab [{state.active_index}] at {page.url} (title: {title!r})"


@mcp.tool()
async def list_tabs() -> str:
    """List all open tabs. The active tab (marked with *) is what other tools act on."""
    state.prune_closed_pages()
    if not state.pages:
        raise RuntimeError("No browser running. Call launch_browser first.")
    lines = []
    for i, page in enumerate(state.pages):
        marker = "*" if i == state.active_index else " "
        title = await page.title()
        lines.append(f"{marker} [{i}] {title!r} -> {page.url}")
    return "\n".join(lines)


@mcp.tool()
async def switch_tab(index: int) -> str:
    """Make the tab at the given index active. All tools then act on it.

    Args:
        index: Tab index from list_tabs.
    """
    state.prune_closed_pages()
    if not (0 <= index < len(state.pages)):
        return f"No tab at index {index}. Call list_tabs to see open tabs."
    state.active_index = index
    page = state.page
    await page.bring_to_front()
    return f"Switched to tab [{index}]: {await page.title()!r} -> {page.url}"


@mcp.tool()
async def close_tab(index: Optional[int] = None) -> str:
    """Close a tab (defaults to the active one).

    Args:
        index: Tab index to close. Defaults to the active tab.
    """
    state.prune_closed_pages()
    if not state.pages:
        raise RuntimeError("No browser running. Call launch_browser first.")
    target = state.active_index if index is None else index
    if not (0 <= target < len(state.pages)):
        return f"No tab at index {target}."
    was_active = target == state.active_index
    await state.pages[target].close()
    del state.pages[target]
    if not state.pages:
        state.active_index = -1
        return "Closed the last tab."
    if was_active:
        state.active_index = min(target, len(state.pages) - 1)
    elif target < state.active_index:
        state.active_index -= 1
    return f"Closed tab [{target}]. Active tab is now [{state.active_index}]: {state.page.url}"


# ===========================================================================
# SECTION 4 — READING THE PAGE
# ===========================================================================

@mcp.tool()
async def get_visible_text(max_chars: int = 6000) -> str:
    """Extract the visible text content of the current page.

    Args:
        max_chars: Truncate output to this many characters.
    """
    page = _require_page()
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    truncated = len(text) > max_chars
    text = text[:max_chars]
    if truncated:
        text += "\n\n...[truncated]"
    return text or "(page has no visible text)"


@mcp.tool()
async def get_page_html(max_chars: int = 8000) -> str:
    """Get the raw HTML of the current page. Useful when structure/attributes matter."""
    page = _require_page()
    html = await page.content()
    truncated = len(html) > max_chars
    html = html[:max_chars]
    if truncated:
        html += "\n\n...[truncated]"
    return html


@mcp.tool()
async def screenshot(full_page: bool = False) -> Image:
    """Take a screenshot of the current page.

    Args:
        full_page: If True, captures the full scrollable page height.
    """
    page = _require_page()
    png_bytes = await page.screenshot(full_page=full_page, type="png")
    return Image(data=png_bytes, format="png")


# ===========================================================================
# SECTION 5 — INTERACTING WITH THE PAGE
# ===========================================================================

_LIST_ELEMENTS_JS = r"""
() => {
    const selector = 'a, button, input, textarea, select, [role="button"], [role="link"], [onclick], summary';
    const nodes = Array.from(document.querySelectorAll(selector));
    const results = [];
    let index = 0;
    for (const el of nodes) {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        const visible = rect.width > 0 && rect.height > 0 &&
            style.visibility !== 'hidden' &&
            style.display !== 'none' &&
            style.opacity !== '0';
        if (!visible) continue;
        el.setAttribute('data-mcp-index', String(index));
        const tag = el.tagName.toLowerCase();
        let label = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim();
        label = label.replace(/\s+/g, ' ').slice(0, 80);
        results.push({
            index: index,
            tag: tag,
            type: el.getAttribute('type') || '',
            label: label,
            href: el.getAttribute('href') || '',
        });
        index += 1;
    }
    return results;
}
"""


@mcp.tool()
async def list_interactive_elements() -> str:
    """List all visible clickable/typable elements on the page, numbered for use
    with click_element and type_text."""
    page = _require_page()
    elements = await page.evaluate(_LIST_ELEMENTS_JS)
    if not elements:
        return "No interactive elements found."
    lines = []
    for el in elements:
        desc = f"[{el['index']}] <{el['tag']}"
        if el["type"]:
            desc += f" type={el['type']}"
        desc += ">"
        if el["label"]:
            desc += f" \"{el['label']}\""
        if el["href"]:
            desc += f" -> {el['href']}"
        lines.append(desc)
    return "\n".join(lines)


@mcp.tool()
async def click_element(index: int) -> str:
    """Click the element with the given index (from list_interactive_elements).

    Args:
        index: Element index from list_interactive_elements.
    """
    page = _require_page()
    locator = page.locator(f'[data-mcp-index="{index}"]')
    if await locator.count() == 0:
        return f"No element at index {index}. Re-run list_interactive_elements."
    await locator.first.click()
    await page.wait_for_timeout(400)
    return f"Clicked [{index}]. URL: {page.url}"


@mcp.tool()
async def click_selector(selector: str) -> str:
    """Click an element by CSS selector.

    Args:
        selector: CSS selector, e.g. "button[type=submit]".
    """
    page = _require_page()
    try:
        await page.locator(selector).first.click()
        await page.wait_for_timeout(400)
        return f"Clicked '{selector}'. URL: {page.url}"
    except Exception as e:
        return f"Could not click '{selector}': {e}"


@mcp.tool()
async def type_text(index: int, text: str, submit: bool = False, clear_first: bool = True) -> str:
    """Type text into an input.

    Args:
        index: Input index from list_interactive_elements.
        text: Text to type.
        submit: If True, presses Enter after typing.
        clear_first: If True, clears the field first.
    """
    page = _require_page()
    locator = page.locator(f'[data-mcp-index="{index}"]')
    if await locator.count() == 0:
        return f"No element at index {index}. Re-run list_interactive_elements."
    if clear_first:
        await locator.first.fill("")
    await locator.first.type(text, delay=15)
    if submit:
        await locator.first.press("Enter")
        await page.wait_for_timeout(600)
    return f"Typed into [{index}]{' and submitted' if submit else ''}."


@mcp.tool()
async def press_key(key: str) -> str:
    """Press a keyboard key, e.g. "Enter", "Escape", "Tab", "ArrowDown".

    Args:
        key: Playwright key name.
    """
    page = _require_page()
    await page.keyboard.press(key)
    await page.wait_for_timeout(200)
    return f"Pressed {key}."


@mcp.tool()
async def scroll(direction: str = "down", amount: int = 600) -> str:
    """Scroll the page.

    Args:
        direction: "up", "down", "top", or "bottom".
        amount: Pixels to scroll for up/down. Ignored for top/bottom.
    """
    page = _require_page()
    if direction == "down":
        await page.mouse.wheel(0, amount)
    elif direction == "up":
        await page.mouse.wheel(0, -amount)
    elif direction == "top":
        await page.evaluate("window.scrollTo(0, 0)")
    elif direction == "bottom":
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    else:
        return f"Unknown direction '{direction}'. Use up, down, top, or bottom."
    await page.wait_for_timeout(200)
    return f"Scrolled {direction}."


@mcp.tool()
async def wait_for_selector(selector: str, timeout_ms: int = 5000) -> str:
    """Wait until an element matching a CSS selector appears (for async content).

    Args:
        selector: CSS selector to wait for.
        timeout_ms: Max wait time in milliseconds.
    """
    page = _require_page()
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
        return f"'{selector}' appeared."
    except Exception as e:
        return f"Timed out waiting for '{selector}': {e}"


# ===========================================================================
# SECTION 6 — DOWNLOADS AND PDFs
# ===========================================================================

@mcp.tool()
async def download_file(url: str, filename: Optional[str] = None) -> str:
    """Download a file directly by URL (no clicking required).

    Args:
        url: Direct URL to the file.
        filename: Name to save as. Defaults to the filename in the URL.
    """
    page = _require_page()
    if not re.match(r"^\w+://", url):
        url = "https://" + url
    download_dir = _ensure_download_dir()
    response = await page.context.request.get(url)
    if not response.ok:
        return f"Download failed: HTTP {response.status} for {url}"
    body = await response.body()
    if not filename:
        filename = url.split("?")[0].rstrip("/").split("/")[-1] or "download"
    dest = _unique_destination(download_dir, filename)
    dest.write_bytes(body)
    return f"Downloaded {len(body):,} bytes to {dest}"


@mcp.tool()
async def click_and_download(index: int, filename: Optional[str] = None) -> str:
    """Click a button/link that triggers a browser download and save the file.

    Args:
        index: Element index from list_interactive_elements.
        filename: Name to save as. Defaults to browser's suggested name.
    """
    page = _require_page()
    locator = page.locator(f'[data-mcp-index="{index}"]')
    if await locator.count() == 0:
        return f"No element at index {index}."
    download_dir = _ensure_download_dir()
    try:
        async with page.expect_download(timeout=15000) as dl_info:
            await locator.first.click()
        download = await dl_info.value
    except Exception as e:
        return f"No download started after clicking [{index}]: {e}"
    dest = _unique_destination(download_dir, filename or download.suggested_filename)
    await download.save_as(dest)
    return f"Downloaded to {dest}"


@mcp.tool()
async def list_downloads() -> str:
    """List files previously downloaded."""
    download_dir = _ensure_download_dir()
    files = sorted(download_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return f"No downloads yet. Files go to {download_dir}"
    lines = [f"{f.name}  ({f.stat().st_size:,} bytes)" for f in files]
    return f"Downloads in {download_dir}:\n" + "\n".join(lines)


@mcp.tool()
async def read_pdf_text(path: str, max_chars: int = 8000) -> str:
    """Extract text from a local PDF file.

    Args:
        path: Path to the PDF file.
        max_chars: Truncate output to this many characters.
    """
    pdf_path = Path(path).expanduser()
    if not pdf_path.exists():
        return f"No file at {pdf_path}. Call list_downloads to see available files."
    try:
        from pypdf import PdfReader
    except ImportError:
        return "pypdf not installed. Run: pip install pypdf"
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        return f"Couldn't open {pdf_path} as PDF: {e}"
    parts = [p.extract_text() or "" for p in reader.pages]
    text = "\n\n".join(parts).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    truncated = len(text) > max_chars
    text = text[:max_chars]
    if truncated:
        text += "\n\n...[truncated]"
    return text or "(no extractable text — PDF may be scanned/image-based)"


# ===========================================================================
# SECTION 8 — FILESYSTEM
# ===========================================================================

@mcp.tool()
async def read_file(path: str, max_chars: int = 20000) -> str:
    """Read a local file and return its contents.

    Args:
        path: File path (supports ~ for home directory).
        max_chars: Truncate to this many characters.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return f"No file at {p}"
    if not p.is_file():
        return f"{p} is not a file."
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Could not read {p}: {e}"
    truncated = len(text) > max_chars
    text = text[:max_chars]
    if truncated:
        text += "\n\n...[truncated]"
    return text


@mcp.tool()
async def write_file(path: str, content: str, append: bool = False) -> str:
    """Write (or append) text to a local file. Creates parent directories as needed.

    Args:
        path: File path to write (supports ~).
        content: Text content to write.
        append: If True, appends to existing content instead of overwriting.
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    try:
        with open(p, mode, encoding="utf-8") as f:
            f.write(content)
        action = "Appended" if append else "Written"
        return f"{action} {len(content):,} chars to {p}"
    except Exception as e:
        return f"Could not write {p}: {e}"


@mcp.tool()
async def list_files(directory: str = "~", pattern: str = "*", recursive: bool = False) -> str:
    """List files in a directory.

    Args:
        directory: Directory to list (supports ~).
        pattern: Glob pattern to filter by, e.g. "*.py" or "*.pdf".
        recursive: If True, lists files in all subdirectories too.
    """
    d = Path(directory).expanduser()
    if not d.exists():
        return f"Directory not found: {d}"
    try:
        if recursive:
            files = sorted(d.rglob(pattern))
        else:
            files = sorted(d.glob(pattern))
        if not files:
            return f"No files matching '{pattern}' in {d}"
        lines = []
        for f in files:
            size = f.stat().st_size if f.is_file() else 0
            kind = "dir" if f.is_dir() else f"{size:,}b"
            lines.append(f"{kind:>12}  {f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing {d}: {e}"


@mcp.tool()
async def delete_file(path: str) -> str:
    """Delete a file or empty directory.

    Args:
        path: Path to delete (supports ~).
    """
    p = Path(path).expanduser()
    if not p.exists():
        return f"Nothing at {p}"
    try:
        if p.is_dir():
            p.rmdir()
        else:
            p.unlink()
        return f"Deleted {p}"
    except Exception as e:
        return f"Could not delete {p}: {e}"


@mcp.tool()
async def move_file(src: str, dst: str) -> str:
    """Move or rename a file or directory.

    Args:
        src: Source path.
        dst: Destination path.
    """
    s = Path(src).expanduser()
    d = Path(dst).expanduser()
    if not s.exists():
        return f"Source not found: {s}"
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(s), str(d))
        return f"Moved {s} -> {d}"
    except Exception as e:
        return f"Move failed: {e}"


@mcp.tool()
async def copy_file(src: str, dst: str) -> str:
    """Copy a file to a new location.

    Args:
        src: Source file path.
        dst: Destination path.
    """
    s = Path(src).expanduser()
    d = Path(dst).expanduser()
    if not s.exists():
        return f"Source not found: {s}"
    d.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(str(s), str(d))
        return f"Copied {s} -> {d}"
    except Exception as e:
        return f"Copy failed: {e}"


@mcp.tool()
async def search_files(directory: str, query: str, pattern: str = "*", max_results: int = 50) -> str:
    """Search file contents for a text query (grep-style).

    Args:
        directory: Directory to search recursively.
        query: Text to search for (case-insensitive).
        pattern: Only search files matching this glob, e.g. "*.py".
        max_results: Maximum number of matching lines to return.
    """
    d = Path(directory).expanduser()
    if not d.exists():
        return f"Directory not found: {d}"
    results = []
    for f in d.rglob(pattern):
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if query.lower() in line.lower():
                    results.append(f"{f}:{i}: {line.strip()}")
                    if len(results) >= max_results:
                        break
        except Exception:
            continue
        if len(results) >= max_results:
            break
    if not results:
        return f"No matches for '{query}' in {d}"
    return f"{len(results)} match(es):\n" + "\n".join(results)


@mcp.tool()
async def make_directory(path: str) -> str:
    """Create a directory (and any missing parents).

    Args:
        path: Directory path to create.
    """
    p = Path(path).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
        return f"Directory ready: {p}"
    except Exception as e:
        return f"Could not create {p}: {e}"


# ===========================================================================
# SECTION 9 — SHELL / COMMAND EXECUTION
# ===========================================================================

@mcp.tool()
async def run_command(
    command: str,
    cwd: Optional[str] = None,
    timeout_seconds: int = 60,
    capture_output: bool = True,
) -> str:
    """Run a shell command and return its output. Use for running scripts,
    installing packages, calling CLIs, processing files, etc.

    Args:
        command: Shell command to run, e.g. "python script.py" or "pip install pandas".
        cwd: Working directory to run the command in. Defaults to home directory.
        timeout_seconds: Max seconds to wait before killing the process.
        capture_output: If True, returns stdout+stderr. If False, runs detached.
    """
    work_dir = Path(cwd).expanduser() if cwd else Path.home()
    try:
        if capture_output:
            result = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: subprocess.run(
                        command,
                        shell=True,
                        cwd=str(work_dir),
                        capture_output=True,
                        text=True,
                    )
                ),
                timeout=timeout_seconds
            )
            output = result.stdout + result.stderr
            rc = result.returncode
            prefix = f"[exit {rc}]\n" if rc != 0 else ""
            return prefix + (output.strip() or "(no output)")
        else:
            subprocess.Popen(command, shell=True, cwd=str(work_dir))
            return f"Started (detached): {command}"
    except asyncio.TimeoutError:
        return f"Command timed out after {timeout_seconds}s: {command}"
    except Exception as e:
        return f"Command failed: {e}"


@mcp.tool()
async def run_python(code: str, cwd: Optional[str] = None) -> str:
    """Write Python code to a temp file and run it, returning the output.
    Great for data processing, calculations, or generating files.

    Args:
        code: Python source code to execute.
        cwd: Working directory. Defaults to home.
    """
    tmp = Path.home() / ".browser_mcp" / f"_tmp_{uuid.uuid4().hex[:8]}.py"
    _ensure_dir(tmp.parent)
    tmp.write_text(code, encoding="utf-8")
    try:
        result = await run_command(f"python {tmp}", cwd=cwd)
    finally:
        tmp.unlink(missing_ok=True)
    return result


# ===========================================================================
# SECTION 10 — PERSISTENT KEY/VALUE MEMORY
# ===========================================================================

def _load_memory() -> dict:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_memory(data: dict) -> None:
    _ensure_dir(MEMORY_FILE.parent)
    MEMORY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@mcp.tool()
async def memory_set(key: str, value: str) -> str:
    """Store a value in persistent memory. Survives across sessions.

    Args:
        key: Unique key, e.g. "resume_path" or "last_job_search".
        value: String value to store (JSON-encode complex objects).
    """
    data = _load_memory()
    data[key] = {"value": value, "updated": datetime.now().isoformat()}
    _save_memory(data)
    return f"Stored '{key}'."


@mcp.tool()
async def memory_get(key: str) -> str:
    """Retrieve a value from persistent memory.

    Args:
        key: Key to look up.
    """
    data = _load_memory()
    if key not in data:
        return f"No memory entry for '{key}'."
    entry = data[key]
    return f"{entry['value']}  (saved {entry['updated']})"


@mcp.tool()
async def memory_list() -> str:
    """List all keys currently stored in memory."""
    data = _load_memory()
    if not data:
        return "Memory is empty."
    lines = [f"{k}  (updated {v['updated']})" for k, v in sorted(data.items())]
    return "\n".join(lines)


@mcp.tool()
async def memory_delete(key: str) -> str:
    """Delete a key from persistent memory.

    Args:
        key: Key to delete.
    """
    data = _load_memory()
    if key not in data:
        return f"No entry for '{key}'."
    del data[key]
    _save_memory(data)
    return f"Deleted '{key}'."


# ===========================================================================
# SECTION 11 — SQLITE DATABASE
# ===========================================================================

def _db_connection() -> sqlite3.Connection:
    _ensure_dir(DB_PATH.parent)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


@mcp.tool()
async def db_query(sql: str, params: Optional[str] = None) -> str:
    """Run a SQL query against the local SQLite database and return results.
    Good for storing and querying structured data (job applications, contacts, etc.).

    Args:
        sql: SQL statement, e.g. "SELECT * FROM applications WHERE status='pending'".
        params: Optional JSON array of query parameters, e.g. "[\"pending\", 5]".
    """
    p = json.loads(params) if params else []
    conn = None
    try:
        conn = _db_connection()
        cur = conn.execute(sql, p)
        conn.commit()
        rows = cur.fetchall()
        if not rows:
            return f"Query OK. {cur.rowcount} row(s) affected." if cur.rowcount >= 0 else "Query OK. No rows returned."
        cols = [d[0] for d in cur.description]
        lines = ["\t".join(cols)]
        for row in rows:
            lines.append("\t".join(str(v) for v in row))
        return "\n".join(lines)
    except Exception as e:
        return f"SQL error: {e}"
    finally:
        if conn is not None:
            conn.close()


@mcp.tool()
async def db_create_table(table_name: str, columns: str) -> str:
    """Create a table in the local SQLite database if it doesn't exist.

    Args:
        table_name: Table name, e.g. "job_applications".
        columns: Column definitions, e.g. "id INTEGER PRIMARY KEY, company TEXT, role TEXT, status TEXT, applied_at TEXT".
    """
    sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({columns})"
    return await db_query(sql)


@mcp.tool()
async def db_insert(table_name: str, data: str) -> str:
    """Insert a row into a SQLite table.

    Args:
        table_name: Target table.
        data: JSON object of column->value pairs, e.g. '{"company":"Google","role":"SWE","status":"applied"}'.
    """
    row = json.loads(data)
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    sql = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"
    return await db_query(sql, json.dumps(list(row.values())))


# ===========================================================================
# SECTION 12 — EMAIL SENDING
# ===========================================================================

@mcp.tool()
async def send_email(
    to: str,
    subject: str,
    body: str,
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 587,
    username: Optional[str] = None,
    password: Optional[str] = None,
    from_name: Optional[str] = None,
) -> str:
    """Send an email via SMTP. Works with Gmail, Outlook, or any SMTP server.

    For Gmail: enable 2FA and use an App Password (not your main password).
    Store credentials in memory first: memory_set('email_user', ...) etc.

    Args:
        to: Recipient email address (or comma-separated list).
        subject: Email subject.
        body: Email body (plain text).
        smtp_host: SMTP server hostname.
        smtp_port: SMTP port (587 for TLS, 465 for SSL).
        username: SMTP username. If omitted, reads from memory key 'email_user'.
        password: SMTP password/app password. If omitted, reads from memory key 'email_pass'.
        from_name: Display name for the From field.
    """
    mem = _load_memory()
    user = username or mem.get("email_user", {}).get("value")
    pwd  = password or mem.get("email_pass", {}).get("value")
    if not user or not pwd:
        return (
            "No SMTP credentials. Either pass username/password, or store them:\n"
            "  memory_set('email_user', 'you@gmail.com')\n"
            "  memory_set('email_pass', 'your-app-password')"
        )
    msg = MIMEMultipart()
    msg["From"] = f"{from_name} <{user}>" if from_name else user
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    try:
        if smtp_port == 465:
            # Port 465 is implicit SSL — connect straight into SSL, no STARTTLS.
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
                server.login(user, pwd)
                server.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
        else:
            # Port 587 (and most others) use plaintext-then-upgrade via STARTTLS.
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.ehlo()
                server.starttls()
                server.login(user, pwd)
                server.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
        return f"Email sent to {to} — subject: '{subject}'"
    except Exception as e:
        return f"Failed to send email: {e}"


# ===========================================================================
# SECTION 13 — DESKTOP NOTIFICATIONS
# ===========================================================================

@mcp.tool()
async def notify(title: str, message: str) -> str:
    """Send a desktop notification. Works on macOS, Linux (notify-send), and
    Windows (toast). Useful for alerting you when a long task completes.

    Args:
        title: Notification title.
        message: Notification body.
    """
    import platform
    system = platform.system()
    try:
        if system == "Darwin":
            script = f'display notification "{message}" with title "{title}"'
            subprocess.run(["osascript", "-e", script], check=True)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message], check=True)
        elif system == "Windows":
            # Requires winotify or win10toast; fall back to a print
            try:
                from winotify import Notification
                toast = Notification(app_id="browser_mcp", title=title, msg=message)
                toast.show()
            except ImportError:
                return "Notification: install winotify for Windows toast notifications."
        else:
            return f"Unsupported OS for notifications: {system}"
        return f"Notification sent: '{title}'"
    except Exception as e:
        return f"Notification failed: {e}"


# ===========================================================================
# SECTION 14 — SCHEDULER (run tasks on a cron/interval schedule)
# ===========================================================================

_scheduled_jobs: dict[str, dict] = {}


@mcp.tool()
async def schedule_task(
    job_id: str,
    command: str,
    interval_minutes: Optional[int] = None,
    cron: Optional[str] = None,
    run_now: bool = False,
) -> str:
    """Schedule a shell command to run on an interval or cron schedule.
    Requires: pip install apscheduler

    Args:
        job_id: Unique name for this job, e.g. "daily_job_check".
        command: Shell command to run, e.g. "python ~/scripts/check_jobs.py".
        interval_minutes: Run every N minutes (use this OR cron, not both).
        cron: Cron expression, e.g. "0 9 * * 1-5" (9am weekdays).
        run_now: If True, also runs the command immediately.
    """
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        return "apscheduler not installed. Run: pip install apscheduler"

    if not hasattr(schedule_task, "_scheduler"):
        schedule_task._scheduler = AsyncIOScheduler()
        schedule_task._scheduler.start()

    scheduler = schedule_task._scheduler

    # Remove existing job with same ID
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    async def _run():
        result = await run_command(command)
        _scheduled_jobs[job_id]["last_run"] = datetime.now().isoformat()
        _scheduled_jobs[job_id]["last_output"] = result[:500]

    if interval_minutes:
        trigger = IntervalTrigger(minutes=interval_minutes)
        desc = f"every {interval_minutes}m"
    elif cron:
        trigger = CronTrigger.from_crontab(cron)
        desc = f"cron({cron})"
    else:
        return "Provide either interval_minutes or a cron expression."

    scheduler.add_job(_run, trigger=trigger, id=job_id)
    _scheduled_jobs[job_id] = {"command": command, "schedule": desc, "last_run": None, "last_output": None}

    msg = f"Scheduled '{job_id}': {command!r} [{desc}]"
    if run_now:
        await _run()
        msg += f"\nRan immediately. Output: {_scheduled_jobs[job_id]['last_output']}"
    return msg


@mcp.tool()
async def list_scheduled_tasks() -> str:
    """List all currently scheduled tasks and their last run output."""
    if not _scheduled_jobs:
        return "No scheduled tasks."
    lines = []
    for jid, info in _scheduled_jobs.items():
        lines.append(
            f"{jid}  [{info['schedule']}]  cmd: {info['command']!r}\n"
            f"  last_run: {info['last_run'] or 'never'}\n"
            f"  last_output: {info['last_output'] or '—'}"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def cancel_scheduled_task(job_id: str) -> str:
    """Cancel a scheduled task by its job ID.

    Args:
        job_id: Job ID from list_scheduled_tasks.
    """
    if not hasattr(schedule_task, "_scheduler"):
        return "No scheduler running."
    try:
        schedule_task._scheduler.remove_job(job_id)
        _scheduled_jobs.pop(job_id, None)
        return f"Cancelled '{job_id}'."
    except Exception as e:
        return f"Could not cancel '{job_id}': {e}"


# ===========================================================================
# SECTION 15 — UTILITIES
# ===========================================================================

@mcp.tool()
async def get_system_info() -> str:
    """Return basic info about the local system: OS, Python version, home dir,
    current time, and disk space."""
    import platform
    import sys
    total, used, free = shutil.disk_usage(Path.home())
    return (
        f"OS:       {platform.system()} {platform.release()}\n"
        f"Python:   {sys.version.split()[0]}\n"
        f"Home:     {Path.home()}\n"
        f"Time:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Disk:     {free // 1_073_741_824}GB free of {total // 1_073_741_824}GB\n"
        f"Agent DB: {DB_PATH}\n"
        f"Memory:   {MEMORY_FILE}\n"
        f"Downloads:{DOWNLOAD_DIR}"
    )


@mcp.tool()
async def clipboard_copy(text: str) -> str:
    """Copy text to the system clipboard.

    Args:
        text: Text to copy.
    """
    import platform
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run("pbcopy", input=text.encode(), check=True)
        elif system == "Linux":
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)
        elif system == "Windows":
            subprocess.run("clip", input=text.encode(), check=True)
        else:
            return f"Clipboard not supported on {system}."
        return f"Copied {len(text)} chars to clipboard."
    except Exception as e:
        return f"Clipboard copy failed: {e}"


# ===========================================================================
# ENTRY POINT
# ===========================================================================

if __name__ == "__main__":
    import sys
    # Optional: pass --http <port> to run as an HTTP server instead of stdio
    # Requires mcp SDK >= 1.6: pip install "mcp[cli]>=1.6"
    if "--http" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--http") + 1])
        except (IndexError, ValueError):
            port = 8080
        print(f"Starting browser-control MCP server on http://127.0.0.1:{port}/mcp")
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run()  # stdio — default for Claude Desktop
