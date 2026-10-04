"""Fill the cook's Instacart cart with a computer-use agent driving a browser over its DevTools port: either the
Docker sandbox (see shopper/) or, with no Docker, a Chrome window of its own on this computer.

Instacart offers no public cart or ordering API, so Claude works the website the way a person would: search each
item, pick a sensible product, add it. It stops at the cart. Checkout and payment always stay with the cook, and a
guard backs out of any checkout page the agent reaches anyway.
"""

import asyncio
import base64
import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import aiohttp
import anthropic
from playwright.async_api import Browser, Page, async_playwright

import openai_api

DEVTOOLS_URL = "http://127.0.0.1:9223"
VIEWER_URL = "http://localhost:6080/vnc.html?autoconnect=1&resize=scale"
# The local shopping browser: Chrome with a profile of its own (so the Instacart sign-in persists, and the cook's own
# Chrome is never touched), on the same DevTools port the sandbox uses.
LOCAL_PROFILE = Path.home() / ".basil" / "shopper-profile"
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser",
]  # fmt: skip
# What the agent sees and clicks on, whatever size the window is: the sandbox's screen size, at 1x so a screenshot's
# pixels are the page's (on a Retina screen they'd otherwise be twice the click coordinates).
VIEWPORT = {"width": 1280, "height": 800}
# Screenshots go to the model as JPEG: a 1280x800 PNG is 1-2 MB, and some networks (VPNs, security software) break
# uploads that size ("SSL: BAD_RECORD_MAC"); a JPEG is ~150 KB and just as legible.
SHOT = {"type": "jpeg", "quality": 70}
CART_URL = "https://www.instacart.com/store"
MODEL = "claude-opus-5-5"
# Plenty for a dozen items; a run that needs more is stuck, and stopping bounds its cost.
MAX_TURNS = 150
# Lets the page settle after an action so the next screenshot shows its result.
SETTLE_SECONDS = 0.6
# Pages the agent must never act on: checkout and payment are the cook's.
FORBIDDEN_URL_PARTS = ("checkout", "payment", "/orders/new")

SYSTEM_PROMPT = """\
You are shopping on instacart.com in a browser for a home cook. Add to or change their cart as asked, then stop.

- Use the cart of the store you were given; with no store given, use whichever store the site offers first.
- To add an item, search for it and add the best plain match in the requested quantity: the common size, the regular \
version, the store brand when it's clearly equivalent and cheaper. Skip anything exotic or wildly overpriced.
- To change the cart, open it (the cart button at the top of the store page) and use its own controls: the quantity \
stepper to set an amount, the remove control to take an item out. A swap is a remove and an add. Match items loosely \
("milk" is the gallon of whole milk in the cart).
- It's one cart that gets added to and changed over time. If something asked for is already in it, don't add \
another: set its quantity to what's asked. Never leave duplicates of the same thing.
- Before you finish, open the cart, note everything in it with quantities, and leave it showing.
- Never go to checkout, never enter payment or address details, never place an order. The cook does that.
- If the site asks you to log in before you can add items, stop and report needs_login.
- When everything is handled, call finish: what you added (with the product you chose), what you changed, what you \
couldn't find, and what's in the cart now.
"""

SITE_PROMPT = """\
You are shopping on a store's website in a browser for a home cook who wants something specific, often premium. Find \
the requested items and add them to the site's cart, then stop.

- Start on the store's site. Use its search or menus; read product pages to pick what was asked for, at the quality \
asked for. Prefer the store's own listing over third-party sellers.
- If the site lets you add to the cart as a guest, do that. If it insists on an account before you can add anything, \
stop and report needs_login.
- Never go to checkout, never enter payment or address details, never place an order. The cook does that.
- Close pop-ups (newsletters, cookies) by dismissing them; decline anything optional.
- To change the cart, open it and use its own controls: quantity to set an amount, remove to take an item out.
- It's one cart that gets added to and changed over time. If something asked for is already in it, don't add \
another: set its quantity to what's asked. Never leave duplicates of the same thing.
- When everything is handled, open the cart page and leave it showing, then call finish: what you added (with the \
product and price), what you couldn't find, and what's in the cart now.
"""

FINISH_TOOL = {
    "name": "finish",
    "description": "Report the result once every item is handled (or you can't continue).",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "added": {"type": "array", "items": {"type": "string"}, "description": "Each as 'item: product chosen'."},
            "changed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Each as 'milk: removed', 'lemons: 1 to 2'.",
            },
            "missing": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Items you couldn't find or change.",
            },
            "in_cart": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Everything in the cart now, 'item: quantity'.",
            },
            "needs_login": {"type": "boolean"},
            "note": {"type": "string", "description": "Anything the cook should know, or empty."},
        },
        "required": ["added", "changed", "missing", "in_cart", "needs_login", "note"],
        "additionalProperties": False,
    },
}

# xdotool-style key names (what the computer toolset uses) -> Playwright's names.
KEY_NAMES = {
    "return": "Enter", "enter": "Enter", "backspace": "Backspace", "escape": "Escape", "esc": "Escape",
    "tab": "Tab", "space": "Space", "delete": "Delete", "home": "Home", "end": "End",
    "page_down": "PageDown", "page_up": "PageUp", "up": "ArrowUp", "down": "ArrowDown",
    "left": "ArrowLeft", "right": "ArrowRight", "ctrl": "Control", "control": "Control", "alt": "Alt",
    "shift": "Shift", "super": "Meta", "cmd": "Meta", "meta": "Meta",
    # OpenAI's spellings
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "pageup": "PageUp", "pagedown": "PageDown", "del": "Delete", "option": "Alt", "command": "Meta",
}  # fmt: skip


def playwright_key(combo: str) -> str:
    return "+".join(KEY_NAMES.get(part.lower(), part) for part in combo.split("+"))


def describe_change(c: dict) -> str:
    """One cart change, as the shopper and the cook read it: 'remove milk', 'lemons to 2', 'add parsley'."""
    amount = f"{c['quantity']:g} {c.get('unit') or ''}".strip() if c.get("quantity") is not None else ""
    match c["change"]:
        case "remove":
            return f"remove {c['name']}"
        case "set_quantity":
            return f"{c['name']} to {amount or 'the usual amount'}"
        case _:
            return f"add {c['name']}" + (f" ({amount})" if amount else "")


def store_url(url: str) -> str:
    """The page of the store the agent shopped at, where 'View cart' is one tap. Instacart has no link that opens
    the cart itself, and carts are per store, so the bare /store home doesn't show it."""
    found = re.match(r"https://www\.instacart\.com/store/([a-z0-9-]+)(?:/|$|\?)", url)
    return f"https://www.instacart.com/store/{found[1]}/storefront" if found else CART_URL


def browser_running(devtools_url: str = DEVTOOLS_URL) -> bool:
    try:
        urllib.request.urlopen(f"{devtools_url}/json/version", timeout=2)
        return True
    except OSError:
        return False


def sandbox_running(devtools_url: str = DEVTOOLS_URL) -> bool:
    """The Docker sandbox, as opposed to the local shopping Chrome: both answer on the DevTools port, only the sandbox
    has its web viewer."""
    if not browser_running(devtools_url):
        return False
    try:
        urllib.request.urlopen(VIEWER_URL, timeout=2).close()
        return True
    except OSError:
        return False


def ensure_window(devtools_url: str = DEVTOOLS_URL) -> bool:
    """Make sure the shopping browser has a window. On a Mac, closing Chrome's last window leaves it running with
    none, and Playwright can't attach to a browser with no window ("Browser context management is not supported")."""
    with urllib.request.urlopen(f"{devtools_url}/json/list", timeout=3) as response:
        pages = [t for t in json.load(response) if t.get("type") == "page"]
    if pages:
        return False
    new = urllib.request.Request(f"{devtools_url}/json/new?{urllib.parse.quote(CART_URL, safe='')}", method="PUT")
    urllib.request.urlopen(new, timeout=5).close()
    return True


async def connect(p, devtools_url: str) -> Browser:
    if await asyncio.to_thread(ensure_window, devtools_url):
        await asyncio.sleep(2)  # let the new window finish loading before anything navigates it again
    return await p.chromium.connect_over_cdp(devtools_url)


def find_chrome() -> str | None:
    return next((path for path in CHROME_PATHS if Path(path).exists() or shutil.which(path)), None)


def browser_info(devtools_url: str = DEVTOOLS_URL) -> dict | None:
    try:
        with urllib.request.urlopen(f"{devtools_url}/json/version", timeout=2) as response:
            return json.load(response)
    except OSError:
        return None


# Which way the shopping Chrome was started. Chrome itself can't say: with a normal user agent, headless reports
# itself as plain "Chrome".
MODE_FILE = LOCAL_PROFILE.parent / "shopper-mode"


def is_headless(info: dict) -> bool:
    try:
        return MODE_FILE.read_text().strip() == "headless"
    except OSError:
        return "Headless" in info.get("Browser", "")


def close_browser(devtools_url: str = DEVTOOLS_URL) -> None:
    """Quit the shopping Chrome (it's ours, with its own profile), and wait until it's gone: a profile can only be
    open in one Chrome at a time, so switching between headless and a window means closing first."""
    from websockets.sync.client import connect as ws_connect

    if not (info := browser_info(devtools_url)):
        return
    with ws_connect(info["webSocketDebuggerUrl"], max_size=None) as ws:
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
    deadline = time.monotonic() + 10
    while browser_running(devtools_url) and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(0.5)  # the profile lock goes a moment after the port does


def normal_user_agent(chrome: str) -> str:
    """The user agent this Chrome has with a window. Headless Chrome says 'HeadlessChrome', which some sites turn away."""
    version = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=10).stdout
    major = next((part.split(".")[0] for part in version.split() if part[:1].isdigit()), "150")
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)"
        f" Chrome/{major}.0.0.0 Safari/537.36"
    )


def already(known: list[str]) -> str:
    """What the shopper is told is in the cart already, so it adjusts instead of duplicating."""
    if not known:
        return ""
    return "\nAlready in the cart at the last look:\n" + "\n".join(f"- {k}" for k in known)


def site_url(site: str) -> str:
    """'murrayscheese.com' or a full address -> a URL to start from."""
    site = site.strip()
    return site if site.startswith("http") else f"https://{site.removeprefix('www.')}"


# Pictures of the carts a run ended on, for the cook to see a cart that only exists in the shopping browser.
SHOTS_DIR = Path(__file__).parent / "cart_shots"


async def save_shot(page: Page) -> str | None:
    try:
        SHOTS_DIR.mkdir(exist_ok=True)
        name = f"{int(time.time() * 1000)}.jpg"
        (SHOTS_DIR / name).write_bytes(await page.screenshot(type="jpeg", quality=75))
        return name
    except Exception:  # a missing picture mustn't lose the cart
        return None


async def describe_frame() -> str:
    """One line on what the shopping browser is showing now, for Basil to answer 'how's the shopping going?'."""
    import anthropic

    import openai_api

    if latest_frame is None:
        return "nothing on screen yet"
    data = base64.b64encode(latest_frame).decode()
    ask = "This is a browser doing someone's grocery shopping. In one short line: what page is it on, and what does it seem to be doing?"
    if os.environ.get("ANTHROPIC_API_KEY"):
        response = await anthropic.AsyncAnthropic().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                        {"type": "text", "text": ask},
                    ],
                }
            ],  # fmt: skip
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()
    if openai_api.available():
        return await openai_api.ask(ask, images=[("image/jpeg", data)])
    return "can't describe it without an Anthropic or OpenAI key"


# One driver at a time: a shopping run, a cart change and the sign-in check all drive the same page, possibly from
# different processes (two servers, a test), and two at once undo each other's clicks. An OS file lock covers that.
LOCK_FILE = LOCAL_PROFILE.parent / "shopper.lock"


class BrowserBusy(RuntimeError):
    pass


@contextlib.contextmanager
def driving():
    """Hold the shopping browser for one job; raises BrowserBusy if something else has it."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BrowserBusy("The shopping browser is busy with another run; try again when it's done.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# What the shopping browser last showed, for watching it shop from Basil's own screen (it has no window of its own).
latest_frame: bytes | None = None


async def keep_frame(page: Page) -> None:
    global latest_frame
    try:
        latest_frame = await page.screenshot(type="jpeg", quality=60)
    except Exception:  # watching is a nicety; never let it stop the shopping
        pass


# While the cook is signing in, the shopping Chrome has a window and must be left alone.
signing_in = False


def start_local_browser(chrome: str, devtools_url: str = DEVTOOLS_URL, visible: bool = False) -> None:
    """Have the shopping browser running on this computer: headless (no window on the cook's screen) unless it's
    needed visible, for signing in. It's left running when the server stops, so a restart picks it back up."""
    if info := browser_info(devtools_url):
        if is_headless(info) != visible:
            return  # already the way it's wanted
        if not visible and signing_in:
            return  # the cook is signing in in that window; it goes headless once they're done
        close_browser(devtools_url)
    LOCAL_PROFILE.mkdir(parents=True, exist_ok=True)
    port = devtools_url.rsplit(":", 1)[1]
    flags = ["--window-size=1280,900"]
    if not visible:
        flags = ["--headless=new", f"--user-agent={normal_user_agent(chrome)}", "--window-size=1280,800"]
    subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={LOCAL_PROFILE}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--force-device-scale-factor=1",
            *flags,
            CART_URL,
        ],  # fmt: skip
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # its own process group, so Ctrl-C on the server doesn't close it
        env={**os.environ},
    )
    MODE_FILE.write_text("window" if visible else "headless")
    deadline = time.monotonic() + 15
    while not browser_running(devtools_url):
        if time.monotonic() > deadline:
            raise RuntimeError("Chrome didn't open its DevTools port")
        time.sleep(0.3)


def on_store_page(devtools_url: str = DEVTOOLS_URL) -> bool:
    """Whether a tab is past the login, on Instacart itself; read from the tab list, so it never touches the page
    the cook is typing into."""
    with urllib.request.urlopen(f"{devtools_url}/json/list", timeout=3) as response:
        urls = [t.get("url", "") for t in json.load(response) if t.get("type") == "page"]
    return any("instacart.com" in u and "/login" not in u and "/signup" not in u for u in urls)


async def in_a_window(url: str, done, devtools_url: str = DEVTOOLS_URL, give_up_after: float = 1800) -> None:
    """Show the shopping browser as a window on `url` until `done()` says so (or the cook closes it), then go back to
    headless with the same profile, so sign-ins and carts carry over."""
    global signing_in
    chrome = find_chrome()
    if chrome is None:
        raise RuntimeError("no Chrome to show")
    signing_in = True
    try:
        with driving():
            await asyncio.to_thread(start_local_browser, chrome, devtools_url, True)
            new = urllib.request.Request(f"{devtools_url}/json/new?{urllib.parse.quote(url, safe='')}", method="PUT")
            await asyncio.to_thread(lambda: urllib.request.urlopen(new, timeout=5).close())
            deadline = time.monotonic() + give_up_after
            while time.monotonic() < deadline and browser_running(devtools_url):
                await asyncio.sleep(3)
                if await asyncio.to_thread(done):
                    await asyncio.sleep(2)
                    break
    finally:
        signing_in = False
    await asyncio.to_thread(start_local_browser, chrome, devtools_url, False)


async def sign_in(devtools_url: str = DEVTOOLS_URL) -> None:
    """The one-time Instacart sign-in: a window until the cook's through to the store."""
    await in_a_window(CART_URL, lambda: on_store_page(devtools_url), devtools_url)


def window_closed(devtools_url: str = DEVTOOLS_URL) -> bool:
    with urllib.request.urlopen(f"{devtools_url}/json/list", timeout=3) as response:
        return not [t for t in json.load(response) if t.get("type") == "page"]


async def show_cart(url: str, devtools_url: str = DEVTOOLS_URL) -> None:
    """A store's cart, opened as a window for the cook to review and check out; headless again once they close it."""
    await in_a_window(url, lambda: window_closed(devtools_url), devtools_url)


class InstacartShopper:
    def __init__(
        self, client: anthropic.AsyncAnthropic, devtools_url: str = DEVTOOLS_URL, viewer_url: str | None = VIEWER_URL
    ) -> None:
        self.client = client
        self.devtools_url = devtools_url
        # Where the cook sees the shopping browser: the sandbox's web viewer, or None when it's a window on this computer.
        self.viewer_url = viewer_url

    async def signed_in(self) -> bool | None:
        """Whether the shopping browser is signed in to Instacart; None if it's busy shopping (checking would navigate
        its page out from under the run). Signed out, the store redirects to the login page or shows 'Log in'."""
        try:
            with driving():
                return await self._signed_in()
        except BrowserBusy:
            return None

    async def _signed_in(self) -> bool:
        async with async_playwright() as p:
            browser = await connect(p, self.devtools_url)
            page = await self._page(browser)
            await page.wait_for_load_state("domcontentloaded")
            if "/login" in page.url:
                return False
            return await page.get_by_role("button", name="Log in").count() == 0

    async def fill_cart(self, items: list[dict], store: str | None, known: list[str] = ()) -> dict:
        """Add `items` ({"name", "quantity", "unit"}) to the cart; returns the agent's finish report. `known` is what
        was in the cart at the last look, so it sets quantities rather than adding duplicates."""
        with driving():
            return await self._fill_cart(items, store, known)

    async def _fill_cart(self, items: list[dict], store: str | None, known: list[str] = ()) -> dict:
        async with async_playwright() as p:
            browser = await connect(p, self.devtools_url)
            page = await self._page(browser)
            wanted = "\n".join(
                f"- {i['name']}" + (f" ({i['quantity']} {i['unit'] or ''})".rstrip() if i["quantity"] else "")
                for i in items
            )
            task = f"Store: {store or 'any'}\nAdd these items:\n{wanted}" + already(known)
            report = await self._run(page, task)
            return {**report, "cart_url": store_url(page.url), "shot": await save_shot(page)}

    async def shop_site(self, site: str, items: list[dict], note: str | None, known: list[str] = ()) -> dict:
        """Add `items` to the cart of any store's website (not Instacart). The cart lives in the shopping browser, so
        the report carries the cart page and a picture of it."""
        with driving():
            async with async_playwright() as p:
                browser = await connect(p, self.devtools_url)
                page = await self._page(browser, site_url(site))
                wanted = "\n".join(
                    f"- {i['name']}" + (f" ({i['quantity']:g} {i['unit'] or ''})".rstrip() if i["quantity"] else "")
                    for i in items
                )
                task = f"Store: {site}\nAdd these items:\n{wanted}" + (f"\nWhat the cook wants: {note}" if note else "")
                task += already(known)
                report = await self._run(page, task, SITE_PROMPT)
                return {**report, "cart_url": page.url, "shot": await save_shot(page)}

    async def change_cart(self, changes: list[dict], store: str | None, known: list[str] = ()) -> dict:
        """Change what's in the cart ({"name", "change": remove|set_quantity|add, "quantity", "unit"}); with no changes,
        just look. Returns the agent's finish report, which lists what's in the cart now."""
        with driving():
            return await self._change_cart(changes, store, known)

    async def _change_cart(self, changes: list[dict], store: str | None, known: list[str] = ()) -> dict:
        # A store's own site (anything with a dot) or an Instacart store.
        site = bool(store and "." in store)
        async with async_playwright() as p:
            browser = await connect(p, self.devtools_url)
            page = await self._page(browser, site_url(store) if site else CART_URL)
            wanted = (
                "\n".join(f"- {describe_change(c)}" for c in changes) or "- nothing; just open the cart and list it"
            )
            task = f"Store: {store or 'any'}\nChange the cart:\n{wanted}" + already(known)
            report = await self._run(page, task, SITE_PROMPT if site else SYSTEM_PROMPT)
            if site:
                return {**report, "cart_url": page.url, "shot": await save_shot(page)}
            return {**report, "cart_url": store_url(page.url), "shot": await save_shot(page)}

    async def _page(self, browser: Browser, start: str = CART_URL) -> Page:
        context = browser.contexts[0]
        pages = [pg for pg in context.pages if pg.url.startswith("http")] or [await context.new_page()]
        page = pages[0]
        await page.set_viewport_size(VIEWPORT)
        await page.bring_to_front()
        await page.goto(start, wait_until="domcontentloaded")
        await keep_frame(page)
        return page

    async def _run(self, page: Page, task: str, system: str = SYSTEM_PROMPT) -> dict:
        messages: list = [{"role": "user", "content": task}]
        for _ in range(MAX_TURNS):
            response = await self.client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=system,
                tools=[{"type": "computer_toolset_20260801"}, FINISH_TOOL],
                messages=messages,
                output_config={"effort": "medium"},
                # Caches the growing screenshot history, so each turn pays mainly for what's new.
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                return {"added": [], "missing": [], "needs_login": False, "note": "The shopping agent declined."}
            # Thinking blocks must go back unchanged, so the whole content is appended as-is.
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            for call in calls:
                if call.name == "finish":
                    return call.input
            if not calls:
                messages.append({"role": "user", "content": "Call finish when you're done."})
                continue
            messages.append({"role": "user", "content": await self._act(page, calls)})
            await keep_frame(page)
        return {"added": [], "missing": [], "needs_login": False, "note": "Ran out of steps before finishing."}

    async def _act(self, page: Page, calls: list) -> list[dict]:
        """Run one turn's computer actions in order; after a failure the rest are reported as not executed."""
        results, failed = [], False
        for call in calls:
            result = {"type": "tool_result", "tool_use_id": call.id, "toolset_name": "computer"}
            if failed:
                results.append({**result, "is_error": True, "content": "Not executed: an earlier action failed."})
                continue
            try:
                content = await self._do(page, call.name, call.input)
                await asyncio.sleep(SETTLE_SECONDS)
                if any(part in page.url for part in FORBIDDEN_URL_PARTS):
                    await page.goto(CART_URL, wait_until="domcontentloaded")
                    raise RuntimeError("Checkout is off limits; went back. Finish without checking out.")
                results.append({**result, "content": content})
            except Exception as e:  # any failed action goes back to the model, which can try another way
                logging.warning(f"shopper action {call.name} failed: {e}")
                failed = True
                results.append({**result, "is_error": True, "content": str(e)})
        return results

    async def _do(self, page: Page, name: str, args: dict) -> list[dict] | str:
        mouse, keyboard = page.mouse, page.keyboard
        match name:
            case "screenshot":
                return [image(await page.screenshot(**SHOT))]
            case "zoom":
                x0, y0, x1, y1 = args["region"]
                return [
                    image(await page.screenshot(**SHOT, clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}))
                ]
            case "left_click" | "right_click" | "middle_click" | "double_click" | "triple_click":
                button = {"right_click": "right", "middle_click": "middle"}.get(name, "left")
                count = {"double_click": 2, "triple_click": 3}.get(name, 1)
                await with_modifiers(
                    keyboard, args.get("text"), lambda: click(mouse, args.get("coordinate"), button, count)
                )
            case "left_click_drag":
                await mouse.move(*args["start_coordinate"])
                await mouse.down()
                await mouse.move(*args["coordinate"], steps=10)
                await mouse.up()
            case "mouse_move":
                await mouse.move(*args["coordinate"])
            case "left_mouse_down":
                await mouse.down()
            case "left_mouse_up":
                await mouse.up()
            case "cursor_position":
                return "Unknown; take a screenshot."
            case "scroll":
                if args.get("coordinate"):
                    await mouse.move(*args["coordinate"])
                step = 120 * args["scroll_amount"]
                dx, dy = {"up": (0, -step), "down": (0, step), "left": (-step, 0), "right": (step, 0)}[
                    args["scroll_direction"]
                ]
                await with_modifiers(keyboard, args.get("text"), lambda: mouse.wheel(dx, dy))
            case "type":
                await keyboard.type(args["text"], delay=25)
            case "key":
                for _ in range(args.get("repeat") or 1):
                    await keyboard.press(playwright_key(args["text"]))
            case "hold_key":
                key = playwright_key(args["text"])
                await keyboard.down(key)
                await asyncio.sleep(min(args["duration"], 10))
                await keyboard.up(key)
            case "wait":
                await asyncio.sleep(min(args["duration"], 10))
            case _:
                raise ValueError(f"unsupported action {name!r}")
        return "OK"


def image(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()},
    }


async def click(mouse, coordinate: list[int] | None, button: str, count: int) -> None:
    if coordinate:
        await mouse.move(*coordinate)
    await mouse.down(button=button, click_count=count)
    await mouse.up(button=button, click_count=count)


async def with_modifiers(keyboard, text: str | None, action) -> None:
    """Hold modifier keys (e.g. 'shift', 'ctrl+shift') around an action."""
    keys = [playwright_key(k) for k in text.split("+")] if text else []
    for key in keys:
        await keyboard.down(key)
    try:
        await action()
    finally:
        for key in reversed(keys):
            await keyboard.up(key)


class OpenAIShopper(InstacartShopper):
    """The same shopping agent on OpenAI's computer tool, for when there's an OpenAI key and no Anthropic one. Same
    browser, same checkout guard, same finish report; only the model loop differs."""

    def __init__(self, devtools_url: str = DEVTOOLS_URL, viewer_url: str | None = VIEWER_URL) -> None:
        super().__init__(None, devtools_url, viewer_url)

    async def _run(self, page: Page, task: str, system: str = SYSTEM_PROMPT) -> dict:
        finish = {"type": "function", "name": "finish", "description": FINISH_TOOL["description"],
                  "parameters": FINISH_TOOL["input_schema"], "strict": True}  # fmt: skip
        # Instructions and tools don't carry over with previous_response_id, so every turn sends them.
        turn = {
            "instructions": system,
            "tools": [{"type": "computer"}, finish],
            "reasoning": {"effort": "medium"},
        }
        body = {**turn, "input": f"{task}\n\nUse the computer tool to work the browser; call finish when done."}
        async with aiohttp.ClientSession(timeout=openai_api.TIMEOUT) as http:
            for _ in range(MAX_TURNS):
                response = await openai_api.respond(http, body)
                output = response.get("output", [])
                for item in output:
                    if item.get("type") == "function_call" and item.get("name") == "finish":
                        return json.loads(item["arguments"])
                calls = [item for item in output if item.get("type") == "computer_call"]
                if not calls:
                    body = {**turn, "previous_response_id": response["id"],
                            "input": [{"role": "user", "content": "Call finish when you're done."}]}  # fmt: skip
                    continue
                replies, note = [], None
                for call in calls:
                    if call.get("pending_safety_checks"):
                        # OpenAI flagged something on the page; don't wave it through on the cook's account.
                        checks = "; ".join(c.get("message", "") for c in call["pending_safety_checks"])
                        return {
                            "added": [],
                            "missing": [],
                            "needs_login": False,
                            "note": f"Stopped for safety: {checks}",
                        }
                    try:
                        for action in call.get("actions") or [call.get("action")]:
                            await self._do_openai(page, action)
                            await asyncio.sleep(SETTLE_SECONDS)
                            if any(part in page.url for part in FORBIDDEN_URL_PARTS):
                                await page.goto(CART_URL, wait_until="domcontentloaded")
                                note = "Checkout is off limits; went back. Finish without checking out."
                                break
                    except Exception as e:  # the model sees the screen as it is and can try another way
                        logging.warning(f"shopper action failed: {e}")
                        note = f"That action failed: {e}"
                    screenshot = base64.b64encode(await page.screenshot(**SHOT)).decode()
                    await keep_frame(page)
                    replies.append({"type": "computer_call_output", "call_id": call["call_id"],
                                    "output": {"type": "computer_screenshot", "detail": "original",
                                               "image_url": f"data:image/jpeg;base64,{screenshot}"}})  # fmt: skip
                if note:
                    replies.append({"role": "user", "content": note})
                body = {**turn, "previous_response_id": response["id"], "input": replies}
        return {"added": [], "missing": [], "needs_login": False, "note": "Ran out of steps before finishing."}

    async def _do_openai(self, page: Page, action: dict) -> None:
        """One of OpenAI's computer actions, in Playwright."""
        mouse, keyboard = page.mouse, page.keyboard
        held = "+".join(action.get("keys") or []) if action["type"] != "keypress" else None
        match action["type"]:
            case "click":
                button = {"wheel": "middle", "right": "right"}.get(action.get("button", "left"), "left")
                await with_modifiers(keyboard, held, lambda: click(mouse, [action["x"], action["y"]], button, 1))
            case "double_click":
                await with_modifiers(keyboard, held, lambda: click(mouse, [action["x"], action["y"]], "left", 2))
            case "drag":
                path = action["path"]
                await mouse.move(path[0]["x"], path[0]["y"])
                await mouse.down()
                for point in path[1:]:
                    await mouse.move(point["x"], point["y"], steps=5)
                await mouse.up()
            case "move":
                await mouse.move(action["x"], action["y"])
            case "scroll":
                await mouse.move(action["x"], action["y"])
                await with_modifiers(
                    keyboard, held, lambda: mouse.wheel(action.get("scroll_x", 0), action.get("scroll_y", 0))
                )
            case "keypress":
                await keyboard.press(playwright_key("+".join(action["keys"])))
            case "type":
                await keyboard.type(action["text"], delay=25)
            case "wait":
                await asyncio.sleep(2)
            case "screenshot":
                pass  # a screenshot goes back after every call anyway
            case other:
                raise ValueError(f"unsupported action {other!r}")
