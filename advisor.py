"""Basil's supervisor: a stronger model it consults for the decisions worth getting right.

Running the consult as Basil's own tool, rather than Phonic's built-in supervisor, means the server knows when one
starts and ends, so the screen can show that Basil is thinking instead of looking frozen.
"""

import anthropic

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """\
You advise Basil, a voice cooking coach talking a home cook through a meal in real time. Basil asks you when a \
decision is worth getting right: which recipe fits, how to plan the steps, how to recover when something goes wrong.

Answer in a few short lines Basil can act on immediately: the recommendation, the key numbers (amounts, times, \
temperatures), and the one thing most likely to go wrong. Work only from what the cook has said and the kitchen \
state you're given; say what's unknown rather than assuming it. No preamble.
"""


class Advisor:
    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self.client = client

    async def advise(self, question: str, context: str) -> str:
        response = await self.client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"{context}\n\nBasil asks: {question}"}],
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return "No advice on this one; use your own judgment."
        return "".join(block.text for block in response.content if block.type == "text").strip()
