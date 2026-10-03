"""Basil's voice app: serves the page and bridges each browser tab to its own Phonic conversation.

    uv run server.py     # then open http://localhost:8000

The browser only does audio and display; the API key, tools and timers stay in this process.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import urllib.parse
from functools import partial
from http import HTTPStatus
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from images import Images, StepPictures
from kitchen import Kitchen, Timer
from session import Session, advisor, instacart, open_session
from timers import Timers
from tools import Toolbox

HERE = Path(__file__).parent
# Events after which the panes need fresh state.
STATE_CHANGING_EVENTS = {"tool_result", "timer_fired", "conversation_created", "cart_update"}


IMAGES = Images(HERE / "images.json")
STEP_PICTURES_DIR = HERE / "step_pictures"
STEP_PICTURES: StepPictures  # made at startup, once the keys are in the environment


class StillTimers(Timers):
    """The saved timers, with no conversation to alert: they can be paused, resumed or set, but none goes off."""

    def _schedule(self, timer: Timer) -> None:
        pass


def saved_toolbox(kitchen_path: Path) -> Toolbox:
    """The kitchen as saved, for the screen to show and change while Basil isn't in a conversation."""
    kitchen = Kitchen.load(kitchen_path)

    async def quiet(*_) -> None:
        pass

    timers = StillTimers(kitchen, save=lambda: kitchen.save(kitchen_path), alert=quiet, nudge=quiet)
    return Toolbox(kitchen, kitchen_path, timers, None, on_cart_done=quiet)


async def process_request(connection: ServerConnection, request: Request) -> Response | None:
    url = urllib.parse.urlsplit(request.path)
    if url.path == "/ws":
        return None
    if url.path == "/img":
        # Pictures load lazily from the page; the lookup runs once per name and is cached.
        query = urllib.parse.parse_qs(url.query)
        found = await IMAGES.url(query["q"][0], query["kind"][0])
        if found is None:
            return connection.respond(HTTPStatus.NOT_FOUND, "no image\n")
        response = connection.respond(HTTPStatus.FOUND, "")
        response.headers["Location"] = found
        response.headers["Cache-Control"] = "max-age=86400"
        return response
    if url.path == "/steppic":
        # Where a step's picture is, and whether it's a real photo or a generated illustration (the page labels those).
        query = urllib.parse.parse_qs(url.query)
        found = await STEP_PICTURES.picture(query["dish"][0], query["title"][0], query["text"][0])
        if found is None:
            body = {"url": None, "illustration": False}
        else:
            illustration = not found.startswith("http")
            body = {"url": f"/step-files/{found}" if illustration else found, "illustration": illustration}
        response = connection.respond(HTTPStatus.OK, json.dumps(body))
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = "application/json"
        return response
    if url.path.startswith("/step-files/"):
        file = STEP_PICTURES_DIR / Path(url.path).name  # .name keeps requests inside the folder
        if not file.is_file():
            return connection.respond(HTTPStatus.NOT_FOUND, "no file\n")
        headers = Headers({"Content-Type": "image/webp", "Cache-Control": "max-age=604800"})
        return Response(HTTPStatus.OK.value, "OK", headers, file.read_bytes())
    if request.path != "/":
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
    response = connection.respond(HTTPStatus.OK, (HERE / "index.html").read_text())
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response


async def push_state(browser: ServerConnection, session: Session) -> None:
    await browser.send(json.dumps({"type": "state", **session.toolbox.snapshot()}))


async def apply_action(toolbox: Toolbox, message: dict) -> dict | None:
    """Apply a tap in the UI. Returns what to tell the agent, if a conversation is running, so it stays in sync.

    A tap on something that's just gone (a timer that went off a moment ago, a step already done) changes nothing;
    it mustn't end the conversation.
    """
    try:
        return await _apply_action(toolbox, message)
    except (KeyError, ValueError):
        logging.warning(f"ignored a tap on something that's gone: {message}")
        return None


async def _apply_action(toolbox: Toolbox, message: dict) -> dict | None:
    match message["action"]:
        case "step":
            step = toolbox.kitchen.step(message["step_id"])
            toolbox.update_step(step.id, message["status"])
            note = f"The user tapped '{step.text}' as {message['status']} in the app. Briefly tell them what's next."
            return {"type": "generate_reply", "system_message": f"[{note}]"}
        case "timer":
            label, change = message["label"], message["change"]
            toolbox.adjust_timer(label, change, message.get("minutes"))
            done = {
                "pause": "paused",
                "resume": "resumed",
                "add": f"added {message.get('minutes')} min to",
                "cancel": "cancelled",
            }
            note = f"[The cook {done[change]} the '{label}' timer in the app. Say nothing about it.]"
            return {"type": "add_system_message", "system_message": note}
        case "timer_again":
            # A rung timer the cook wants a little longer on; the alert keeps it simple.
            toolbox.set_timer(message["label"], message["minutes"], "Check it again.")
            note = f"[The cook gave '{message['label']}' {message['minutes']} more minute(s) in the app. Say nothing.]"
            return {"type": "add_system_message", "system_message": note}
        case "buy_missing":
            # One tap orders what the dish's ingredient list says the cook has none of.
            recipe = toolbox.kitchen.recipes[message["dish"]]
            missing = [
                i for i in recipe if (s := toolbox.kitchen.stock(i["name"])) and s[1]["have"].strip().lower() == "none"
            ]
            items = [
                {"name": f"{i['name']} ({i['amount']})" if i["amount"] else i["name"], "quantity": None, "unit": None}
                for i in missing
            ]
            result = await toolbox.call("fill_instacart_cart", {"store": None, "items": items})
            note = f"[The cook tapped 'Add to Instacart cart' for {', '.join(i['name'] for i in missing)}: {json.dumps(result)}. Don't repeat it.]"
            return {"type": "add_system_message", "system_message": note}
        case "clear_all":
            toolbox.clear_all()
            note = "[The cook tapped Clear all: the plan, timers and conversation are gone. Start fresh; say nothing.]"
            return {"type": "add_system_message", "system_message": note}
        case "clear_dish":
            toolbox.clear_plan(message["dish"])
            note = f"[The cook cleared {message['dish']} from the plan in the app. Say nothing about it.]"
            return {"type": "add_system_message", "system_message": note}
    return None


async def handle_action(browser: ServerConnection, session: Session, message: dict) -> None:
    if note := await apply_action(session.toolbox, message):
        await session.send(note)
    await push_state(browser, session)


async def handle_screen(browser: ServerConnection, kitchen_path: Path) -> None:
    """The screen without Basil: shows the saved kitchen and applies taps to it. Any message asks for fresh state."""
    try:
        async for raw in browser:
            message = json.loads(raw)
            toolbox = saved_toolbox(kitchen_path)
            # Buying needs the shopping agent, which only runs inside a conversation.
            if message["type"] == "action" and message["action"] != "buy_missing":
                await apply_action(toolbox, message)
            await browser.send(json.dumps({"type": "state", **toolbox.snapshot()}))
    except ConnectionClosed:
        pass


async def handle_browser(browser: ServerConnection, args: argparse.Namespace) -> None:
    # /ws?talk=0 is the screen alone; plain /ws is a conversation with Basil, which starts only when the cook asks.
    if urllib.parse.urlsplit(browser.request.path).query == "talk=0":
        return await handle_screen(browser, Path(args.kitchen))
    session: Session | None = None

    async def forward(event: dict) -> None:
        await browser.send(json.dumps(event))
        if event["type"] in STATE_CHANGING_EVENTS:
            await push_state(browser, session)

    async def read_browser() -> None:
        async for raw in browser:
            message = json.loads(raw)
            match message["type"]:
                case "audio":
                    await session.send_audio(message["audio"])
                case "action":
                    await handle_action(browser, session, message)

    session_args = (args.voice, args.speed, instacart(args), advisor(), args.api_base)
    try:
        async with open_session(Path(args.kitchen), forward, *session_args) as session:
            # Stop when either side goes away: the tab closes, or Phonic ends the conversation.
            reading = asyncio.create_task(read_browser())
            ended = asyncio.create_task(session.ended.wait())
            await asyncio.wait([reading, ended], return_when=asyncio.FIRST_COMPLETED)
            for task in [reading, ended]:
                task.cancel()
            if reading.done() and not reading.cancelled():
                reading.result()
    except ConnectionClosed:
        pass  # the tab closed or the server is shutting down; state is saved as it changes, so nothing is lost


async def run(args: argparse.Namespace) -> None:
    global STEP_PICTURES
    # Both keys are optional: without Claude there's no vetting (so no real photos), without OpenAI no illustrations.
    claude = anthropic.AsyncAnthropic() if "ANTHROPIC_API_KEY" in os.environ else None
    STEP_PICTURES = StepPictures(STEP_PICTURES_DIR, claude, os.environ.get("OPENAI_API_KEY"))
    # Only pages served from localhost (on any port, so SSH forwarding to another local port works) may connect;
    # this stops other sites open in the browser from driving the agent, and its orders, through localhost.
    origins = [re.compile(r"http://(localhost|127\.0\.0\.1)(:\d+)?")]
    async with serve(
        partial(handle_browser, args=args),
        args.host,
        args.port,
        process_request=process_request,
        origins=origins,
        max_size=None,
        close_timeout=1,
        # Finding or making a step picture takes 10-30 s; the default 10 s request limit would cut it off.
        open_timeout=120,
    ) as server:
        logging.info(f"Basil is ready at http://localhost:{args.port}")
        await server.serve_forever()


def main() -> None:
    load_dotenv(HERE / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--kitchen", default=str(HERE / "kitchen.json"), help="where pantry, plan and timers persist")
    parser.add_argument("--api-base", default="wss://api.phonic.ai")
    parser.add_argument("--voice", default="jerome")
    parser.add_argument("--speed", type=float, default=1.5, help="speaking speed, 0.5 to 1.5")
    parser.add_argument(
        "--instacart",
        choices=["auto", "off", "browser"],
        default="auto",
        help="fill your Instacart cart with a browser agent in the shopper/ sandbox (needs ANTHROPIC_API_KEY);"
        " auto turns it on when the sandbox and key are there",
    )
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Per-request HTTP and connection lines from websockets are noise here; keep its warnings and errors.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        args = parser.parse_args()
        instacart(args)  # fail at startup, not on the first tab, if the key is missing
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
