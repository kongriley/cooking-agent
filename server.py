"""Basil's voice app: serves the page and bridges each browser tab to its own Phonic conversation.

    uv run server.py     # then open http://localhost:8000

The browser only does audio and display; the API key, tools and timers stay in this process.
"""

import argparse
import asyncio
import faulthandler
import json
import logging
import os
import re
import signal
import urllib.parse
from functools import partial
from http import HTTPStatus
from pathlib import Path

from dotenv import load_dotenv
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

import shopping
from images import Images, StepPictures
from kitchen import Kitchen, Timer
from session import Session, advisor, instacart, open_session
from shopping import sign_in
from timers import Timers
from tools import Toolbox

HERE = Path(__file__).parent
# Events after which the panes need fresh state.
STATE_CHANGING_EVENTS = {"tool_result", "timer_fired", "reminder", "step_due", "conversation_created", "cart_update"}


IMAGES = Images(HERE / "images.json")
STEP_PICTURES_DIR = HERE / "step_pictures"
STEP_PICTURES: StepPictures  # made at startup, once the keys are in the environment
SIGNING_IN: set[asyncio.Task] = set()  # an Instacart sign-in window waiting for the cook


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
    if url.path == "/shopper-frame":
        # The shopping browser as it last looked, so the cook can watch it shop from the Shopping sheet.
        frame = shopping.latest_frame
        if frame is None:
            return connection.respond(HTTPStatus.NOT_FOUND, "nothing yet\n")
        return Response(
            HTTPStatus.OK.value, "OK", Headers({"Content-Type": "image/jpeg", "Cache-Control": "no-store"}), frame
        )
    if url.path.startswith("/cart-shots/"):
        file = shopping.SHOTS_DIR / Path(url.path).name  # .name keeps requests inside the folder
        if not file.is_file():
            return connection.respond(HTTPStatus.NOT_FOUND, "no picture\n")
        return Response(HTTPStatus.OK.value, "OK", Headers({"Content-Type": "image/jpeg"}), file.read_bytes())
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
    except Exception as e:
        if (
            message.get("action") == "show_shopper"
        ):  # e.g. the shopping window was closed; the next conversation reopens it
            logging.warning(f"couldn't show the shopping browser: {e}")
            return None
        if not isinstance(e, (KeyError, ValueError)):
            raise
        logging.warning(f"ignored a tap on something that's gone: {message}")
        return None


async def _apply_action(toolbox: Toolbox, message: dict) -> dict | None:
    match message["action"]:
        case "step":
            step = toolbox.kitchen.step(message["step_id"])
            toolbox.update_step(step.id, message["status"])
            note = (
                f"The cook tapped '{step.title}' as {message['status']} in the app. The screen now shows what's next;"
                " don't read it out. Say a few words only if they need something it doesn't show: a cue, a warning,"
                " or what to do while something cooks. Otherwise say nothing."
            )
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
        case "kitchen":
            # An edit in the Kitchen sheet: items with how much and where, or the setup (burners, skill, diet...).
            fields = ["items", "burners", "ovens", "cooks", "skill", "dietary_notes", "cook_names", "store"]
            changes = {k: message[k] for k in fields if k in message}
            toolbox.update_kitchen(**changes)
            note = f"[The cook edited their kitchen in the app: {json.dumps(changes)}. Say nothing about it.]"
            return {"type": "add_system_message", "system_message": note}
        case "serve":
            toolbox.set_serve_time(message["at"])
            when = f"to {message['at']}" if message["at"] else "off"
            note = f"[The cook set the serve time {when} in the app; the plan has moved to fit. Say nothing about it.]"
            return {"type": "add_system_message", "system_message": note}
        case "forget_item":
            del toolbox.kitchen.inventory[message["name"]]
            toolbox.save()
            note = (
                f"[The cook removed '{message['name']}' from their kitchen in the app; it's unknown now. Say nothing.]"
            )
            return {"type": "add_system_message", "system_message": note}
        case "ingredients":
            # An edit to a dish's ingredient list from the screen (a rename is a remove and an add).
            toolbox.update_ingredients(message["dish"], message.get("items"), message.get("remove"))
            changes = {k: message[k] for k in ("items", "remove") if message.get(k)}
            note = f"[The cook edited the ingredients for {message['dish']} in the app: {json.dumps(changes)}. Say nothing.]"
            return {"type": "add_system_message", "system_message": note}
        case "open_cart":
            # A store's own cart lives in the shopping browser: open it as a window to review and pay.
            if not str(message.get("url", "")).startswith("https://"):
                return None
            SIGNING_IN.add(task := asyncio.create_task(shopping.show_cart(message["url"])))
            task.add_done_callback(SIGNING_IN.discard)
            return None
        case "clear_carts":
            toolbox.clear_carts(message.get("store"), message.get("at"))
            return {
                "type": "add_system_message",
                "system_message": "[The cook cleared the shopping list. Say nothing.]",
            }
        case "clear_finished":
            toolbox.clear_plan(None)
            toolbox.kitchen.finished_at = None
            toolbox.save()
            return {
                "type": "add_system_message",
                "system_message": "[The cook cleared the finished meal. Say nothing.]",
            }
        case "close_how":
            toolbox.kitchen.how_to = None
            toolbox.save()
            return {"type": "add_system_message", "system_message": "[The cook closed the how-to card. Say nothing.]"}
        case "show_shopper":
            # The local shopping browser runs headless; for the one-time sign-in it opens as a window, and goes back
            # to headless once the cook's through. That takes minutes, so it runs on its own.
            SIGNING_IN.add(task := asyncio.create_task(sign_in()))
            task.add_done_callback(SIGNING_IN.discard)
            return None
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

    quiet = "quiet=1" in urllib.parse.urlsplit(browser.request.path).query
    # Checking for the shopping browser makes blocking calls; off the event loop, so no one's audio stalls meanwhile.
    shopper = await asyncio.to_thread(instacart, args)
    session_args = (args.voice, args.speed, shopper, advisor(), args.api_base, quiet, args.fresh_after)
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
    # Step pictures are generated with OpenAI (none without a key).
    STEP_PICTURES = StepPictures(STEP_PICTURES_DIR, os.environ.get("OPENAI_API_KEY"))
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


def stopped_from_outside(*_) -> None:
    """Something outside stopped the server (it would exit 143): say so, show what it was in the middle of, and stop
    the way Ctrl-C does."""
    logging.error("Received SIGTERM from outside the server; stopping. It was doing:")
    faulthandler.dump_traceback(all_threads=True)
    raise KeyboardInterrupt


def main() -> None:
    load_dotenv(HERE / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--kitchen", default=str(HERE / "kitchen.json"), help="where pantry, plan and timers persist")
    parser.add_argument("--api-base", default="wss://api.phonic.ai")
    parser.add_argument("--voice", default="jerome")
    parser.add_argument(
        "--fresh-after",
        type=int,
        default=5,
        help="reset the Phonic conversation's memory (briefed with a recap) after this many turns, at the next pause; 0 never",
    )
    parser.add_argument("--speed", type=float, default=1.15, help="speaking speed, 0.5 to 1.5")
    parser.add_argument(
        "--instacart",
        choices=["auto", "off", "browser", "local"],
        default="auto",
        help="fill your Instacart cart with a browser agent (needs ANTHROPIC_API_KEY): 'browser' drives the Docker"
        " sandbox in shopper/, 'local' a Chrome window of its own on this computer; auto uses the sandbox if it's"
        " running, else local Chrome",
    )
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stopped_from_outside)
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
