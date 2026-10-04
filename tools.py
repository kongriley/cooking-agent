"""The agent's custom_websocket tools: their schemas (sent in the STS config) and the handlers that run them."""

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from advisor import Advisor
from kitchen import HANDS_ON_SPEED, Kitchen, Slot, Step, drop_busywork, duration, schedule
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
        "description": "At most 15 words, written like a chef's prep list, read at a glance from across the room:"
        " fragments, verb first, numbers as digits, the done-cue last. 'Pound cold butter to a 7-inch square."
        " Chill until firm, 30 min.' 'Garlic in. Pale gold, 2 min.' 'Rest 10 min. Don't cut early.' No 'you',"
        " no 'next', no tips or reasons; say extra detail aloud.",
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

# A plan that lands within a few minutes of the serve time is on time; nobody minds the gravy at 7:34.
LATE_AFTER_MINUTES = 5

# Instacart carts stay on screen for a day; older ones are history.
CART_SHOWN_FOR_SECONDS = 24 * 3600

# Appended to every tool that should run without spoken narration ("let me check...").
SILENT = " Call it without saying anything first; speak only once you have the result."
# Phonic makes the assistant speak before every tool call unless told otherwise; Basil's tools are quick and silent.
QUICK = {"require_speech_before_tool_call": False}
# A slow tool (a careful chef thinking, 10-30 s): a line first, then it runs in the background while the conversation
# carries on, and Basil brings the result back himself the moment it lands (Phonic's "busy mode").
SLOW = {
    "require_speech_before_tool_call": True,
    "execution_mode": "async",
    "wait_for_response": True,
    "allow_tool_chaining": False,
    "tool_call_output_timeout_ms": 90_000,
}
ASYNC_TOOLS = {"plan_dish", "think_it_through"}
# Changes the screen already shows: no reply after them at all, so Basil never says "cleared" a beat after it's gone
# from the screen, or "pulling it up" after it's up.
SHOWN = {**QUICK, "forbid_speech_after_tool_call": True}
SHOWN_TOOLS = {
    "show_on_screen", "update_kitchen", "update_ingredients", "clear_carts", "set_timer", "adjust_timer",
    "set_listening", "clear_plan",
}  # fmt: skip

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
            store=_nullable("string", "Their usual Instacart store, e.g. 'Safeway'."),
            remove=_nullable(
                "array",
                "Things to take off the list entirely, so they're unknown again (not 'none', which means they're out).",
                items={"type": "string"},
            ),
            cook_names=_nullable(
                "array",
                "Names of the people cooking, in order, starting with whoever is at the screen; 'You' is fine for them.",
                items={"type": "string"},
            ),
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
        " Instacart themselves. It shops at their usual store; if that isn't known yet, ask which one once.",
        _obj(
            store=_nullable("string", "Store on Instacart, e.g. 'Safeway', or null for their usual one."),
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
    "change_cart": (
        "Change a cart that already exists, at any store (Instacart or a store's own site): add to it, take something"
        " out, change how many, swap one thing for another, or with no changes just find out what's in it. Each"
        " store has one cart: to add more to a cart you started, use this, not a new run. Runs in the background;"
        " you're told what changed and what's in the cart when it's done.",
        _obj(
            store=_nullable("string", "The Instacart store, or the site (e.g. 'dartagnan.com'); null for their usual."),
            changes={
                "type": "array",
                "description": "What to change; empty to just look.",
                "items": _obj(
                    name={"type": "string", "description": "The item, as they'd say it: 'milk', 'lemons'."},
                    change={"type": "string", "enum": ["remove", "set_quantity", "add"]},
                    quantity=_nullable("number", "For set_quantity or add: how many."),
                    unit=_nullable("string", "e.g. 'lb', 'each', 'bunch'."),
                ),
            },
        ),
        {},
    ),
    "shop_online": (
        "Shop a store's own website instead of Instacart, for something premium or specialist (aged beef, good"
        " cheese, a particular olive oil). Pick a reputable store if they don't name one. A browser agent adds the"
        " items to that site's cart in the background and stops before checkout; the screen shows the cart, and"
        " they can open it to review and pay. Ask before buying.",
        _obj(
            site={"type": "string", "description": "The store's web address, e.g. 'murrayscheese.com'."},
            items={
                "type": "array",
                "items": _obj(
                    name={"type": "string", "description": "What to get, specifically: '2 lb dry-aged ribeye'."},
                    quantity=_nullable("number", "How many, if it matters."),
                    unit=_nullable("string", "e.g. 'lb', 'each'."),
                ),
            },
            note=_nullable("string", "What they care about: grade, budget, origin."),
        ),
        {},
    ),
    "check_shopping": (
        "How the shopping run is going right now: its status, how long it's been, and what the shopping browser is"
        " looking at. For 'how's the shopping going?'; never guess.",
        _obj(),
        {},
    ),
    "clear_carts": (
        "Take carts off the Shopping sheet: 'clear the shopping list'. Doesn't touch their Instacart cart.",
        _obj(),
        {},
    ),
    "set_plan": (
        "Save or replace the plan for one dish as steps with durations, dependencies and equipment. Other dishes keep"
        " cooking alongside: steps from every dish are scheduled together around the cook's burners, ovens, hands"
        " and skill. Step ids must be unique across dishes (prefix them, e.g. 'fish_sear'). To replan a dish"
        " mid-cook, resend all its steps, keeping the ids of steps already started or done. An oven holds two things at"
        " once; if they need different temperatures, make one step wait for the other. Waiting steps (preheat, boil,"
        " simmer, bake, rest, proof) are hands-off, so other work happens alongside. If the result lists"
        " assumed_kitchen, confirm those in one line ('Four burners, right?'). Then give the first thing to do.",
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
    "set_serve_time": (
        "Set when they want to eat, e.g. '19:30' (24-hour, local). The plan then works back from it: each step starts"
        " as late as it can with every dish still ready together, so ask for it before planning a meal of several"
        " dishes. Null clears it, and everything starts as soon as it can.",
        _obj(at=_nullable("string", "Local time as HH:MM, 24-hour. Null to clear.")),
        {},
    ),
    "update_ingredients": (
        "Change a dish's ingredient list: add something, change an amount, or take something off ('only half a cup"
        " of cream', 'skip the anchovies', 'add a pinch of chili'). Doesn't change the steps; replan if it should.",
        _obj(
            dish={"type": "string"},
            items=_nullable("array", "Ingredients to add, or whose amount changes.", items=_INGREDIENT),
            remove=_nullable("array", "Names of ingredients to take off.", items={"type": "string"}),
        ),
        {},
    ),
    "plan_dish": (
        "Build and save the whole plan for a dish once they've chosen it: a more careful chef writes every step and"
        " ingredient, and it goes straight onto the screen. Takes 10-30 seconds; say a few words first ('Chicken. One"
        " sec.'), keep talking if they do, and when it's saved give the first thing to do. Use set_plan only to adjust"
        " a plan by hand.",
        _obj(
            dish={"type": "string", "description": "The dish, as a short title: 'Roast chicken'."},
            notes={"type": "string", "description": "Anything that shapes it: servings, time, what they have, taste."},
        ),
        SLOW,
    ),
    "think_it_through": (
        "Consult a more careful chef before choosing a recipe and when recovering after something goes wrong. Not for"
        " routine steps, timers or kitchen updates, and not for planning (plan_dish does that). Takes 10-30 seconds;"
        " say a few words first. The advice is for you; never read it out.",
        _obj(question={"type": "string", "description": "What to decide, with anything the cook said that matters."}),
        SLOW,
    ),
    "clear_plan": (
        "Drop a dish's plan when the cook abandons it, or everything (and its timers) with null to start over. If"
        " they start a new dish while one's unfinished, ask once whether to drop the old one.",
        _obj(dish=_nullable("string", "The dish to drop, or null for everything.")),
        {},
    ),
    "next_steps": (
        "What to do right now and who does it, what is in progress, what's coming up and when, running timers, and"
        " when it'll all be ready against the serve time. If late_by_minutes is there, say so once, with what you'd"
        " cut or simplify.",
        _obj(),
        {},
    ),
    "update_step": (
        "Mark a step started or done. Starting a hands-off step (simmer, bake, rest) sets a timer for it"
        " automatically.",
        _obj(step_id={"type": "string"}, status={"type": "string", "enum": ["started", "done"]}),
        {},
    ),
    "stay_quiet": (
        "Say nothing this turn: what you heard wasn't for you (cooks talking to each other, the radio, a phone call,"
        " thinking out loud). Call it and stop; never say that you're staying quiet.",
        _obj(),
        {"forbid_speech_after_tool_call": True},
    ),
    "show_on_screen": (
        "Drive the cook's screen: everything they could tap, they can ask you for. 'step' shows a step (step_id, or"
        " null for the one they're on): 'what's next', 'go back', 'show me the soup'. 'plan' opens the run sheet,"
        " narrowed to one dish if given. 'kitchen', 'shopping' and 'ingredients' open those sheets. 'nothing' clears"
        " the screen back to what they're doing: closes any sheet or how-to card, puts away a finished cart, and"
        " dismisses timers that went off. For 'thanks, got it', 'close that', 'clear the screen'.",
        _obj(
            view={"type": "string", "enum": ["step", "plan", "kitchen", "shopping", "ingredients", "nothing"]},
            step_id=_nullable("string", "For 'step': which one, or null for the one they're on."),
            dish=_nullable("string", "For 'plan': narrow it to this dish, or null for all of them."),
        ),
        {},
    ),
    "set_listening": (
        "Whether you keep listening between conversations: always (you pick up again on your own) or only when they"
        " tap the mic. For 'always listen', 'stop listening when we're done', 'only when I tap'.",
        _obj(always={"type": "boolean"}),
        {},
    ),
    "show_how": (
        "Show how to do something on the cook's screen, as a card of numbered steps with a picture each and a picture of"
        " the key moment: for any 'how do I...' about a tool, technique or cut (a moka pot, folding egg"
        " whites, spatchcocking a chicken), and unasked when a beginner reaches a technique. Replaces any card already"
        " showing. Then say one line at most; don't read the card.",
        _obj(
            topic={"type": "string", "description": "What it's how to do, as a short title: 'Moka pot coffee'."},
            clip=_nullable(
                "string",
                "The one moment worth seeing, pictured at the top: 'coffee streaming up into a moka pot's top"
                " chamber'. Null if there's nothing to show.",
            ),
            steps={
                "type": "array",
                "description": "3 to 6 steps, in order.",
                "items": _obj(
                    title={"type": "string", "description": "2-5 words: 'Fill to the valve'."},
                    text={
                        "type": "string",
                        "description": "At most 10 words, prep-list style: what to do, then how you know."
                        " 'Water to the valve. No higher.'",
                    },
                ),
            },
            watch_out=_nullable("string", "The one mistake people make, in a short sentence. Null if none."),
        ),
        {},
    ),
    "set_timer": (
        "Start a kitchen timer, or queue one to start when another goes off (sear 4 min, then rest 5). It shows on the"
        " cook's screen, so don't announce it; at most say the duration. When it goes off you'll be told, and"
        " anything queued after it starts.",
        _obj(
            label={"type": "string", "description": "One or two words, shown on screen, e.g. 'pasta'."},
            minutes={"type": "number"},
            alert={"type": "string", "description": "What to tell the user when it goes off."},
            after=_nullable("string", "Label of a timer to start this one after, or null to start now."),
        ),
        {},
    ),
    "remind": (
        "Have yourself say something at a set time: 'remind me to start the rice at 6:40', 'in 20 minutes tell me to"
        " flip the brisket'. When it's due you'll be told, and you say it once there's a gap in the conversation."
        " Give either at or in_minutes. Cancel it with adjust_timer and its label.",
        _obj(
            label={"type": "string", "description": "One or two words, e.g. 'rice'."},
            message={"type": "string", "description": "What to say then, e.g. 'Start the rice.'"},
            at=_nullable("string", "Local time as HH:MM, 24-hour, or null."),
            in_minutes=_nullable("number", "Minutes from now, or null."),
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


def tool_definitions(shopping: bool, thinking: bool) -> list[dict | str]:
    """The tools to offer the agent; ones it has no backing for (shopping, the advisor) are left out."""
    # Phonic's own: skipping a turn that isn't meant for Basil (cooks talking to each other), and hanging up when the
    # cook's done ("that's all for now").
    # Phonic's own hang-up ("that's all for now"). Staying quiet is Basil's own stay_quiet: Phonic's built-in for it
    # sometimes came out as the words "tool_choose_not_to_respond" instead of silence.
    return ["natural_conversation_ending"] + [
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
            **QUICK,
            **(SHOWN if name in SHOWN_TOOLS else {}),
            **options,
        }
        for name, (description, parameters, options) in TOOL_SPECS.items()
        if (shopping or name != "fill_instacart_cart") and (thinking or name not in ("think_it_through", "plan_dish"))
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
        self.waiting_on_clock: set[str] | None = None  # steps ready but for their start time, as of the last check
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
            "serve_at": clock(k.serve_at) if k.serve_at else None,
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
        cook_names: list[str] | None = None,
        remove: list[str] | None = None,
        store: str | None = None,
    ) -> dict:
        k = self.kitchen
        for item in items or []:
            name = (k.stock(item["name"]) or (item["name"].lower(), None))[0]
            k.inventory[name] = {"have": item["have"], "where": item["where"]}
        for name in remove or []:
            if found := k.stock(name):
                del k.inventory[found[0]]
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
            ("cook_names", cook_names),
            ("store", store.strip().strip("\"'") or None if store else None),
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

    def update_ingredients(self, dish: str, items: list[dict] | None = None, remove: list[str] | None = None) -> dict:
        if dish not in self.kitchen.recipes and dish not in self.kitchen.dishes:
            raise KeyError(f"no dish {dish!r}; cooking: {self.kitchen.dishes}")
        gone = {name.strip().lower() for name in remove or []}
        recipe = [dict(i) for i in self.kitchen.recipes.get(dish, []) if i["name"].strip().lower() not in gone]
        for item in items or []:
            name, amount = item["name"].strip(), item["amount"].strip()
            if not name:
                raise ValueError("an ingredient needs a name")
            same = next((i for i in recipe if i["name"].lower() == name.lower()), None)
            if same:
                same["amount"] = amount
            else:
                recipe.append({"name": name, "amount": amount})
        self.kitchen.recipes[dish] = recipe
        self.save()
        return {"ok": True, "ingredients": recipe}

    async def plan_dish(self, dish: str, notes: str = "") -> dict:
        if self.advisor is None:
            raise ValueError("No planner available; write the plan yourself with set_plan.")
        context = f"Kitchen state: {json.dumps(self.get_kitchen())}\n\n{self.recap() or 'Nothing has happened yet.'}"
        plan = await self.advisor.plan(dish, notes, context)
        result = self.set_plan(dish, plan["steps"], plan["ingredients"])
        return {**result, "planned": dish, "say": "The plan is on screen. Give the first thing to do; don't read it."}

    async def think_it_through(self, question: str) -> dict:
        if self.advisor is None:
            raise ValueError("No advisor available; decide yourself.")
        context = f"Kitchen state: {json.dumps(self.get_kitchen())}\n\n{self.recap() or 'Nothing has happened yet.'}"
        return {"advice": await self.advisor.advise(question, context)}

    def set_serve_time(self, at: str | None) -> dict:
        self.kitchen.serve_at = None if at is None else at_time(at)
        self.save()
        return self.next_steps() if self.kitchen.steps else {"serve_at": at}

    def clear_carts(self, store: str | None = None, at: float | None = None) -> dict:
        """Take a store's cart (every run on it) off the list, or one run, or every finished one. Taking off one that's
        still being worked on stops that run."""
        k = self.kitchen
        if store is not None or at is not None:
            gone = [o for o in k.orders if o.get("store") == store or o["at"] == at]
            if any(o["status"] == "filling" for o in gone) and self.cart_job and not self.cart_job.done():
                self.cart_job.cancel()
            k.orders = [o for o in k.orders if o not in gone]
        else:
            k.orders = [o for o in k.orders if o["status"] == "filling"]
        self.save()
        return {"ok": True}

    def fill_instacart_cart(self, store: str | None, items: list[dict]) -> dict:
        store = self._cart_store(store)
        known = self._known(store)
        return self._start_cart_job(
            "add", store, [i["name"] for i in items], lambda: self.shopper.fill_cart(items, store, known)
        )

    def change_cart(self, store: str | None, changes: list[dict]) -> dict:
        from shopping import describe_change

        site = clean_site(store) if store and "." in store else None
        store = site or self._cart_store(store)
        if site:
            self._ready_to_shop()
        known = self._known(store)
        labels = [describe_change(c) for c in changes]
        return self._start_cart_job(
            "site" if site else "change", store, labels, lambda: self.shopper.change_cart(changes, store, known)
        )

    def _known(self, store: str) -> list[str]:
        """What was in this store's cart at the last look, so a run adjusts it instead of duplicating."""
        for order in reversed(self.kitchen.orders):
            if order.get("store") == store and order.get("in_cart"):
                return order["in_cart"]
        return []

    def _ready_to_shop(self) -> None:
        if self.shopper is None:
            raise ValueError("Shopping is turned off; tell the cook what to buy and where instead.")
        if any(o["status"] == "filling" for o in self.kitchen.orders):
            raise ValueError("Already working on a cart; wait for that to finish.")

    def shop_online(self, site: str, items: list[dict], note: str | None = None) -> dict:
        self._ready_to_shop()
        site = clean_site(site)
        known = self._known(site)
        return self._start_cart_job(
            "site", site, [i["name"] for i in items], lambda: self.shopper.shop_site(site, items, note, known)
        )

    async def check_shopping(self) -> dict:
        from shopping import describe_frame

        orders = self.kitchen.orders
        if not orders:
            return {"status": "nothing's been shopped"}
        order = orders[-1]
        status = {"status": order["status"], "store": order.get("store"), "items": order["items"],
                  "minutes": round((time.time() - order["at"]) / 60, 1)}  # fmt: skip
        if order["status"] == "filling":
            status["looking_at"] = await describe_frame()
        else:
            status.update({k: order.get(k) for k in ("added", "changed", "missing", "in_cart", "note")})
        return status

    def _cart_store(self, store: str | None) -> str:
        if self.shopper is None:
            raise ValueError("Instacart is turned off; tell the cook what to buy instead.")
        if any(o["status"] == "filling" for o in self.kitchen.orders):
            raise ValueError("Already working on the cart; wait for that to finish.")
        # Without a store the agent takes whatever Instacart offers first, which is rarely theirs.
        store = (store or "").strip().strip("\"'") or self.kitchen.profile.store  # the model sometimes quotes it
        if store is None:
            raise ValueError("Which store do they use on Instacart? Ask once, save it with update_kitchen, then retry.")
        if self.kitchen.profile.store is None:
            self.kitchen.profile.store = store
        return store

    def _start_cart_job(self, kind: str, store: str, items: list[str], run: Callable[[], Awaitable[dict]]) -> dict:
        order = {"kind": kind, "store": store, "items": items, "status": "filling", "at": time.time()}
        self.kitchen.orders.append(order)
        self.save()
        self.cart_job = asyncio.create_task(self._fill_cart(order, run))
        # Without this the agent tends to announce the items as already in the cart.
        return {"started": True, "in_cart_yet": False, "say": "Only that you're on it. You'll be told when it's done."}

    async def _fill_cart(self, order: dict, run: Callable[[], Awaitable[dict]]) -> None:
        try:
            report = await run()
            status = "needs_login" if report["needs_login"] else "ready"
        except Exception as e:  # e.g. the sandbox is down; the cook still needs to hear it didn't work
            logging.exception("filling the Instacart cart failed")
            report = {
                "added": [],
                "changed": [],
                "missing": order["items"],
                "in_cart": [],
                "needs_login": False,
                "note": f"Shopping browser failed: {e}",
            }
            status = "failed"
        order.update(report, status=status, done_at=time.time())
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
                    "after": s.after,
                    "cook": slots[s.id].cook if s.id in slots else s.cook,
                    "start": round(slots[s.id].start, 2) if s.id in slots else None,
                    "end": round(slots[s.id].end, 2) if s.id in slots else None,
                }
                for s in k.steps
            ],
            "timers": self.timers.remaining(),
            "finished_at": k.finished_at,
            "serve_at": k.serve_at,
            "how_to": k.how_to,
            "cooks": [k.profile.cook_name(i) for i in range(k.profile.for_planning().cooks)],
            # The last line from each speaker, so a reload can show the exchange it interrupted.
            "said": {line["who"]: line["text"] for line in k.history},
            "carts": carts_by_store([o for o in k.orders if time.time() - o["at"] < CART_SHOWN_FOR_SECONDS]),
            "kitchen": {"profile": vars(k.profile), "inventory": k.inventory},
            "shopping": {
                "on": self.shopper is not None,
                "signed_in": self.signed_in,
                # The sandbox's web viewer, or None when the shopping browser is a window on this computer.
                "viewer": self.shopper.viewer_url if self.shopper else None,
            },
            # Each dish's ingredients with what the cook has of each, so the screen can show both side by side.
            "recipes": {
                dish: [{**i, "have": (stock := k.stock(i["name"])) and stock[1]["have"]} for i in ingredients]
                for dish, ingredients in k.recipes.items()
            },
        }

    def set_plan(self, dish: str, steps: list[dict], ingredients: list[dict] | None = None) -> dict:
        kept = drop_busywork([Step(**s, dish=dish) for s in steps])
        if not kept:
            raise ValueError("A plan needs real cooking steps, not just gathering or checking; use plan_dish.")
        self.kitchen.set_plan(dish, kept, ingredients)
        self.save()
        result = self.next_steps()
        if assumed := self.kitchen.profile.assumed():
            result["assumed_kitchen"] = assumed
        return result

    def next_steps(self) -> dict:
        k = self.kitchen
        if not k.steps:
            return {"error": "No plan yet; call set_plan first."}
        now = time.time()
        slots = schedule(k, now=now)
        ready_in = max((s.end for s in slots), default=0)
        result = {
            "dishes": k.dishes,
            "do_now": [
                {**self._brief(s), "minutes": round(duration(s.step, k.profile), 1)}
                for s in slots
                if s.step.status == "pending" and s.start == 0
            ],
            "in_progress": [
                {**self._brief(s), "minutes_left": round(s.end, 1)} for s in slots if s.step.status == "in_progress"
            ],
            "coming_up": [
                {**self._brief(s), "at": clock(now + s.start * 60), "starts_in_minutes": round(s.start, 1)}
                for s in slots
                if s.start > 0
            ][:5],
            "minutes_to_finish": round(ready_in, 1),
            "ready_at": clock(now + ready_in * 60),
            "steps_done": f"{sum(s.status == 'done' for s in k.steps)}/{len(k.steps)}",
            "timers": self.timers.remaining(),
        }
        if k.serve_at is not None:
            result["serve_at"] = clock(k.serve_at)
            late = (now + ready_in * 60 - k.serve_at) / 60
            if late > LATE_AFTER_MINUTES:
                result["late_by_minutes"] = round(late)
        return result

    def _brief(self, slot: Slot) -> dict:
        # The cook sees the title and text on screen; the agent gets them to know what's next, not to read out.
        step = slot.step
        brief = {"id": step.id, "dish": step.dish, "title": step.title, "text": step.text}
        if who := self.kitchen.profile.cook_name(slot.cook):
            brief["who"] = who
        return brief

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
        if k.serve_at is not None:
            lines.append(f"Serving at {clock(k.serve_at)}.")
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
            slot = next((sl for sl in schedule(self.kitchen, now=time.time()) if sl.step is step), None)
            step.status, step.started_at = "in_progress", time.time()
            step.cook = slot.cook if slot else None
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
        if dish is None:  # starting over: the timers go too (reminders the cook asked for stay)
            for timer in [t for t in self.kitchen.timers if t.kind == "timer"]:
                self.timers.cancel(timer.label)
        self.save()
        return {"ok": True, "cooking": self.kitchen.dishes}

    def clear_all(self) -> None:
        """Start over: every dish's plan, every timer, and the conversation go. The kitchen itself stays."""
        self.kitchen.clear_plan(None)
        for timer in list(self.kitchen.timers):
            self.timers.cancel(timer.label)
        self.kitchen.history = []
        self.kitchen.finished_at = None
        self.kitchen.serve_at = None
        self.save()

    def show_on_screen(self, view: str, step_id: str | None = None, dish: str | None = None) -> dict:
        # The front end changes what it shows when it sees this tool's result go by; a how-to card lives here, since
        # it survives a reload, so stepping away from it closes it here too.
        if step_id is not None:
            self.kitchen.step(step_id)
        if dish is not None and dish not in self.kitchen.dishes:
            raise KeyError(f"no dish {dish!r}; cooking: {self.kitchen.dishes}")
        if view == "nothing" and self.kitchen.finished_at is not None:  # "clear the screen" puts away a finished meal
            self.clear_plan(None)
            self.kitchen.finished_at = None
            self.save()
        if view in ("step", "nothing") and self.kitchen.how_to is not None:
            self.kitchen.how_to = None
            self.save()
        return {"ok": True, "showing": view, "step_id": step_id, "dish": dish}

    def stay_quiet(self) -> dict:
        return {"ok": True}

    def set_listening(self, always: bool) -> dict:
        # The setting lives in the browser, which sees this result go by.
        return {"ok": True, "always": always}

    def show_how(self, topic: str, steps: list[dict], clip: str | None = None, watch_out: str | None = None) -> dict:
        if not steps:
            raise ValueError("give the steps")
        self.kitchen.how_to = {"topic": topic, "clip": clip, "steps": steps, "watch_out": watch_out}
        self.save()
        return {"ok": True, "showing": topic, "say": "One line at most; the screen shows the steps."}

    def set_timer(self, label: str, minutes: float, alert: str, after: str | None = None) -> dict:
        self.timers.set(label, minutes, alert, follows=after)
        return {"ok": True, "timers": self.timers.remaining()}

    def remind(self, label: str, message: str, at: str | None = None, in_minutes: float | None = None) -> dict:
        if (at is None) == (in_minutes is None):
            raise ValueError("give either at or in_minutes")
        when = at_time(at) if at is not None else time.time() + in_minutes * 60
        if when <= time.time():
            raise ValueError("that time has passed")
        self.timers.remind_at(label, when, message)
        return {"ok": True, "at": clock(when)}

    def clock_due(self) -> list[str]:
        """Steps whose start time has just come: they were waiting only on the clock and are due now. Steps freed by
        finishing the one before are left to the conversation, which already says what's next."""
        k = self.kitchen
        slots = schedule(k, now=time.time()) if k.steps else []
        done = {s.id for s in k.steps if s.status == "done"}
        free = [sl for sl in slots if sl.step.status == "pending" and all(d in done for d in sl.step.after)]
        waiting = {sl.step.id for sl in free if sl.start > 0}
        due = [sl for sl in free if sl.start == 0 and sl.step.id in (self.waiting_on_clock or set())]
        self.waiting_on_clock = waiting
        return [
            f"{who + ': ' if (who := k.profile.cook_name(sl.cook)) else ''}{sl.step.title} ({sl.step.dish})"
            for sl in due
        ]

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


def clock(at: float) -> str:
    """A local clock time the way people say it: '7:30 PM'."""
    return datetime.fromtimestamp(at).strftime("%-I:%M %p")


def at_time(hhmm: str) -> float:
    """The next time the clock reads HH:MM (local), as epoch seconds. Up to half an hour ago still counts as today:
    7:30 said at 7:40 is running late, not tomorrow."""
    clock_time = datetime.strptime(hhmm.strip(), "%H:%M")
    now = datetime.now()
    when = now.replace(hour=clock_time.hour, minute=clock_time.minute, second=0, microsecond=0)
    if when < now - timedelta(minutes=30):
        when += timedelta(days=1)
    return when.timestamp()


def clean_site(site: str) -> str:
    return site.strip().strip("\"'").removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")


def carts_by_store(orders: list[dict]) -> list[dict]:
    """One cart per store, newest first: the latest run on it, with the latest known contents and picture. Each run
    is something done to the one cart, not a cart of its own."""
    carts: dict[str, dict] = {}
    for order in orders:  # oldest first, so later runs win
        store = order.get("store") or "?"
        cart = carts.setdefault(store, {"runs": 0})
        known = {k: order[k] for k in ("in_cart", "shot", "cart_url") if order.get(k)}
        cart.update({**{k: v for k, v in order.items() if k not in ("in_cart", "shot", "cart_url")}, **known})
        cart["runs"] += 1
    return sorted(carts.values(), key=lambda c: -c["at"])
