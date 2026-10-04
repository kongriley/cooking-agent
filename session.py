"""One Phonic STS conversation for Basil: runs its tools and timer alerts and reports every event to a front end."""

import asyncio
import base64
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anthropic
import websockets

import openai_api
from advisor import Advisor, OpenAIAdvisor
from kitchen import Kitchen
from shopping import VIEWER_URL, InstacartShopper, OpenAIShopper, find_chrome, sandbox_running, start_local_browser
from timers import Timers
from tools import ASYNC_TOOLS, TOOL_SPECS, Toolbox, tool_definitions

# Re-read at the start of every conversation, so edits take effect on the next one without a restart.
SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"


# How Basil says the French kitchen words he lives by; a chef from Lyon who mangles "mise en place" isn't one.
PRONUNCIATIONS = {
    "Lyon": "lee-OHN", "mise en place": "meez ahn PLAHS", "bouchon": "boo-SHOHN", "crème fraîche": "krem FRESH",
    "creme fraiche": "krem FRESH", "beurre blanc": "bur BLAHN", "beurre noisette": "bur nwah-ZET", "nappe": "NAP",
    "jus": "ZHOO", "fond": "FOHN", "gruyère": "groo-YAIR", "gruyere": "groo-YAIR", "brunoise": "broon-WAHZ",
    "chiffonade": "shif-uh-NAHD", "roux": "ROO", "vinaigrette": "vin-uh-GRET",
}  # fmt: skip
# Heard more reliably: his name (which is how the cook says they're talking to him), and kitchen words.
BOOSTED_KEYWORDS = [
    "Basil", "mise en place", "julienne", "chiffonade", "brunoise", "crème fraîche", "beurre blanc", "roux",
    "deglaze", "sous vide", "gochujang", "za'atar",
]  # fmt: skip


RESUME_INSTRUCTION = (
    "[The cook just reloaded the page and is back. Don't introduce yourself or recap. In one short line, pick up"
    " where you left off: usually the next thing to do.]"
)
SAMPLE_RATE = 16_000
# The API rejects audio chunks longer than 40 ms.
CHUNK_SECONDS = 0.02
ALERT_ACK_TIMEOUT_SECONDS = 8
ALERT_ATTEMPTS = 3


@dataclass(frozen=True)
class Urgency:
    """How an unprompted line waits its turn: for `quiet` seconds of nobody talking, but no longer than `patience`."""

    quiet: float
    patience: float


# A timer can't wait long (the pasta overcooks), so it takes the first short pause and cuts in after 20 s. A reminder
# or a step coming due waits for the conversation to wind down; the cart can wait longest.
TIMER = Urgency(quiet=1.5, patience=20)
DUE = Urgency(quiet=3, patience=90)
REMINDER = Urgency(quiet=4, patience=180)
LATER = Urgency(quiet=4, patience=300)
# How often to check whether a step's start time has come.
DUE_CHECK_SECONDS = 10
# A Phonic conversation keeps every turn and tool result, so a long cook makes it slow and costly. After this many turns
# from the cook, the next real pause starts a fresh one, briefed with the recap. 0 never does.
FRESH_AFTER_TURNS = 5
# What counts as a real pause: nobody talking for this long, nothing running, nothing waiting to be said.
FRESH_QUIET_SECONDS = 8

OnEvent = Callable[[dict], Awaitable[None]]


def join_speech(said: str, more: str) -> str:
    """Append a streamed text chunk. Separate utterances in one turn (speech before and after a tool call)
    arrive with no space between them, even when the first doesn't end in punctuation."""
    boundary = said and not said[-1].isspace() and not more[:1].isspace()
    if boundary and (said[-1] in ".!?,:;" or more[:1].isupper()):
        return f"{said} {more}"
    return said + more


def together(messages: list[str]) -> str:
    """One unprompted turn from several that came due at once."""
    if len(messages) == 1:
        return messages[0]
    parts = " ".join(m.strip().removeprefix("[").removesuffix("]") for m in messages)
    return f"[Several things at once. Say them together, most urgent first, in a line or two: {parts}]"


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
        self.tools_running = 0  # a consult can be silent for half a minute without the turn being over
        self.background: set[asyncio.Task] = set()  # slow tools running alongside the conversation
        self.resume_instruction: str | None = None
        self.ended = asyncio.Event()
        self.last_user_speech = 0.0
        self.turns = 0  # what the cook has said in this conversation
        self.announcing = False  # an unprompted line is waiting for its gap or being said
        self.fresh_due = asyncio.Event()  # time for a fresh conversation
        # Lines Basil says unprompted (timers, reminders, steps coming due), one at a time, each when there's a gap.
        self.announcements: asyncio.Queue[tuple[str, Urgency]] = asyncio.Queue()

    async def send(self, message: dict) -> None:
        await self.socket.send(json.dumps(message))

    async def send_audio(self, audio_b64: str) -> None:
        self.last_user_audio = time.monotonic()
        await self.send({"type": "audio_chunk", "audio": audio_b64})

    async def alert(self, label: str, text: str) -> None:
        await self.on_event({"type": "timer_fired", "label": label, "text": text})
        await self.announce(f"[{text} Tell the cook right now, in a few words.]", TIMER)

    async def remind(self, label: str, message: str) -> None:
        await self.on_event({"type": "reminder", "label": label, "text": message})
        await self.announce(f"[A reminder you set has come due: {message} Say it now, in a few words.]", REMINDER)

    async def cart_done(self, order: dict) -> None:
        await self.on_event({"type": "cart_update"})
        await self.announce(
            f"[The Instacart cart job finished: {json.dumps(order)}. The screen shows the cart. In one short line, say"
            " it's ready and name only what couldn't be found. If needs_login, just say to sign in on the screen.]",
            LATER,
        )

    async def nudge(self, text: str) -> None:
        """A heads-up before a long timer goes off; it can wait as long as a timer can."""
        await self.announce(f"[{text}]", TIMER)

    async def announce(self, system_message: str, urgency: Urgency) -> None:
        await self.announcements.put((system_message, urgency))

    def busy(self, quiet: float) -> bool:
        now = time.monotonic()
        return (
            self.user_speaking
            or self.assistant_speaking
            or self.tool_running
            or now - self.last_assistant_activity < quiet
            or now - self.last_user_speech < quiet
        )

    async def speak_announcements(self) -> None:
        """Say queued lines one after another: each waits for a gap (or runs out of patience), and the next waits for
        it to finish. Retries if Phonic drops the turn (e.g. mid uninterruptible turn)."""
        while True:
            system_message, urgency = await self.announcements.get()
            self.announcing = True
            start = time.monotonic()
            while self.busy(urgency.quiet) and time.monotonic() - start < urgency.patience:
                await asyncio.sleep(0.2)
            # Whatever else came due meanwhile (three timers at once) is said together, in one breath.
            waiting = [system_message]
            while not self.announcements.empty():
                waiting.append(self.announcements.get_nowait()[0])
            system_message = together(waiting)
            for _ in range(ALERT_ATTEMPTS):
                self.assistant_started.clear()
                await self.send({"type": "generate_reply", "system_message": system_message})
                try:
                    await asyncio.wait_for(self.assistant_started.wait(), ALERT_ACK_TIMEOUT_SECONDS)
                    break
                except TimeoutError:
                    logging.warning("unprompted turn got no reply, retrying")
            else:
                logging.error(f"unprompted turn never delivered: {system_message}")
            while self.assistant_speaking:
                await asyncio.sleep(0.2)
            self.announcing = False

    async def watch_for_a_fresh_start(self, after_turns: int, check_seconds: float = 2) -> None:
        """Once the conversation is long, ask for a fresh one at the next real pause, never mid-exchange, mid-alert,
        mid-tool or while the cart is filling (that job lives with this conversation)."""
        while after_turns:
            await asyncio.sleep(check_seconds)
            cart = self.toolbox.cart_job
            if (
                self.turns >= after_turns
                and not self.busy(FRESH_QUIET_SECONDS)
                and not self.announcing
                and self.announcements.empty()
                and (cart is None or cart.done())
            ):
                logging.info(f"{self.turns} turns in: starting a fresh conversation")
                self.fresh_due.set()
                return

    async def watch_the_clock(self) -> None:
        """Speak up when a step's start time comes, the way a host with a plan does: 'Sam, start the beans.'"""
        while True:
            await asyncio.sleep(DUE_CHECK_SECONDS)
            due = self.toolbox.clock_due()
            if due:
                await self.on_event({"type": "step_due"})
                await self.announce(
                    f"[It's time to start: {'; '.join(due)}. Tell them now in a few words, by name if more than one"
                    " person is cooking.]",
                    DUE,
                )

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
                        if LEAKED_TOOL.search(message["text"]):
                            message = {**message, "text": LEAKED_TOOL.sub("", message["text"])}
                case "assistant_started_speaking":
                    self.assistant_speaking = True
                    self.assistant_started.set()
                    self.last_assistant_activity = time.monotonic()
                    self.reply = ""
                case "assistant_finished_speaking" | "interrupted_response":
                    self.assistant_speaking = False
                    self.last_assistant_activity = time.monotonic()
                    if self.reply.strip():
                        self.remember("basil", self.reply.strip())  # a leaked tool name is logged, then left out
                    self.reply = ""
                case "input_text":
                    self.turns += 1
                    self.remember("cook", message["text"])
                case "user_started_speaking":
                    self.user_speaking = True
                case "user_finished_speaking":
                    self.user_speaking = False
                    self.last_user_speech = time.monotonic()
                case "tool_call" if message["tool_name"] not in TOOL_SPECS:
                    # A Phonic built-in (choose_not_to_respond) runs on Phonic's side; there's nothing to answer.
                    logging.info(f"built-in tool {message['tool_name']}")
                case "tool_call" if message["tool_name"] in ASYNC_TOOLS:
                    # Slow (a careful chef thinking): it runs alongside, so the conversation keeps flowing meanwhile.
                    self.background.add(task := asyncio.create_task(self.run_tool(message)))
                    task.add_done_callback(self.background.discard)
                    continue
                case "tool_call":
                    await self.run_tool(message)
                    continue
                case "error":
                    logging.error(f"Phonic error: {message}")
                    await self.on_event(message)
                    raise RuntimeError(f"Phonic error: {message}")
            await self.on_event(message)
            if message["type"] == "assistant_ended_conversation":
                return

    async def run_tool(self, call: dict) -> None:
        self.last_assistant_activity = time.monotonic()
        name, parameters = call["tool_name"], call["parameters"] or {}
        # Lets the screen show work in progress (a consult can take half a minute).
        await self.on_event({"type": "tool_started", "tool_name": name})
        self.tools_running += 1
        try:
            output = await self.toolbox.call(name, parameters)
        finally:
            self.tools_running -= 1
            self.last_assistant_activity = time.monotonic()
        logging.info(f"tool {name} {json.dumps(parameters)[:400]} -> {json.dumps(output)[:400]}")
        await self.send({"type": "tool_call_output", "tool_call_id": call["tool_call_id"], "output": output})
        await self.on_event({"type": "tool_result", "tool_name": name, "parameters": parameters, "output": output})

    @property
    def tool_running(self) -> bool:
        return self.tools_running > 0

    def remember(self, who: str, text: str) -> None:
        logging.info(f"{who}: {text}")
        # A tool's name said out loud means the model voiced a call instead of making it; worth seeing in the log.
        if who == "basil" and (said := [n for n in SPOKEN_TOOL_NAMES if n in text.lower().replace(" ", "_")]):
            logging.warning(f"Basil said a tool name out loud: {said}")
            text = LEAKED_TOOL.sub("", text).strip()
            if not text:
                return
        self.toolbox.kitchen.remember(who, text)
        self.toolbox.save()

    async def stream_silence(self) -> None:
        """Fill the audio stream with silence whenever no mic audio is arriving, keeping the conversation alive."""
        silence = base64.b64encode(b"\x00\x00" * int(SAMPLE_RATE * CHUNK_SECONDS)).decode()
        while True:
            if time.monotonic() - self.last_user_audio > 2 * CHUNK_SECONDS:
                await self.send({"type": "audio_chunk", "audio": silence})
            await asyncio.sleep(CHUNK_SECONDS)


# Names that should never be heard: Basil's tools, and Phonic's built-ins (which Phonic prefixes with "tool_").
SPOKEN_TOOL_NAMES = [*TOOL_SPECS, "choose_not_to_respond", "natural_conversation_ending"]
# A tool's name that leaked into speech ("tool_choose_not_to_respond"): kept out of captions and history.
LEAKED_TOOL = re.compile(r"\btool_[a-z_]+\b\.?")

# Appended to the system prompt when shopping is off, since the prompt file describes the Instacart flow.
NO_SHOPPING_NOTE = (
    "## Shopping is off\nYou can't send shopping lists. When the cook needs something, say what to buy in a few words."
)


def advisor() -> Advisor | OpenAIAdvisor | None:
    """Basil's own supervisor: Claude when there's an Anthropic key, else OpenAI; with neither, Phonic's built-in one
    does the job (which the screen can't show as thinking)."""
    if "ANTHROPIC_API_KEY" in os.environ:
        return Advisor(anthropic.AsyncAnthropic())
    if openai_api.available():
        return OpenAIAdvisor()
    logging.warning("No ANTHROPIC_API_KEY or OPENAI_API_KEY: using Phonic's supervisor, which can't show as thinking.")
    return None


def instacart(args) -> InstacartShopper | None:
    """The Instacart cart agent when shopping is on (None when off); checks its browser and Claude key up front.

    'browser' is the Docker sandbox; 'local' opens a Chrome window of its own on this computer; 'auto' uses the sandbox
    when it's running, else local Chrome. With 'auto', a missing piece turns shopping off with a warning saying what's
    missing, instead of stopping.
    """
    if args.instacart == "off":
        return None
    problem, shopper = None, None

    # Claude drives the browser when there's an Anthropic key, OpenAI's computer tool when there's only an OpenAI one.
    def make(viewer_url: str | None = VIEWER_URL) -> InstacartShopper:
        if "ANTHROPIC_API_KEY" in os.environ:
            return InstacartShopper(anthropic.AsyncAnthropic(), viewer_url=viewer_url)
        return OpenAIShopper(viewer_url=viewer_url)

    if "ANTHROPIC_API_KEY" not in os.environ and not openai_api.available():
        problem = "it needs ANTHROPIC_API_KEY or OPENAI_API_KEY; start under `infisical run --env=dev --`"
    elif args.instacart in ("auto", "browser") and sandbox_running():
        shopper = make()
    elif args.instacart == "browser":
        problem = "the shopping browser isn't running; start it with `docker start basil-shopper`"
    elif chrome := find_chrome():
        try:
            start_local_browser(chrome)
            shopper = make(viewer_url=None)
        except (OSError, RuntimeError) as e:
            problem = f"couldn't open Chrome for it ({e})"
    else:
        problem = "there's no Chrome to shop with; install Google Chrome, or run the Docker sandbox in shopper/"
    if shopper is not None:
        return shopper
    if args.instacart != "auto":
        raise SystemExit(f"Shopping is on but {problem}.")
    logging.warning(f"Shopping is off: {problem}.")
    return None


async def check_sign_in(session: Session, instacart: InstacartShopper) -> None:
    try:
        signed_in = await instacart.signed_in()
        if signed_in is None:
            return  # busy shopping (so signed in enough); checked again next conversation
        session.toolbox.signed_in = signed_in
    except Exception:  # the check is a convenience; a sandbox hiccup shouldn't stop the conversation
        logging.exception("couldn't check the Instacart sign-in")
        return
    await session.on_event({"type": "cart_update"})  # refreshes the screen


@asynccontextmanager
async def open_session(
    kitchen_path: Path,
    on_event: OnEvent,
    voice: str,
    speed: float,
    instacart: InstacartShopper | None,
    thinker: Advisor | None,
    api_base: str,
    quiet: bool = False,
    fresh_after: int = FRESH_AFTER_TURNS,
) -> AsyncIterator[Session]:
    """Connect to Phonic, start the conversation, and keep the receive and silence loops running while open.

    `quiet` is for the page reconnecting on its own (always-on listening): Basil picks up silently instead of
    greeting the cook or announcing where things stand.
    """
    kitchen = Kitchen.load(kitchen_path)
    config = {
        "type": "config",
        # Basil greets in his own words (the prompt says how), different every time; never on a reconnect.
        "generate_welcome_message": True,
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
        "pronunciation_dictionary": [{"word": w, "pronunciation": say} for w, say in PRONUNCIATIONS.items()],
        "boosted_keywords": BOOSTED_KEYWORDS,
    }
    headers = {"Authorization": f"Bearer {os.environ['PHONIC_API_KEY']}"}
    url = f"{api_base}/v1/sts/ws"
    # A short close timeout keeps Ctrl-C and tab closes snappy; everything is already saved by then.
    async with websockets.connect(url, additional_headers=headers, max_size=None, close_timeout=1) as socket:
        session = Session(socket, on_event)
        timers = Timers(
            kitchen,
            save=lambda: kitchen.save(kitchen_path),
            alert=session.alert,
            nudge=session.nudge,
            remind=session.remind,
        )
        session.toolbox = Toolbox(
            kitchen, kitchen_path, timers, instacart, on_cart_done=session.cart_done, advisor=thinker
        )
        # Checked alongside the conversation, not before it (it loads a page, ~4 s), so the screen can ask for the
        # one-time sign-in before an order fails on it.
        sign_in_check = asyncio.create_task(check_sign_in(session, instacart)) if instacart is not None else None
        # With history on disk this is a reconnect: brief the agent instead of greeting the cook from scratch.
        recap = session.toolbox.recap()
        prompt = SYSTEM_PROMPT_PATH.read_text() + ("" if instacart else f"\n\n{NO_SHOPPING_NOTE}")
        config["system_prompt"] = prompt + (f"\n\n{recap}" if recap else "")
        if recap or quiet:
            config["generate_welcome_message"] = False
        if recap and not quiet:
            session.resume_instruction = RESUME_INSTRUCTION
        await session.send(config)
        timers.restore()
        receiving = asyncio.create_task(session.receive())
        silence = asyncio.create_task(session.stream_silence())
        speaking = asyncio.create_task(session.speak_announcements())
        session.toolbox.clock_due()  # what's waiting on the clock now; only steps that come due later are announced
        clock = asyncio.create_task(session.watch_the_clock())
        freshness = asyncio.create_task(session.watch_for_a_fresh_start(fresh_after))
        try:
            yield session
        finally:
            for task in [
                receiving,
                silence,
                speaking,
                clock,
                freshness,
                *session.background,
                sign_in_check,
                *timers.tasks.values(),
                session.toolbox.cart_job,
            ]:
                if task is None:
                    continue
                task.cancel()
        if receiving.done() and not receiving.cancelled() and receiving.exception() is not None:
            raise receiving.exception()
