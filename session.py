"""One Phonic STS conversation for Basil: runs its tools and timer alerts and reports every event to a front end."""

import asyncio
import base64
import json
import logging
import os
import time
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
import websockets

from advisor import Advisor
from kitchen import Kitchen
from shopping import DEVTOOLS_URL, InstacartShopper
from timers import Timers
from tools import Toolbox, tool_definitions

# Re-read at the start of every conversation, so edits take effect on the next one without a restart.
SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"
WELCOME_MESSAGE = "Basil here. What are we cooking?"
RESUME_INSTRUCTION = (
    "[The cook just reloaded the page and is back. Don't introduce yourself or recap. In one short line, pick up"
    " where you left off: usually the next thing to do.]"
)
SAMPLE_RATE = 16_000
# The API rejects audio chunks longer than 40 ms.
CHUNK_SECONDS = 0.02
# How long a timer alert waits for a gap before cutting in. Basil finishes its thought, then interrupts itself
# like a chef would; the cook gets longer to finish theirs. A late alert is worse than talking over someone.
ASSISTANT_DEFER_SECONDS = 3
USER_DEFER_SECONDS = 10
ALERT_ACK_TIMEOUT_SECONDS = 8
ALERT_ATTEMPTS = 3

OnEvent = Callable[[dict], Awaitable[None]]


def join_speech(said: str, more: str) -> str:
    """Append a streamed text chunk. Separate utterances in one turn (speech before and after a tool call)
    arrive with no space between them, even when the first doesn't end in punctuation."""
    boundary = said and not said[-1].isspace() and not more[:1].isspace()
    if boundary and (said[-1] in ".!?,:;" or more[:1].isupper()):
        return f"{said} {more}"
    return said + more


class Session:
    def __init__(self, socket: websockets.ClientConnection, on_event: OnEvent) -> None:
        self.socket = socket
        self.on_event = on_event
        self.toolbox: Toolbox  # set once timers exist, since they alert through this session
        self.user_speaking = False
        self.assistant_speaking = False
        self.assistant_started = asyncio.Event()
        self.last_assistant_activity = 0.0
        self.last_user_audio = 0.0
        self.reply = ""  # what the agent has said so far this turn
        self.tool_running = False  # a consult can be silent for half a minute without the turn being over
        self.resume_instruction: str | None = None
        self.ended = asyncio.Event()

    async def send(self, message: dict) -> None:
        await self.socket.send(json.dumps(message))

    async def send_audio(self, audio_b64: str) -> None:
        self.last_user_audio = time.monotonic()
        await self.send({"type": "audio_chunk", "audio": audio_b64})

    async def alert(self, label: str, text: str) -> None:
        await self.on_event({"type": "timer_fired", "label": label, "text": text})
        await self.speak_up(f"[{text} Tell the user right now, in one sentence.]")

    async def cart_done(self, order: dict) -> None:
        await self.on_event({"type": "cart_update"})
        await self.speak_up(
            f"[The Instacart cart job finished: {json.dumps(order)}. Tell the cook in a sentence or two what's in the"
            " cart and what couldn't be found, and that they review and pay on Instacart. If needs_login, tell them"
            " to sign in on the shopping browser screen first.]"
        )

    async def speak_up(self, system_message: str) -> None:
        """Make the agent speak unprompted, retrying if the turn is dropped (e.g. mid uninterruptible turn)."""
        for _ in range(ALERT_ATTEMPTS):
            start = time.monotonic()
            while (self.user_speaking and time.monotonic() - start < USER_DEFER_SECONDS) or (
                self.assistant_speaking and time.monotonic() - start < ASSISTANT_DEFER_SECONDS
            ):
                await asyncio.sleep(0.2)
            self.assistant_started.clear()
            await self.send({"type": "generate_reply", "system_message": system_message})
            try:
                await asyncio.wait_for(self.assistant_started.wait(), ALERT_ACK_TIMEOUT_SECONDS)
                return
            except TimeoutError:
                logging.warning("unprompted turn got no reply, retrying")
        logging.error(f"unprompted turn never delivered: {system_message}")

    async def nudge(self, text: str) -> None:
        """Prompt one proactive line from the agent, unless the cook or the agent is already talking."""
        if self.user_speaking or self.assistant_speaking:
            return
        await self.send({"type": "generate_reply", "system_message": f"[{text}]"})

    async def receive(self) -> None:
        try:
            await self._receive()
        except websockets.ConnectionClosed as e:
            # Phonic explains account problems (no payment method, out of credit) in the close reason.
            reason = e.rcvd.reason if e.rcvd else ""
            logging.error(f"Phonic closed the conversation: {reason or e}")
            await self.on_event({"type": "phonic_closed", "reason": reason})
        finally:
            self.ended.set()

    async def _receive(self) -> None:
        async for raw in self.socket:
            message = json.loads(raw)
            match message["type"]:
                case "conversation_created" if self.resume_instruction:
                    await self.send({"type": "generate_reply", "system_message": self.resume_instruction})
                case "audio_chunk":
                    # The server streams silent chunks continuously, so only chunks carrying words count.
                    if message["text"]:
                        self.last_assistant_activity = time.monotonic()
                        self.reply = join_speech(self.reply, message["text"])
                case "assistant_started_speaking":
                    self.assistant_speaking = True
                    self.assistant_started.set()
                    self.last_assistant_activity = time.monotonic()
                    self.reply = ""
                case "assistant_finished_speaking" | "interrupted_response":
                    self.assistant_speaking = False
                    self.last_assistant_activity = time.monotonic()
                    if self.reply.strip():
                        self.remember("basil", self.reply.strip())
                    self.reply = ""
                case "input_text":
                    self.remember("cook", message["text"])
                case "user_started_speaking":
                    self.user_speaking = True
                case "user_finished_speaking":
                    self.user_speaking = False
                case "tool_call":
                    self.last_assistant_activity = time.monotonic()
                    parameters = message["parameters"] or {}
                    # Lets the screen show work in progress (a consult can take half a minute).
                    await self.on_event({"type": "tool_started", "tool_name": message["tool_name"]})
                    self.tool_running = True
                    try:
                        output = await self.toolbox.call(message["tool_name"], parameters)
                    finally:
                        self.tool_running = False
                        self.last_assistant_activity = time.monotonic()
                    logging.info(
                        f"tool {message['tool_name']} {json.dumps(parameters)[:400]} -> {json.dumps(output)[:400]}"
                    )
                    await self.send(
                        {"type": "tool_call_output", "tool_call_id": message["tool_call_id"], "output": output}
                    )
                    message = {
                        "type": "tool_result",
                        "tool_name": message["tool_name"],
                        "parameters": parameters,
                        "output": output,
                    }
                case "error":
                    logging.error(f"Phonic error: {message}")
                    await self.on_event(message)
                    raise RuntimeError(f"Phonic error: {message}")
            await self.on_event(message)
            if message["type"] == "assistant_ended_conversation":
                return

    def remember(self, who: str, text: str) -> None:
        logging.info(f"{who}: {text}")
        self.toolbox.kitchen.remember(who, text)
        self.toolbox.save()

    async def stream_silence(self) -> None:
        """Fill the audio stream with silence whenever no mic audio is arriving, keeping the conversation alive."""
        silence = base64.b64encode(b"\x00\x00" * int(SAMPLE_RATE * CHUNK_SECONDS)).decode()
        while True:
            if time.monotonic() - self.last_user_audio > 2 * CHUNK_SECONDS:
                await self.send({"type": "audio_chunk", "audio": silence})
            await asyncio.sleep(CHUNK_SECONDS)


# Appended to the system prompt when shopping is off, since the prompt file describes the Instacart flow.
NO_SHOPPING_NOTE = (
    "## Shopping is off\nYou can't send shopping lists. When the cook needs something, say what to buy in a few words."
)


def advisor() -> Advisor | None:
    """Basil's own supervisor when a Claude key is available; otherwise Phonic's built-in one does the job."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        logging.warning("No ANTHROPIC_API_KEY: using Phonic's supervisor, which the screen can't show as thinking.")
        return None
    return Advisor(anthropic.AsyncAnthropic())


def instacart(args) -> InstacartShopper | None:
    """The Instacart cart agent when shopping is on (None when off); checks its sandbox and Claude key up front.

    With 'auto', a missing piece turns shopping off with a warning saying what's missing, instead of stopping.
    """
    if args.instacart == "off":
        return None
    problem = None
    if "ANTHROPIC_API_KEY" not in os.environ:
        problem = "it needs ANTHROPIC_API_KEY; start under `infisical run --env=dev --`"
    else:
        try:
            urllib.request.urlopen(f"{DEVTOOLS_URL}/json/version", timeout=3)
        except OSError:
            problem = "the shopping browser isn't running; start it with `docker start basil-shopper`"
    if problem is None:
        return InstacartShopper(anthropic.AsyncAnthropic())
    if args.instacart == "browser":
        raise SystemExit(f"Shopping is on but {problem}.")
    logging.warning(f"Shopping is off: {problem}.")
    return None


@asynccontextmanager
async def open_session(
    kitchen_path: Path,
    on_event: OnEvent,
    voice: str,
    speed: float,
    instacart: InstacartShopper | None,
    thinker: Advisor | None,
    api_base: str,
) -> AsyncIterator[Session]:
    """Connect to Phonic, start the conversation, and keep the receive and silence loops running while open."""
    kitchen = Kitchen.load(kitchen_path)
    config = {
        "type": "config",
        "welcome_message": WELCOME_MESSAGE,
        "voice_id": voice,
        "audio_speed": speed,
        "phonic_model": "phonic_v1",
        # Without Basil's own advisor, "high" turns on Phonic's built-in supervisor consult instead.
        "intelligence_level": "standard" if thinker else "high",
        "tools": tool_definitions(shopping=instacart is not None, thinking=thinker is not None),
        "input_format": f"pcm_{SAMPLE_RATE}",
        "output_format": f"pcm_{SAMPLE_RATE}",
        # Long bakes are mostly silence; the API allows up to an hour before ending the conversation.
        "no_input_end_conversation_sec": 3600,
        "websocket_timeout_sec": 300,
    }
    headers = {"Authorization": f"Bearer {os.environ['PHONIC_API_KEY']}"}
    url = f"{api_base}/v1/sts/ws"
    # A short close timeout keeps Ctrl-C and tab closes snappy; everything is already saved by then.
    async with websockets.connect(url, additional_headers=headers, max_size=None, close_timeout=1) as socket:
        session = Session(socket, on_event)
        timers = Timers(kitchen, save=lambda: kitchen.save(kitchen_path), alert=session.alert, nudge=session.nudge)
        session.toolbox = Toolbox(
            kitchen, kitchen_path, timers, instacart, on_cart_done=session.cart_done, advisor=thinker
        )
        if instacart is not None:
            # Known up front, so the screen can ask for the one-time sign-in before an order fails on it.
            try:
                session.toolbox.signed_in = await instacart.signed_in()
            except Exception:  # the check is a convenience; a sandbox hiccup shouldn't stop the conversation
                logging.exception("couldn't check the Instacart sign-in")
        # With history on disk this is a reconnect: brief the agent instead of greeting the cook from scratch.
        recap = session.toolbox.recap()
        prompt = SYSTEM_PROMPT_PATH.read_text() + ("" if instacart else f"\n\n{NO_SHOPPING_NOTE}")
        config["system_prompt"] = prompt + (f"\n\n{recap}" if recap else "")
        if recap:
            config["welcome_message"] = None
            session.resume_instruction = RESUME_INSTRUCTION
        await session.send(config)
        timers.restore()
        receiving = asyncio.create_task(session.receive())
        silence = asyncio.create_task(session.stream_silence())
        try:
            yield session
        finally:
            for task in [receiving, silence, *timers.tasks.values(), session.toolbox.cart_job]:
                if task is None:
                    continue
                task.cancel()
        if receiving.done() and not receiving.cancelled() and receiving.exception() is not None:
            raise receiving.exception()
