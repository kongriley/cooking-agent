"""Talk to Basil by typing, in the terminal. Lines are your turns; '/wait <seconds>' pauses, '/quit' exits.

Handy for scripted runs (`uv run client.py < script.txt`); `server.py` is the voice app.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from session import Session, advisor, instacart, open_session

HERE = Path(__file__).parent
# A reply is complete once the assistant has been quiet this long; long enough to span the pause while it
# consults the supervisor.
TEXT_TURN_QUIET_SECONDS = 5
TEXT_TURN_TIMEOUT_SECONDS = 90


async def print_event(message: dict) -> None:
    match message["type"]:
        case "conversation_created":
            print(f"[conversation {message['conversation_id']}]", flush=True)
        case "assistant_started_speaking":
            print("\nbasil: ", end="", flush=True)
        case "audio_chunk" if message["text"]:
            print(message["text"], end="", flush=True)
        case "input_text":
            print(f"\nyou: {message['text']}", flush=True)
        case "tool_result":
            print(f"\n  [tool] {message['tool_name']}({json.dumps(message['parameters'])})", flush=True)
            print(f"  [→] {json.dumps(message['output'])[:600]}", flush=True)
        case "phonic_closed":
            print(f"\n[Phonic ended the conversation: {message['reason'] or 'no reason given'}]", flush=True)
        case "timer_fired":
            print(f"\n⏰ {message['text']}", flush=True)


async def wait_for_reply(session: Session, since: float) -> None:
    deadline = time.monotonic() + TEXT_TURN_TIMEOUT_SECONDS
    while time.monotonic() < deadline and not session.ended.is_set():
        quiet = time.monotonic() - session.last_assistant_activity
        done = session.last_assistant_activity > since and not session.assistant_speaking and not session.tool_running
        if done and quiet > TEXT_TURN_QUIET_SECONDS:
            return
        await asyncio.sleep(0.1)


async def run(args: argparse.Namespace) -> None:
    session_args = (args.voice, args.speed, instacart(args), advisor(), args.api_base)
    async with open_session(Path(args.kitchen), print_event, *session_args) as session:
        await wait_for_reply(session, since=0)  # welcome message
        while not session.ended.is_set():
            raw = await asyncio.to_thread(sys.stdin.readline)
            line = raw.strip()
            if not raw or line == "/quit":
                return
            if line.startswith("/wait"):
                await asyncio.sleep(float(line.split()[1]))
            elif line:
                sent_at = time.monotonic()
                await session.send({"type": "generate_reply", "system_message": None, "user_message": line})
                await wait_for_reply(session, since=sent_at)


def main() -> None:
    load_dotenv(HERE / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kitchen", default=str(HERE / "kitchen.json"), help="where pantry, plan and timers persist")
    parser.add_argument("--api-base", default="wss://api.phonic.ai")
    parser.add_argument("--voice", default="jerome")
    parser.add_argument("--speed", type=float, default=1.15, help="speaking speed, 0.5 to 1.5")
    parser.add_argument(
        "--instacart",
        choices=["auto", "off", "browser"],
        default="auto",
        help="fill your Instacart cart with a browser agent in the shopper/ sandbox (needs ANTHROPIC_API_KEY);"
        " auto turns it on when the sandbox and key are there",
    )
    args = parser.parse_args()
    instacart(args)  # fail at startup, not on the first tab, if the key is missing
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
