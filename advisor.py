"""Basil's supervisor: a stronger model it consults for the decisions worth getting right.

Running the consult as Basil's own tool, rather than Phonic's built-in supervisor, means the server knows when one
starts and ends, so the screen can show that Basil is thinking instead of looking frozen.
"""

import json

import aiohttp
import anthropic

import openai_api

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """\
You advise Basil, a voice cooking coach talking a home cook through a meal in real time. Basil asks you when a \
decision is worth getting right: which recipe fits, how to plan the steps, how to recover when something goes wrong.

Answer in at most four short lines Basil can act on immediately: the recommendation, the key numbers (amounts, times, \
temperatures), and the one thing most likely to go wrong. Work only from what the cook has said and the kitchen \
state you're given; say what's unknown rather than assuming it. No preamble.
"""


PLAN_PROMPT = """\
You write the cooking plan Basil, a voice cooking coach, will run with a home cook. Give every ingredient and tool \
with its amount, and the steps in cookbook order. Each step: a 2-5 word heading, and an instruction written like a \
chef's prep list, at most 15 words, verb first, numbers as digits, the done-cue last ("Garlic in. Pale gold, 2 min."). \
Realistic times; waiting steps (preheat, boil, simmer, bake, rest, proof) are hands-off so work happens alongside; \
dependencies only where one step truly needs another; step ids unique and prefixed with a short form of the dish. \
No steps for gathering, checking or setting out ingredients or tools: the screen lists those. Start with the first \
real action. Work only from what the cook has and has said; season and taste as you go.
"""


def plan_schema() -> dict:
    # The same shape set_plan takes, so a plan goes straight into the kitchen.
    from tools import TOOL_SPECS

    params = TOOL_SPECS["set_plan"][1]["properties"]
    schema = {"ingredients": params["ingredients"], "steps": params["steps"]}
    return {"type": "object", "properties": schema, "required": list(schema), "additionalProperties": False}


def plan_request(dish: str, notes: str, context: str) -> str:
    return f"{context}\n\nPlan: {dish}.\nWhat the cook wants: {notes or 'nothing more said'}"


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

    async def plan(self, dish: str, notes: str, context: str) -> dict:
        """A whole plan for a dish ({ingredients, steps}), in the shape set_plan takes."""
        response = await self.client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=PLAN_PROMPT,
            tools=[
                {"name": "save_plan", "description": "Save the plan.", "input_schema": plan_schema(), "strict": True}
            ],
            tool_choice={"type": "tool", "name": "save_plan"},
            messages=[{"role": "user", "content": plan_request(dish, notes, context)}],
            output_config={"effort": "low"},  # the cook is waiting; a home dish doesn't need deep reasoning
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        raise ValueError("the planner didn't return a plan; plan it yourself with set_plan")


class OpenAIAdvisor:
    """The same supervisor on OpenAI, for when there's an OpenAI key and no Anthropic one."""

    async def advise(self, question: str, context: str) -> str:
        reply = await openai_api.ask(f"{context}\n\nBasil asks: {question}", SYSTEM_PROMPT, effort="medium")
        return reply or "No advice on this one; use your own judgment."

    async def plan(self, dish: str, notes: str, context: str) -> dict:
        body = {
            "instructions": PLAN_PROMPT,
            "input": plan_request(dish, notes, context),
            "text": {"format": {"type": "json_schema", "name": "plan", "schema": plan_schema(), "strict": True}},
            # A plan for a home dish doesn't need deep reasoning, and the cook is waiting: low keeps it near 15 s.
            "reasoning": {"effort": "low"},
        }
        async with aiohttp.ClientSession(timeout=openai_api.TIMEOUT) as http:
            return json.loads(openai_api.text(await openai_api.respond(http, body)))
