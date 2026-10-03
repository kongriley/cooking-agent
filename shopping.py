"""Fill the cook's Instacart cart with a computer-use agent driving the sandboxed browser (see shopper/).

Instacart offers no public cart or ordering API, so Claude works the website the way a person would: search each
item, pick a sensible product, add it. It stops at the cart. Checkout and payment always stay with the cook, and a
guard backs out of any checkout page the agent reaches anyway.
"""

import asyncio
import base64
import logging

import anthropic
from playwright.async_api import Browser, Page, async_playwright

DEVTOOLS_URL = "http://127.0.0.1:9223"
VIEWER_URL = "http://localhost:6080/vnc.html?autoconnect=1&resize=scale"
CART_URL = "https://www.instacart.com/store"
MODEL = "claude-opus-5-5"
# Plenty for a dozen items; a run that needs more is stuck, and stopping bounds its cost.
MAX_TURNS = 150
# Lets the page settle after an action so the next screenshot shows its result.
SETTLE_SECONDS = 0.6
# Pages the agent must never act on: checkout and payment are the cook's.
FORBIDDEN_URL_PARTS = ("checkout", "payment", "/orders/new")

SYSTEM_PROMPT = """\
You are shopping on instacart.com in a browser for a home cook. Add the requested items to their cart, then stop.

- Use the cart of the store you were given; with no store given, use whichever store the site offers first.
- For each item, search for it and add the best plain match in the requested quantity: the common size, the regular \
version, the store brand when it's clearly equivalent and cheaper. Skip anything exotic or wildly overpriced.
- Never go to checkout, never enter payment or address details, never place an order. The cook does that.
- If the site asks you to log in before you can add items, stop and report needs_login.
- When every item is handled, call finish. List what you added (with the product you chose) and what you couldn't find.
"""

FINISH_TOOL = {
    "name": "finish",
    "description": "Report the result once every item is handled (or you can't continue).",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "added": {"type": "array", "items": {"type": "string"}, "description": "Each as 'item: product chosen'."},
            "missing": {"type": "array", "items": {"type": "string"}},
            "needs_login": {"type": "boolean"},
            "note": {"type": "string", "description": "Anything the cook should know, or empty."},
        },
        "required": ["added", "missing", "needs_login", "note"],
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
}  # fmt: skip


def playwright_key(combo: str) -> str:
    return "+".join(KEY_NAMES.get(part.lower(), part) for part in combo.split("+"))


class InstacartShopper:
    def __init__(self, client: anthropic.AsyncAnthropic, devtools_url: str = DEVTOOLS_URL) -> None:
        self.client = client
        self.devtools_url = devtools_url

    async def signed_in(self) -> bool:
        """Whether the sandbox browser is signed in to Instacart (Instacart shows a 'Log in' button when not)."""
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(self.devtools_url)
            page = await self._page(browser)
            await page.wait_for_load_state("domcontentloaded")
            return await page.get_by_role("button", name="Log in").count() == 0

    async def fill_cart(self, items: list[dict], store: str | None) -> dict:
        """Add `items` ({"name", "quantity", "unit"}) to the cart; returns the agent's finish report."""
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(self.devtools_url)
            page = await self._page(browser)
            wanted = "\n".join(
                f"- {i['name']}" + (f" ({i['quantity']} {i['unit'] or ''})".rstrip() if i["quantity"] else "")
                for i in items
            )
            task = f"Store: {store or 'any'}\nItems:\n{wanted}"
            return await self._run(page, task)

    async def _page(self, browser: Browser) -> Page:
        context = browser.contexts[0]
        pages = [pg for pg in context.pages if pg.url.startswith("http")] or [await context.new_page()]
        page = pages[0]
        await page.bring_to_front()
        await page.goto(CART_URL, wait_until="domcontentloaded")
        return page

    async def _run(self, page: Page, task: str) -> dict:
        messages: list = [{"role": "user", "content": task}]
        for _ in range(MAX_TURNS):
            response = await self.client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
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
                return [image(await page.screenshot())]
            case "zoom":
                x0, y0, x1, y1 = args["region"]
                return [image(await page.screenshot(clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}))]
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


def image(png: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(png).decode()},
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
