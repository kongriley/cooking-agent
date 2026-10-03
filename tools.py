"""The agent's custom_websocket tools: their schemas (sent in the STS config) and the handlers that run them."""

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from advisor import Advisor
from kitchen import HANDS_ON_SPEED, Kitchen, Step, duration, schedule
from shopping import InstacartShopper
from timers import Timers


def _obj(**properties: Any) -> dict:
    # Strict tool schemas need every property listed as required; optional ones are nullable instead.
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _nullable(type_: str, description: str, **extra: Any) -> dict:
    return {"type": [type_, "null"], "description": description, **extra}


_STR_LIST = {"type": "array", "items": {"type": "string"}}
_STEP = _obj(
    id={"type": "string", "description": "Short unique id, e.g. 'boil_water'. Keep ids stable when replanning."},
    title={
        "type": "string",
        "description": "2-5 words, like a cookbook heading: 'Make the butter block', 'Sear the fish'.",
    },
    text={
        "type": "string",
        "description": "One or two sentences in cookbook voice: imperative, exact, with the cue for when it's done."
        " 'Pound the cold butter between parchment into a 7-inch square, about 1 cm thick. Chill until firm,"
        " about 30 minutes.' Not 'Next, you'll want to...'. Say any extra detail aloud instead.",
    },
    minutes={"type": "number", "description": "How long the step takes for an intermediate cook."},
    after={**_STR_LIST, "description": "Ids of steps that must finish before this one starts."},
    equipment={"type": "string", "enum": ["burner", "oven", "none"]},
    hands_on={"type": "boolean", "description": "True if the cook is busy for the whole step (chopping, stirring)."},
    uses={**_STR_LIST, "description": "Names from the dish's ingredients (and tools) this step uses."},
)
_INGREDIENT = _obj(
    name={"type": "string", "description": "Plain name, e.g. 'butter', 'bread flour', 'stand mixer'."},
    amount={"type": "string", "description": "e.g. '280 g', '2 tbsp', '1 large', '' for equipment."},
)
_WHERE = ["fridge", "freezer", "pantry", "spices", "tools"]

# Instacart carts stay on screen for a day; older ones are history.
CART_SHOWN_FOR_SECONDS = 24 * 3600

# Appended to every tool that should run without spoken narration ("let me check...").
SILENT = " Call it without saying anything first; speak only once you have the result."

# name -> (description, parameters, extra tool options)
TOOL_SPECS: dict[str, tuple[str, dict, dict]] = {
    "get_kitchen": (
        "Get what the cook has told you: profile (burners, ovens, cooks, skill, diet), what's in their kitchen with"
        " how much, recipes in progress, and Instacart carts. Null or unlisted means unknown, not zero or missing.",
        _obj(),
        {},
    ),
    "update_kitchen": (
        "Record what's in the cook's kitchen and how much: from what they say, what a recipe used up, or what they"
        " bought. Fill only what you know and pass null for everything else; never guess a value.",
        _obj(
            items=_nullable(
                "array",
                "Ingredients and tools with how much they have now.",
                items=_obj(
                    name={"type": "string"},
                    have={
                        "type": "string",
                        "description": "In their words or yours: '2 sticks', 'half a bag', 'plenty', 'none'.",
                    },
                    where={"type": "string", "enum": _WHERE},
                ),
            ),
            burners=_nullable("integer", "Number of usable stovetop burners."),
            ovens=_nullable("integer", "Number of ovens."),
            cooks=_nullable("integer", "People cooking (pairs of hands)."),
            skill=_nullable("string", "Cooking skill.", enum=["beginner", "intermediate", "advanced", None]),
            dietary_notes=_nullable("string", "Allergies, diet, likes and dislikes. Replaces the previous notes."),
        ),
        {},
    ),
    "shopping_list": (
        "For each thing a recipe needs, how much the cook has (or unknown). Compare the amounts yourself to decide"
        " what to buy and how much. Ask about unknowns in one short question before buying.",
        _obj(items={"type": "array", "items": _INGREDIENT}),
        {},
    ),
    "fill_instacart_cart": (
        "Add things the cook needs to their Instacart cart. A browser agent does it in the background and takes a"
        " few minutes; keep cooking meanwhile. A system message tells you when it's done. They review and pay on"
        " Instacart themselves. Ask once which store they use if you don't know.",
        _obj(
            store=_nullable("string", "Store on Instacart, e.g. 'Safeway', or null for whichever is offered first."),
            items={
                "type": "array",
                "items": _obj(
                    name={"type": "string", "description": "Plain product name, e.g. 'colander', 'chili flakes'."},
                    quantity=_nullable("number", "How many of the unit, if it matters."),
                    unit=_nullable("string", "e.g. 'lb', 'oz', 'each', 'bunch'."),
                ),
            },
        ),
        {},
    ),
    "set_plan": (
        "Save or replace the plan for one dish as steps with durations, dependencies and equipment. Other dishes keep"
        " cooking alongside: steps from every dish are scheduled together around the cook's burners, ovens, hands"
        " and skill. Step ids must be unique across dishes (prefix them, e.g. 'fish_sear'). To replan a dish"
        " mid-cook, resend all its steps, keeping the ids of steps already started or done.",
        _obj(
            dish={"type": "string"},
            ingredients={
                "type": "array",
                "items": _INGREDIENT,
                "description": "Everything the dish needs, with amounts.",
            },
            steps={"type": "array", "items": _STEP},
        ),
        {},
    ),
    "think_it_through": (
        "Consult a more careful chef before choosing a recipe, before set_plan, and when replanning after something"
        " goes wrong. Not for routine steps, timers or kitchen updates. Takes 10-30 seconds; the cook's screen shows"
        " that you're thinking, so you don't need to say anything first.",
        _obj(question={"type": "string", "description": "What to decide, with anything the cook said that matters."}),
        {"tool_call_output_timeout_ms": 60_000},
    ),
    "clear_plan": (
        "Drop a dish's plan when the cook abandons it, or every dish's with null to start over.",
        _obj(dish=_nullable("string", "The dish to drop, or null for everything.")),
        {},
    ),
    "next_steps": (
        "What to do right now, what is in progress, what's coming up, running timers and time to finish.",
        _obj(),
        {},
    ),
    "update_step": (
        "Mark a step started or done. Starting a hands-off step (simmer, bake, rest) sets a timer for it"
        " automatically.",
        _obj(step_id={"type": "string"}, status={"type": "string", "enum": ["started", "done"]}),
        {},
    ),
    "show_on_screen": (
        "Open a sheet on the cook's screen when they ask to see something: their Instacart carts, or their kitchen"
        " (fridge, freezer, pantry, spices, tools and setup). 'nothing' closes it.",
        _obj(view={"type": "string", "enum": ["shopping", "kitchen", "nothing"]}),
        {},
    ),
    "set_timer": (
        "Start a kitchen timer. It shows on the cook's screen, so don't announce it; at most say the duration."
        " When it goes off you will be told to alert the cook.",
        _obj(
            label={"type": "string", "description": "One or two words, shown on screen, e.g. 'pasta'."},
            minutes={"type": "number"},
            alert={"type": "string", "description": "What to tell the user when it goes off."},
        ),
        {},
    ),
    "adjust_timer": (
        "Pause, resume, cancel, or add time to a running timer (negative minutes take time off). The screen shows the"
        " change, so don't announce it.",
        _obj(
            label={"type": "string"},
            action={"type": "string", "enum": ["pause", "resume", "add", "cancel"]},
            minutes=_nullable("number", "For add: how many minutes to add. Null otherwise."),
        ),
        {},
    ),
}


def tool_definitions(shopping: bool, thinking: bool) -> list[dict]:
    """The tools to offer the agent; ones it has no backing for (shopping, the advisor) are left out."""
    return [
        {
            "type": "custom_websocket",
            "tool_schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description + ("" if options.get("require_speech_before_tool_call") else SILENT),
                    "parameters": parameters,
                    "strict": True,
                },
            },
            "execution_mode": "sync",
            "tool_call_output_timeout_ms": 10_000,
            **options,
        }
        for name, (description, parameters, options) in TOOL_SPECS.items()
        if (shopping or name != "fill_instacart_cart") and (thinking or name != "think_it_through")
    ]


class Toolbox:
    def __init__(
        self,
        kitchen: Kitchen,
        path: Path,
        timers: Timers,
        shopper: InstacartShopper | None,
        on_cart_done: Callable[[dict], Awaitable[None]],
        advisor: Advisor | None = None,
    ) -> None:
        self.kitchen = kitchen
        self.path = path
        self.timers = timers
        self.shopper = shopper
        self.on_cart_done = on_cart_done
        self.advisor = advisor
        self.cart_job: asyncio.Task | None = None
        self.signed_in: bool | None = None  # whether the shopping browser is signed in to Instacart, once checked
        # A cart job only lives as long as the conversation that started it.
        for order in kitchen.orders:
            if order["status"] == "filling":
                order["status"] = "interrupted"

    def save(self) -> None:
        self.kitchen.save(self.path)

    async def call(self, name: str, parameters: dict) -> dict:
        """Run a tool; errors go back to the model as data so it can correct itself mid-conversation."""
        if name not in TOOL_SPECS:
            return {"error": f"unknown tool {name!r}"}
        # Phonic adds its own fields (e.g. pre_tool_text) to every call; pass only the declared ones. The model
        # sometimes writes a nullable field as the string "null", which means the same as null.
        declared = TOOL_SPECS[name][1]["properties"]
        args = {k: None if v == "null" else v for k, v in parameters.items() if k in declared}
        try:
            result = getattr(self, name)(**args)
            return await result if inspect.isawaitable(result) else result
        except (KeyError, ValueError, TypeError) as e:
            return {"error": str(e) or type(e).__name__}

    def get_kitchen(self) -> dict:
        k = self.kitchen
        return {
            "profile": vars(k.profile),
            "inventory": k.inventory,
            "recipes": k.recipes,
            "carts": k.orders,
            "cooking": k.dishes,
        }

    def update_kitchen(
        self,
        # The model sometimes omits nullable fields instead of sending null, so they all default to None.
        items: list[dict] | None = None,
        burners: int | None = None,
        ovens: int | None = None,
        cooks: int | None = None,
        skill: str | None = None,
        dietary_notes: str | None = None,
    ) -> dict:
        k = self.kitchen
        for item in items or []:
            name = (k.stock(item["name"]) or (item["name"].lower(), None))[0]
            k.inventory[name] = {"have": item["have"], "where": item["where"]}
        if dietary_notes is not None:
            # The model sometimes sends an empty quoted string ('""') to mean "none".
            dietary_notes = dietary_notes.strip().strip("\"'")
        if skill is not None and skill not in HANDS_ON_SPEED:
            raise ValueError(f"skill must be one of {list(HANDS_ON_SPEED)}, or null if unknown")
        for attr, value in [
            ("burners", burners),
            ("ovens", ovens),
            ("cooks", cooks),
            ("skill", skill),
            ("dietary_notes", dietary_notes),
        ]:
            if value is not None:
                setattr(k.profile, attr, value)
        self.save()
        return self.get_kitchen()

    def shopping_list(self, items: list[dict]) -> dict:
        rows = []
        for item in items:
            stock = self.kitchen.stock(item["name"])
            rows.append(
                {"name": item["name"], "need": item["amount"], "have": stock[1]["have"] if stock else "unknown"}
            )
        return {"items": rows}

    async def think_it_through(self, question: str) -> dict:
        if self.advisor is None:
            raise ValueError("No advisor available; decide yourself.")
        context = f"Kitchen state: {json.dumps(self.get_kitchen())}\n\n{self.recap() or 'Nothing has happened yet.'}"
        return {"advice": await self.advisor.advise(question, context)}

    def fill_instacart_cart(self, store: str | None, items: list[dict]) -> dict:
        if self.shopper is None:
            raise ValueError("Instacart is turned off; tell the cook what to buy instead.")
        if any(o["status"] == "filling" for o in self.kitchen.orders):
            raise ValueError("Already filling the cart; wait for that to finish.")
        order = {"store": store, "items": [i["name"] for i in items], "status": "filling", "at": time.time()}
        self.kitchen.orders.append(order)
        self.save()
        self.cart_job = asyncio.create_task(self._fill_cart(order, items, store))
        # Without this the agent tends to announce the items as already in the cart.
        return {"started": True, "in_cart_yet": False, "say": "Only that you're on it. You'll be told when it's done."}

    async def _fill_cart(self, order: dict, items: list[dict], store: str | None) -> None:
        try:
            report = await self.shopper.fill_cart(items, store)
            status = "needs_login" if report["needs_login"] else "ready"
        except Exception as e:  # e.g. the sandbox is down; the cook still needs to hear it didn't work
            logging.exception("filling the Instacart cart failed")
            report = {
                "added": [],
                "missing": order["items"],
                "needs_login": False,
                "note": f"Shopping browser failed: {e}",
            }
            status = "failed"
        order.update(report, status=status)
        if status in ("ready", "needs_login"):
            self.signed_in = status == "ready"
        self.save()
        await self.on_cart_done(order)

    def snapshot(self) -> dict:
        """What the screen shows: the scheduled plan, timers, and recent Instacart lists."""
        k = self.kitchen
        slots = {sl.step.id: sl for sl in schedule(k, now=time.time())} if k.steps else {}
        return {
            "steps": [
                {
                    "id": s.id,
                    "dish": s.dish,
                    "title": s.title,
                    "text": s.text,
                    "status": s.status,
                    "hands_on": s.hands_on,
                    "equipment": s.equipment,
                    "minutes": round(duration(s, k.profile), 1),
                    "uses": s.uses,
                    "start": round(slots[s.id].start, 2) if s.id in slots else None,
                    "end": round(slots[s.id].end, 2) if s.id in slots else None,
                }
                for s in k.steps
            ],
            "timers": self.timers.remaining(),
            "finished_at": k.finished_at,
            # The last line from each speaker, so a reload can show the exchange it interrupted.
            "said": {line["who"]: line["text"] for line in k.history},
            "carts": [o for o in k.orders if time.time() - o["at"] < CART_SHOWN_FOR_SECONDS][::-1],
            "kitchen": {"profile": vars(k.profile), "inventory": k.inventory},
            "shopping": {"on": self.shopper is not None, "signed_in": self.signed_in},
            # Each dish's ingredients with what the cook has of each, so the screen can show both side by side.
            "recipes": {
                dish: [{**i, "have": (stock := k.stock(i["name"])) and stock[1]["have"]} for i in ingredients]
                for dish, ingredients in k.recipes.items()
            },
        }

    def set_plan(self, dish: str, steps: list[dict], ingredients: list[dict] | None = None) -> dict:
        self.kitchen.set_plan(dish, [Step(**s, dish=dish) for s in steps], ingredients)
        self.save()
        result = self.next_steps()
        if assumed := self.kitchen.profile.assumed():
            result["assumed_kitchen"] = assumed
        return result

    def next_steps(self) -> dict:
        k = self.kitchen
        if not k.steps:
            return {"error": "No plan yet; call set_plan first."}
        slots = schedule(k, now=time.time())
        return {
            "dishes": k.dishes,
            "do_now": [
                {"id": s.step.id, "text": s.step.text, "minutes": round(duration(s.step, k.profile), 1)}
                for s in slots
                if s.step.status == "pending" and s.start == 0
            ],
            "in_progress": [
                {"id": s.step.id, "text": s.step.text, "minutes_left": round(s.end, 1)}
                for s in slots
                if s.step.status == "in_progress"
            ],
            "coming_up": [
                {"id": s.step.id, "text": s.step.text, "starts_in_minutes": round(s.start, 1)}
                for s in slots
                if s.start > 0
            ][:3],
            "minutes_to_finish": round(max((s.end for s in slots), default=0), 1),
            "steps_done": f"{sum(s.status == 'done' for s in k.steps)}/{len(k.steps)}",
            "timers": self.timers.remaining(),
        }

    def recap(self) -> str | None:
        """Where things stand, for an agent picking up a conversation it has no memory of; None if nothing has happened."""
        k = self.kitchen
        if not k.history and not k.steps:
            return None
        lines = ["## Where things stand", "You are picking up a conversation already in progress with this cook."]
        if k.steps:
            marks = {"done": "done", "in_progress": "in progress", "pending": "to do"}
            for dish in k.dishes:
                lines += [f"Cooking {dish}:"] + [
                    f"- {s.title} ({s.id}): {marks[s.status]}" for s in k.steps if s.dish == dish
                ]
        if timers := self.timers.remaining():
            running = [
                f"{t['label']} ({t['seconds_left'] // 60}m{t['seconds_left'] % 60:02d}s left{', paused' if t['paused'] else ''})"
                for t in timers
            ]
            lines.append("Timers running: " + ", ".join(running))
        if carts := self.snapshot()["carts"]:
            lines.append("Instacart carts: " + "; ".join(f"{', '.join(o['items'])} ({o['status']})" for o in carts))
        if k.history:
            lines += ["Recent conversation, oldest first:"] + [f"{line['who']}: {line['text']}" for line in k.history]
        return "\n".join(lines)

    def update_step(self, step_id: str, status: str) -> dict:
        step = self.kitchen.step(step_id)
        timer_note = None
        if status == "started":
            step.status, step.started_at = "in_progress", time.time()
            if not step.hands_on:
                minutes = duration(step, self.kitchen.profile)
                # Labelled with the step's title, since the label is what the cook sees on the dial.
                self.timers.set(step.title, minutes, f"Check that '{step.text}' is done.", step_id=step_id)
                timer_note = f"Timer '{step.title}' set for {round(minutes, 1)} minutes."
        else:
            step.status = "done"
            self.timers.cancel(step.title)
            if all(s.status == "done" for s in self.kitchen.steps):
                self.kitchen.finished_at = time.time()
        self.save()
        result = self.next_steps()
        if timer_note:
            result["timer_set"] = timer_note
        return result

    def clear_plan(self, dish: str | None = None) -> dict:
        self.kitchen.clear_plan(dish)
        self.save()
        return {"ok": True, "cooking": self.kitchen.dishes}

    def show_on_screen(self, view: str) -> dict:
        # The front end opens the sheet when it sees this tool's result go by.
        return {"ok": True, "showing": view}

    def set_timer(self, label: str, minutes: float, alert: str) -> dict:
        self.timers.set(label, minutes, alert)
        return {"ok": True, "timers": self.timers.remaining()}

    def adjust_timer(self, label: str, action: str, minutes: float | None = None) -> dict:
        match action:
            case "pause":
                self.timers.pause(label)
            case "resume":
                self.timers.resume(label)
            case "add":
                if minutes is None:
                    raise ValueError("add needs minutes")
                self.timers.add(label, minutes)
            case "cancel":
                self.timers.cancel(label)
        return {"ok": True, "timers": self.timers.remaining()}
