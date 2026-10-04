import asyncio
import json
import time
from pathlib import Path

import pytest

from kitchen import Kitchen, Profile, Step, schedule
from timers import Timers
from tools import Toolbox, tool_definitions

PASTA = [
    {
        "id": "boil",
        "title": "boil",
        "text": "Boil a big pot of salted water",
        "minutes": 10,
        "after": [],
        "equipment": "burner",
        "hands_on": False,
    },
    {
        "id": "chop",
        "title": "chop",
        "text": "Chop onion and garlic",
        "minutes": 5,
        "after": [],
        "equipment": "none",
        "hands_on": True,
    },
    {
        "id": "sauce",
        "title": "sauce",
        "text": "Cook the sauce",
        "minutes": 15,
        "after": ["chop"],
        "equipment": "burner",
        "hands_on": False,
    },
    {
        "id": "pasta",
        "title": "pasta",
        "text": "Cook the spaghetti",
        "minutes": 9,
        "after": ["boil"],
        "equipment": "burner",
        "hands_on": False,
    },
    {
        "id": "toss",
        "title": "toss",
        "text": "Toss pasta with sauce",
        "minutes": 2,
        "after": ["sauce", "pasta"],
        "equipment": "none",
        "hands_on": True,
    },
]


def make_toolbox(
    tmp_path: Path,
    profile: Profile | None = None,
    alerts: list[str] | None = None,
    shopper=None,
    carts_done: list[dict] | None = None,
) -> Toolbox:
    kitchen = Kitchen(profile=profile or Profile())
    path = tmp_path / "kitchen.json"
    sink = alerts if alerts is not None else []
    done = carts_done if carts_done is not None else []

    async def alert(label: str, text: str) -> None:
        sink.append(text)

    async def nudge(text: str) -> None:
        sink.append(text)

    async def cart_done(order: dict) -> None:
        done.append(dict(order))

    timers = Timers(kitchen, save=lambda: kitchen.save(path), alert=alert, nudge=nudge)
    return Toolbox(kitchen, path, timers, shopper, on_cart_done=cart_done)


def call(toolbox: Toolbox, name: str, parameters: dict) -> dict:
    return asyncio.run(toolbox.call(name, parameters))


def finish(toolbox: Toolbox) -> float:
    return toolbox.next_steps()["minutes_to_finish"]


def test_parallel_burners_shorten_the_plan(tmp_path):
    two = make_toolbox(tmp_path, Profile(burners=2))
    two.set_plan("pasta", PASTA)
    # Water boils while the onion is chopped; sauce and pasta then share the two burners.
    assert {s["id"] for s in two.next_steps()["do_now"]} == {"boil", "chop"}
    assert finish(two) == 22

    one = make_toolbox(tmp_path, Profile(burners=1))
    one.set_plan("pasta", PASTA)
    assert finish(one) > finish(two)


def test_one_cook_never_does_two_hands_on_steps_at_once(tmp_path):
    steps = [
        {
            "id": f"chop{i}",
            "title": "Chop",
            "text": "chop",
            "minutes": 5,
            "after": [],
            "equipment": "none",
            "hands_on": True,
        }
        for i in range(3)
    ]
    solo = make_toolbox(tmp_path, Profile(cooks=1))
    solo.set_plan("salad", steps)
    assert finish(solo) == 15
    pair = make_toolbox(tmp_path, Profile(cooks=2))
    pair.set_plan("salad", steps)
    assert finish(pair) == 10


def test_beginners_get_more_time_and_less_juggling(tmp_path):
    passive = [
        {
            "id": f"bake{i}",
            "title": "Bake",
            "text": "bake",
            "minutes": 10,
            "after": [],
            "equipment": "none",
            "hands_on": False,
        }
        for i in range(3)
    ]
    beginner = make_toolbox(tmp_path, Profile(skill="beginner"))
    beginner.set_plan("x", passive)
    assert finish(beginner) == 20  # at most two things going at once
    advanced = make_toolbox(tmp_path, Profile(skill="advanced"))
    advanced.set_plan("x", passive)
    assert finish(advanced) == 10

    chop = [
        {
            "id": "chop",
            "title": "chop",
            "text": "chop",
            "minutes": 10,
            "after": [],
            "equipment": "none",
            "hands_on": True,
        }
    ]
    beginner.set_plan("x", chop)  # replaces dish x
    assert finish(beginner) == 15


def test_replanning_keeps_progress_and_rejects_bad_plans(tmp_path):
    toolbox = make_toolbox(tmp_path)
    toolbox.set_plan("pasta", PASTA)
    toolbox.update_step("chop", "done")
    toolbox.set_plan("pasta", PASTA)
    assert toolbox.kitchen.step("chop").status == "done"
    assert "chop" not in {s["id"] for s in toolbox.next_steps()["do_now"]}

    cycle = [
        {"id": "a", "title": "a", "text": "a", "minutes": 1, "after": ["b"], "equipment": "none", "hands_on": True},
        {"id": "b", "title": "b", "text": "b", "minutes": 1, "after": ["a"], "equipment": "none", "hands_on": True},
    ]
    assert "cycle" in call(toolbox, "set_plan", {"dish": "loop", "steps": cycle})["error"]
    dangling = [
        {"id": "a", "title": "a", "text": "a", "minutes": 1, "after": ["zzz"], "equipment": "none", "hands_on": True}
    ]
    assert "unknown" in call(toolbox, "set_plan", {"dish": "x", "steps": dangling})["error"]
    no_oven = make_toolbox(tmp_path, Profile(ovens=0))
    bake = [
        {
            "id": "bake",
            "title": "bake",
            "text": "bake",
            "minutes": 30,
            "after": [],
            "equipment": "oven",
            "hands_on": False,
        }
    ]
    assert "oven" in call(no_oven, "set_plan", {"dish": "bread", "steps": bake})["error"]


def test_a_serve_time_plans_backwards_so_every_dish_lands_together(tmp_path):
    def step(id, minutes, after=(), equipment="none", hands_on=True):
        return {
            "id": id,
            "title": id,
            "text": id,
            "minutes": minutes,
            "after": list(after),
            "equipment": equipment,
            "hands_on": hands_on,
        }

    toolbox = make_toolbox(tmp_path, Profile(burners=4, ovens=1, cooks=1, skill="intermediate"))
    toolbox.set_plan(
        "roast",
        [
            step("oven", 15, equipment="oven", hands_on=False),
            step("roast", 60, ["oven"], "oven", False),
            step("rest", 10, ["roast"], hands_on=False),
        ],
    )
    toolbox.set_plan("salad", [step("wash", 5), step("dress", 3, ["wash"])])
    now = time.time()
    toolbox.kitchen.serve_at = now + 120 * 60
    slots = {s.step.id: s for s in schedule(toolbox.kitchen, now=now)}
    # The roast's chain is 85 minutes, so it starts 35 minutes from now; the salad is made last, not first.
    assert slots["oven"].start == pytest.approx(35, abs=0.1)
    assert slots["rest"].end == pytest.approx(120, abs=0.1)
    assert slots["dress"].end == pytest.approx(120, abs=0.1) and slots["wash"].start > 100
    assert toolbox.next_steps()["do_now"] == [] and "late_by_minutes" not in toolbox.next_steps()

    # Not enough time: the long chain starts now and the plan says how late it'll be; the salad still comes last.
    toolbox.kitchen.serve_at = now + 60 * 60
    result = toolbox.next_steps()
    assert [s["id"] for s in result["do_now"]] == ["oven"] and result["late_by_minutes"] == 25


def test_a_dinner_party_of_eight_dishes_lands_together_in_two_hours(tmp_path):
    def step(id, minutes, after=(), equipment="none", hands_on=True):
        return {
            "id": id,
            "title": id,
            "text": id,
            "minutes": minutes,
            "after": list(after),
            "equipment": equipment,
            "hands_on": hands_on,
        }

    menu = {
        "chicken": [
            step("brine", 5),
            step("preheat", 15, equipment="oven", hands_on=False),
            step("roast", 60, ["brine", "preheat"], "oven", False),
            step("rest", 15, ["roast"], hands_on=False),
            step("carve", 8, ["rest"]),
        ],
        "gravy": [
            step("roux", 5, equipment="burner"),
            step("stock", 15, ["roux"], "burner", False),
            step("finish", 4, ["stock", "rest"], "burner"),
        ],
        "potatoes": [step("peel", 12), step("boil", 25, ["peel"], "burner", False), step("mash", 6, ["boil"])],
        "beans": [
            step("trim", 10),
            step("blanch", 4, ["trim"], "burner", False),
            step("saute", 5, ["blanch"], "burner"),
        ],
        "salad": [
            step("dressing", 8),
            step("croutons", 10, equipment="oven", hands_on=False),
            step("toss", 4, ["dressing", "croutons"]),
        ],
        "rolls": [step("warm", 10, ["roast"], "oven", False)],
        "cranberry": [
            step("cook", 15, equipment="burner", hands_on=False),
            step("chill", 30, ["cook"], hands_on=False),
        ],
        "crisp": [step("apples", 12), step("topping", 6), step("bake", 40, ["apples", "topping"], "oven", False)],
    }
    profile = Profile(burners=4, ovens=1, cooks=2, skill="intermediate", cook_names=["You", "Sam"])
    toolbox = make_toolbox(tmp_path, profile)
    for dish, steps in menu.items():
        toolbox.set_plan(dish, steps)
    now = time.time()
    toolbox.kitchen.serve_at = now + 120 * 60
    slots = {s.step.id: s for s in schedule(toolbox.kitchen, now=now)}
    assert 115 <= max(s.end for s in slots.values()) <= 125  # everything lands at dinner, not before or well after
    assert slots["rest"].start == pytest.approx(slots["roast"].end)  # out of the oven and straight to resting
    assert slots["mash"].start == pytest.approx(slots["boil"].end)  # mashed while hot
    assert "late_by_minutes" not in toolbox.next_steps()


def test_two_cooks_each_get_their_own_steps(tmp_path):
    chop = [
        {
            "id": f"chop{i}",
            "title": "Chop",
            "text": "chop",
            "minutes": 5,
            "after": [],
            "equipment": "none",
            "hands_on": True,
        }
        for i in range(4)
    ]
    toolbox = make_toolbox(tmp_path, Profile(cooks=2, cook_names=["You", "Sam"]))
    toolbox.set_plan("salad", chop)
    slots = schedule(toolbox.kitchen, now=time.time())
    for cook in (0, 1):  # neither cook ever has two things in hand at once
        mine = sorted((s.start, s.end) for s in slots if s.cook == cook)
        assert len(mine) == 2 and mine[0][1] <= mine[1][0]
    assert {s["who"] for s in toolbox.next_steps()["do_now"]} == {"You", "Sam"}
    # A started step stays with whoever started it.
    toolbox.update_step("chop1", "started")
    who = toolbox.kitchen.step("chop1").cook
    assert next(s for s in schedule(toolbox.kitchen, now=time.time()) if s.step.id == "chop1").cook == who


def test_in_progress_steps_count_down(tmp_path):
    toolbox = make_toolbox(tmp_path)
    toolbox.set_plan("pasta", PASTA)
    toolbox.kitchen.step("boil").status = "in_progress"
    toolbox.kitchen.step("boil").started_at = time.time() - 4 * 60
    slots = {s.step.id: s for s in schedule(toolbox.kitchen, now=time.time())}
    assert slots["boil"].end == pytest.approx(6, abs=0.01)
    assert slots["pasta"].start == pytest.approx(6, abs=0.01)


class FakeShopper:
    viewer_url = None

    async def change_cart(self, changes: list[dict], store: str | None, known=()) -> dict:
        self.calls.append((changes, store))
        return self.report

    def __init__(self, report: dict | None) -> None:
        self.report = report
        self.calls: list[tuple] = []

    async def fill_cart(self, items: list[dict], store: str | None, known=()) -> dict:
        self.calls.append((items, store))
        await asyncio.sleep(0.05)
        if self.report is None:
            raise ConnectionError("browser is gone")
        return self.report


ITEMS = [{"name": "butter", "quantity": 1, "unit": "lb"}, {"name": "flaky salt", "quantity": None, "unit": None}]


def test_the_cart_fills_in_the_background_and_reports_back(tmp_path):
    async def scenario() -> tuple:
        done: list[dict] = []
        report = {"added": ["butter: Land O Lakes 1 lb"], "missing": ["flaky salt"], "needs_login": False, "note": ""}
        shopper = FakeShopper(report)
        toolbox = make_toolbox(tmp_path, shopper=shopper, carts_done=done)
        started = await toolbox.call("fill_instacart_cart", {"store": "Safeway", "items": ITEMS})
        filling = toolbox.snapshot()["carts"][0]["status"]
        again = await toolbox.call("fill_instacart_cart", {"store": None, "items": ITEMS})
        await toolbox.cart_job
        return started, filling, again, done, toolbox.snapshot()["carts"][0], shopper.calls

    started, filling, again, done, cart, calls = asyncio.run(scenario())
    assert started["started"] and not started["in_cart_yet"] and filling == "filling"
    assert "Already working" in again["error"] and len(calls) == 1 and calls[0][1] == "Safeway"
    assert cart["status"] == "ready" and cart["missing"] == ["flaky salt"]
    assert done[0]["added"] == ["butter: Land O Lakes 1 lb"]


def test_a_broken_shopping_browser_is_reported_not_swallowed(tmp_path):
    async def scenario() -> list[dict]:
        done: list[dict] = []
        toolbox = make_toolbox(tmp_path, shopper=FakeShopper(None), carts_done=done)
        await toolbox.call("fill_instacart_cart", {"store": "Safeway", "items": ITEMS})
        await toolbox.cart_job
        return done

    done = asyncio.run(scenario())
    assert done[0]["status"] == "failed" and done[0]["missing"] == ["butter", "flaky salt"]
    assert "browser is gone" in done[0]["note"]


def test_a_cart_cut_off_by_a_restart_shows_as_interrupted(tmp_path):
    toolbox = make_toolbox(tmp_path)
    toolbox.kitchen.orders.append({"store": None, "items": ["butter"], "status": "filling", "at": time.time()})
    toolbox.save()
    reloaded = Toolbox(Kitchen.load(toolbox.path), toolbox.path, toolbox.timers, None, on_cart_done=None)
    assert reloaded.snapshot()["carts"][0]["status"] == "interrupted"


def test_the_kitchen_tracks_how_much_and_unknown_is_not_none(tmp_path):
    toolbox = make_toolbox(tmp_path)
    call(
        toolbox,
        "update_kitchen",
        {
            "items": [
                {"name": "Butter", "have": "1 stick", "where": "fridge"},
                {"name": "flour", "have": "none", "where": "pantry"},
            ],
            "burners": 2,
        },
    )
    rows = toolbox.shopping_list(
        [
            {"name": "unsalted butter", "amount": "280 g"},
            {"name": "flour", "amount": "500 g"},
            {"name": "yeast", "amount": "7 g"},
        ]
    )["items"]
    assert rows == [
        {"name": "unsalted butter", "need": "280 g", "have": "1 stick"},  # loose name match finds 'butter'
        {"name": "flour", "need": "500 g", "have": "none"},
        {"name": "yeast", "need": "7 g", "have": "unknown"},
    ]
    # Updating an item by a slightly different name changes the existing entry instead of adding a second.
    toolbox.update_kitchen(items=[{"name": "unsalted butter", "have": "none", "where": "fridge"}])
    assert toolbox.kitchen.inventory == {
        "butter": {"have": "none", "where": "fridge"},
        "flour": {"have": "none", "where": "pantry"},
    }
    assert toolbox.kitchen.profile.burners == 2


def test_the_screen_shows_each_ingredient_beside_what_you_have(tmp_path):
    toolbox = make_toolbox(tmp_path)
    toolbox.update_kitchen(items=[{"name": "garlic", "have": "1 head", "where": "pantry"}])
    toolbox.set_plan(
        "pasta", PASTA, ingredients=[{"name": "garlic", "amount": "6 cloves"}, {"name": "spaghetti", "amount": "200 g"}]
    )
    assert toolbox.snapshot()["recipes"]["pasta"] == [
        {"name": "garlic", "amount": "6 cloves", "have": "1 head"},
        {"name": "spaghetti", "amount": "200 g", "have": None},
    ]
    toolbox.set_plan("pasta", PASTA)  # replanning without ingredients keeps the list
    assert len(toolbox.snapshot()["recipes"]["pasta"]) == 2
    toolbox.clear_plan("pasta")
    assert toolbox.snapshot()["recipes"] == {}


def test_plans_report_what_they_assumed_about_the_kitchen(tmp_path):
    toolbox = make_toolbox(tmp_path, Profile(burners=2))
    result = toolbox.set_plan("pasta", PASTA)
    assert result["assumed_kitchen"] == {"ovens": 1, "cooks": 1, "skill": "intermediate"}
    toolbox.update_kitchen(ovens=1, cooks=1, skill="advanced")
    assert "assumed_kitchen" not in toolbox.set_plan("pasta", PASTA)


def test_tool_calls_ignore_phonic_fields_and_report_errors(tmp_path):
    toolbox = make_toolbox(tmp_path)
    assert "error" not in call(toolbox, "get_kitchen", {"pre_tool_text": "Let me check."})
    assert "error" in call(toolbox, "update_step", {"step_id": "nope", "status": "done"})
    assert "error" in call(toolbox, "not_a_tool", {})


def test_tool_schemas_are_strict():
    for tool in tool_definitions(shopping=True, thinking=True):
        if isinstance(tool, str):  # a Phonic built-in, by name
            continue
        params = tool["tool_schema"]["function"]["parameters"]
        assert params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"])


def test_timers_alert_and_starting_a_passive_step_sets_one(tmp_path):
    async def scenario() -> list[str]:
        alerts: list[str] = []
        toolbox = make_toolbox(tmp_path, alerts=alerts)
        toolbox.set_plan("pasta", PASTA)
        result = toolbox.update_step("boil", "started")
        assert "timer_set" in result and result["timers"][0]["label"] == "boil"  # PASTA titles equal their ids
        toolbox.update_step("boil", "done")
        assert toolbox.kitchen.timers == []

        toolbox.set_timer("pasta", minutes=0.001, alert="Drain the pasta.")
        await asyncio.sleep(0.2)
        assert toolbox.kitchen.timers == []
        return alerts

    alerts = asyncio.run(scenario())
    assert len(alerts) == 1 and "Drain the pasta." in alerts[0]


def test_timers_survive_a_reconnect(tmp_path):
    path = tmp_path / "kitchen.json"

    async def scenario() -> list[str]:
        alerts: list[str] = []

        async def alert(*args: str) -> None:
            alerts.append(args[-1])

        kitchen = Kitchen()
        Timers(kitchen, lambda: kitchen.save(path), alert, nudge=alert).set("bread", 0.002, "Take the bread out.")
        reloaded = Kitchen.load(path)
        assert [t.label for t in reloaded.timers] == ["bread"]
        Timers(reloaded, lambda: reloaded.save(path), alert, nudge=alert).restore()
        await asyncio.sleep(0.3)
        return alerts

    assert any("Take the bread out." in a for a in asyncio.run(scenario()))


def test_long_timers_get_a_heads_up_before_they_ring():
    async def scenario() -> list[tuple[str, float]]:
        events: list[tuple[str, float]] = []
        start = time.monotonic()

        async def alert(label: str, text: str) -> None:
            events.append(("alert", time.monotonic() - start))

        async def nudge(text: str) -> None:
            events.append(("heads-up", time.monotonic() - start))

        kitchen = Kitchen()
        timers = Timers(kitchen, lambda: None, alert, nudge, heads_up_min_seconds=0.3, heads_up_lead_seconds=0.2)
        timers.set("roast", minutes=0.5 / 60, alert="Pull the roast.")
        timers.set("toast", minutes=0.1 / 60, alert="Toast is done.")  # too short for a heads-up
        await asyncio.sleep(0.7)
        return events

    events = asyncio.run(scenario())
    assert [e for e, _ in events] == ["alert", "heads-up", "alert"]
    assert events[1][1] == pytest.approx(0.3, abs=0.08)


def test_null_strings_from_the_model_mean_unknown(tmp_path):
    toolbox = make_toolbox(tmp_path, Profile(skill="beginner", dietary_notes="no nuts"))
    call(toolbox, "update_kitchen", {"skill": "null", "dietary_notes": "null", "burners": 2})
    assert (toolbox.kitchen.profile.skill, toolbox.kitchen.profile.dietary_notes) == ("beginner", "no nuts")
    assert "error" in call(toolbox, "update_kitchen", {"skill": "expert"})
    assert toolbox.kitchen.profile.skill == "beginner"


def test_speech_chunks_join_into_readable_text():
    from session import join_speech

    said = ""
    for chunk in ["Checking where we are", "Garlic's ", "done. ", "Fish ", "next.", "Pat it dry."]:
        said = join_speech(said, chunk)
    assert said == "Checking where we are Garlic's done. Fish next. Pat it dry."


def test_a_reconnect_briefs_the_agent_on_where_things_stand(tmp_path):
    async def scenario() -> None:
        toolbox = make_toolbox(tmp_path)
        assert toolbox.recap() is None  # a fresh kitchen gets the normal greeting
        toolbox.set_plan("pasta", PASTA)
        toolbox.update_step("chop", "done")
        toolbox.set_timer("pasta", 9, "Drain it.")
        for i in range(40):
            toolbox.kitchen.remember("cook", f"line {i}")
        toolbox.kitchen.remember("basil", "Water's on. Slice the garlic.")
        toolbox.save()

        history = Kitchen.load(toolbox.path).history  # survives a restart
        assert len(history) == 30 and history[-1]["text"] == "Water's on. Slice the garlic."
        text = toolbox.recap()
        assert "chop): done" in text and "boil): to do" in text and "pasta (" in text
        assert "basil: Water's on. Slice the garlic." in text and "line 10" not in text
        assert toolbox.snapshot()["said"] == {"cook": "line 39", "basil": "Water's on. Slice the garlic."}
        toolbox.timers.cancel("pasta")

    asyncio.run(scenario())


def test_timers_pause_resume_and_take_extra_time(tmp_path):
    async def scenario() -> tuple[list[str], list[dict]]:
        alerts: list[str] = []
        toolbox = make_toolbox(tmp_path, alerts=alerts)
        toolbox.set_timer("rice", minutes=0.3 / 60, alert="Fluff the rice.")
        toolbox.adjust_timer("rice", "pause")
        await asyncio.sleep(0.5)  # well past when it would have rung
        paused = toolbox.timers.remaining()
        assert alerts == [] and paused[0]["paused"] and paused[0]["seconds_left"] == 0  # 0.3 s rounds to 0

        toolbox.adjust_timer("rice", "add", minutes=0.2 / 60)
        toolbox.adjust_timer("rice", "resume")
        await asyncio.sleep(0.25)
        assert alerts == []  # 0.3 s left plus 0.2 s added, so not yet
        await asyncio.sleep(0.4)
        assert "error" in await toolbox.call("adjust_timer", {"label": "rice", "action": "pause", "minutes": None})
        return alerts, toolbox.timers.remaining()

    alerts, remaining = asyncio.run(scenario())
    assert len(alerts) == 1 and "Fluff the rice." in alerts[0] and remaining == []


def test_finishing_the_last_step_marks_the_plan_done_until_the_next_one(tmp_path):
    toolbox = make_toolbox(tmp_path)
    toolbox.set_plan("salad", [PASTA[1]])
    assert toolbox.snapshot()["finished_at"] is None
    toolbox.update_step("chop", "done")
    assert toolbox.snapshot()["finished_at"] is not None
    toolbox.set_plan("pasta", PASTA)
    assert toolbox.snapshot()["finished_at"] is None


SAUCE = [
    {
        "id": "sauce_onion",
        "title": "Sweat onion",
        "text": "Sweat the onion.",
        "minutes": 8,
        "after": [],
        "equipment": "burner",
        "hands_on": True,
    },
    {
        "id": "sauce_simmer",
        "title": "Simmer sauce",
        "text": "Simmer.",
        "minutes": 20,
        "after": ["sauce_onion"],
        "equipment": "burner",
        "hands_on": False,
    },
]


def test_dishes_cook_side_by_side_and_share_the_burners(tmp_path):
    toolbox = make_toolbox(tmp_path, Profile(burners=1))
    toolbox.set_plan("pasta", PASTA)
    alone = finish(toolbox)
    result = toolbox.set_plan("sauce", SAUCE)
    assert result["dishes"] == ["pasta", "sauce"]
    assert finish(toolbox) > alone  # one burner now has the sauce to fit in too

    # Replanning one dish leaves the other alone; clashing ids are refused.
    toolbox.set_plan("sauce", SAUCE[:1])
    assert {s.id for s in toolbox.kitchen.steps if s.dish == "pasta"} == {s["id"] for s in PASTA}
    assert "unique" in call(toolbox, "set_plan", {"dish": "salad", "steps": [PASTA[0]]})["error"]
    assert [s.id for s in toolbox.kitchen.steps if s.dish == "sauce"] == ["sauce_onion"]  # refused plan changed nothing

    toolbox.clear_plan("sauce")
    assert toolbox.kitchen.dishes == ["pasta"]
    toolbox.clear_plan(None)
    assert toolbox.kitchen.steps == []


def test_starting_a_new_dish_clears_finished_ones(tmp_path):
    toolbox = make_toolbox(tmp_path)
    toolbox.set_plan("salad", [PASTA[1]])
    toolbox.update_step("chop", "done")
    toolbox.set_plan("sauce", SAUCE)
    assert toolbox.kitchen.dishes == ["sauce"]


def test_kitchens_saved_by_older_versions_still_load(tmp_path):
    old = {
        "profile": {"burners": 4, "ovens": 1, "cooks": 1, "skill": "intermediate", "dietary_notes": ""},
        "pantry": {"eggs": "6"},
        "equipment": [],
        "dish": "Branzino",
        "steps": [
            {
                "id": "fish_dry",
                "text": "Dry it.",
                "minutes": 3,
                "after": [],
                "equipment": "none",
                "hands_on": True,
                "status": "done",
                "started_at": None,
            }
        ],
        "timers": [{"label": "fish", "fire_at": time.time() + 60, "alert": "Flip.", "step_id": None}],
        "orders": [],
    }
    path = tmp_path / "kitchen.json"
    path.write_text(json.dumps(old))
    kitchen = Kitchen.load(path)
    assert kitchen.dishes == ["Branzino"] and kitchen.steps[0].title == "Fish dry"
    assert kitchen.history == [] and kitchen.timers[0].paused_left is None
    assert kitchen.inventory == {"eggs": {"have": "6", "where": "pantry"}}


def test_with_shopping_off_the_agent_never_sees_the_instacart_tool(tmp_path):
    names = {
        t["tool_schema"]["function"]["name"]
        for t in tool_definitions(shopping=False, thinking=False)
        if isinstance(t, dict)
    }
    assert "fill_instacart_cart" not in names and "set_plan" in names
    toolbox = make_toolbox(tmp_path)
    assert "turned off" in call(toolbox, "fill_instacart_cart", {"store": None, "items": []})["error"]


def test_ingredient_pictures_match_through_descriptors(tmp_path):
    from images import Images

    images = Images(tmp_path / "images.json")
    images.catalog = {"butter": "Butter", "flour": "Flour", "egg": "Egg", "chilli flakes": "Chilli Flakes"}
    names = ["European-style unsalted butter", "bread flour", "eggs", "chili flakes", "unobtainium"]
    matches = [asyncio.run(images._catalog_match(None, n)) for n in names]
    assert matches == ["Butter", "Flour", "Egg", "Chilli Flakes", None]


def test_the_advisor_gets_the_kitchen_and_the_conversation(tmp_path):
    class FakeAdvisor:
        async def advise(self, question: str, context: str) -> str:
            self.seen = (question, context)
            return "Make the butter block first; it needs 30 minutes to chill."

    toolbox = make_toolbox(tmp_path)
    toolbox.advisor = FakeAdvisor()
    toolbox.update_kitchen(items=[{"name": "butter", "have": "1 stick", "where": "fridge"}])
    toolbox.kitchen.remember("cook", "I want croissants for Sunday.")
    result = call(toolbox, "think_it_through", {"question": "What order should the prep go in?"})
    assert result == {"advice": "Make the butter block first; it needs 30 minutes to chill."}
    question, context = toolbox.advisor.seen
    assert question == "What order should the prep go in?"
    assert '"butter": {"have": "1 stick"' in context and "I want croissants for Sunday." in context


def test_step_pictures_are_generated_once_and_cached(tmp_path):
    from images import StepPictures

    class Fake(StepPictures):
        calls: list[str] = []

        async def _generate(self, http, key, dish, text):
            self.calls.append("generate")
            return f"{key}.webp"

    async def scenario():
        pictures = Fake(tmp_path / "pics", openai_key="k")
        first = await pictures.picture("Croissants", "Make the butter block", "Pound the butter.")
        again = await pictures.picture("Croissants", "Make the butter block", "Pound the butter.")
        no_key = await Fake(tmp_path / "none", openai_key=None).picture("x", "y", "z")
        return first, again, no_key, pictures.calls

    first, again, no_key, calls = asyncio.run(scenario())
    assert first.endswith(".webp") and again == first and calls == ["generate"]  # the repeat came from the cache
    assert no_key is None


def test_the_screen_changes_the_saved_kitchen_without_a_conversation(tmp_path):
    from server import apply_action, saved_toolbox

    path = tmp_path / "kitchen.json"

    async def tap(message: dict) -> None:
        await apply_action(saved_toolbox(path), {"type": "action", **message})

    async def scenario() -> None:
        toolbox = make_toolbox(tmp_path)
        toolbox.set_plan("pasta", PASTA)
        toolbox.set_timer("sauce", minutes=10, alert="Stir the sauce.")
        await tap({"action": "timer", "label": "sauce", "change": "pause"})
        assert Kitchen.load(path).timers[0].paused_left is not None
        await tap({"action": "timer", "label": "sauce", "change": "resume"})
        assert Kitchen.load(path).timers[0].paused_left is None
        # A tap on a timer that has just gone off is ignored, not an error that would end the conversation.
        await tap({"action": "timer", "label": "gone", "change": "pause"})
        await tap({"action": "step", "step_id": "boil", "status": "done"})
        assert Kitchen.load(path).step("boil").status == "done"
        for task in toolbox.timers.tasks.values():
            task.cancel()

    asyncio.run(scenario())


def test_the_cook_edits_the_kitchen_from_the_screen(tmp_path):
    from server import apply_action, saved_toolbox

    path = tmp_path / "kitchen.json"

    async def tap(message: dict) -> dict | None:
        return await apply_action(saved_toolbox(path), {"type": "action", **message})

    async def scenario() -> dict | None:
        await tap({"action": "kitchen", "items": [{"name": "butter", "have": "2 sticks", "where": "fridge"}]})
        await tap({"action": "kitchen", "burners": 2, "skill": "beginner"})
        await tap({"action": "kitchen", "items": [{"name": "butter", "have": "none", "where": "fridge"}]})
        await tap({"action": "kitchen", "skill": "wizard"})  # refused, and doesn't end the conversation
        note = await tap({"action": "forget_item", "name": "butter"})
        await tap({"action": "forget_item", "name": "butter"})  # already gone: ignored
        return note

    note = asyncio.run(scenario())
    saved = Kitchen.load(path)
    assert saved.profile.burners == 2 and saved.profile.skill == "beginner"
    assert "butter" not in saved.inventory
    # Basil hears about it, but keeps quiet.
    assert note["type"] == "add_system_message" and "Say nothing" in note["system_message"]


def test_next_steps_name_the_dish_so_several_can_be_told_apart(tmp_path):
    toolbox = make_toolbox(tmp_path)
    toolbox.set_plan("pasta", PASTA)
    do_now = toolbox.next_steps()["do_now"]
    assert all(s["dish"] == "pasta" and s["title"] for s in do_now)


def test_clear_all_starts_over_but_keeps_the_kitchen(tmp_path):
    async def scenario() -> Toolbox:
        toolbox = make_toolbox(tmp_path)
        toolbox.kitchen.inventory["salt"] = {"have": "plenty", "where": "spices"}
        toolbox.set_plan("pasta", PASTA)
        toolbox.set_timer("sauce", minutes=10, alert="Stir the sauce.")
        toolbox.kitchen.remember("cook", "Let's make pasta.")
        toolbox.clear_all()
        return toolbox

    toolbox = asyncio.run(scenario())
    saved = Kitchen.load(tmp_path / "kitchen.json")
    assert saved.steps == [] and saved.timers == [] and saved.history == [] and saved.recipes == {}
    assert "salt" in saved.inventory and toolbox.timers.tasks == {}


def test_the_cart_link_goes_to_the_store_that_was_shopped():
    from shopping import store_url

    assert (
        store_url("https://www.instacart.com/store/wegmans/s?k=butter")
        == "https://www.instacart.com/store/wegmans/storefront"
    )
    assert store_url("https://www.instacart.com/store/?categoryFilter=x") == "https://www.instacart.com/store"
    assert store_url("https://www.instacart.com/store/checkout_v3") == "https://www.instacart.com/store"


def test_a_queued_timer_starts_when_the_one_before_goes_off(tmp_path):
    alerts: list[str] = []

    async def scenario() -> tuple[dict, dict]:
        toolbox = make_toolbox(tmp_path, alerts=alerts)
        toolbox.set_timer("sear", minutes=0.05 / 60, alert="Flip it.")
        toolbox.set_timer("rest", minutes=0.05 / 60, alert="Slice it.", after="sear")
        queued = {t["label"]: t for t in toolbox.timers.remaining()}["rest"]
        assert "error" in await toolbox.call("adjust_timer", {"label": "rest", "action": "pause", "minutes": None})
        await asyncio.sleep(0.2)
        return queued, {t["label"]: t for t in toolbox.timers.remaining()}

    queued, after = asyncio.run(scenario())
    assert queued["follows"] == "sear" and queued["seconds"] == pytest.approx(0.05)  # waits with its whole time
    assert after == {}  # both went off, one after the other
    assert "'rest'" in alerts[0] and "Slice it." in alerts[1]


def test_reminders_are_said_at_their_time_not_rung(tmp_path):
    said: list[tuple[str, str]] = []

    async def scenario() -> list[dict]:
        kitchen = Kitchen()

        async def ring(label: str, text: str) -> None:
            said.append(("ring", text))

        async def remind(label: str, text: str) -> None:
            said.append(("say", text))

        timers = Timers(kitchen, lambda: None, ring, ring, remind=remind)
        toolbox = Toolbox(kitchen, tmp_path / "k.json", timers, None, on_cart_done=ring)
        assert "error" in await toolbox.call("remind", {"label": "x", "message": "x", "at": None, "in_minutes": None})
        toolbox.remind("rice", "Start the rice.", in_minutes=0.05 / 60)
        listed = timers.remaining()
        await asyncio.sleep(0.2)
        return listed

    listed = asyncio.run(scenario())
    assert listed[0]["kind"] == "reminder" and listed[0]["says"] == "Start the rice."
    assert said == [("say", "Start the rice.")]


def test_a_step_is_announced_when_its_start_time_comes_not_before(tmp_path):
    toolbox = make_toolbox(tmp_path, Profile(cooks=2, cook_names=["You", "Sam"]))
    toolbox.set_plan(
        "salad",
        [
            {
                "id": "dress",
                "title": "Make the dressing",
                "text": "x",
                "minutes": 5,
                "after": [],
                "equipment": "none",
                "hands_on": True,
            }
        ],
    )
    toolbox.kitchen.serve_at = time.time() + 30 * 60
    assert toolbox.clock_due() == []  # first look: just notes what's waiting
    assert toolbox.clock_due() == []  # not due yet
    toolbox.kitchen.serve_at = time.time() + 5 * 60  # the clock catches up with it
    assert toolbox.clock_due() == ["You: Make the dressing (salad)"]
    assert toolbox.clock_due() == []  # said once


def test_unprompted_lines_wait_their_turn_and_never_talk_over_each_other():
    from session import Session, Urgency

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    async def scenario() -> None:
        async def ignore(_: dict) -> None:
            pass

        socket = Socket()
        session = Session(socket, ignore)
        speaker = asyncio.create_task(session.speak_announcements())
        session.user_speaking = True  # someone's mid-sentence: nothing cuts in
        await session.announce("[first]", Urgency(quiet=0, patience=5))
        await asyncio.sleep(0.3)
        assert socket.sent == []
        session.user_speaking = False
        await asyncio.sleep(0.3)
        assert [m["system_message"] for m in socket.sent] == ["[first]"]
        session.assistant_started.set()  # Basil is saying it...
        session.assistant_speaking = True
        await session.announce("[second]", Urgency(quiet=0, patience=5))
        await asyncio.sleep(0.3)
        assert len(socket.sent) == 1  # ...and the next line waits for him to finish
        session.assistant_speaking = False
        await asyncio.sleep(0.5)
        speaker.cancel()
        assert [m["system_message"] for m in socket.sent][:2] == ["[first]", "[second]"]

    asyncio.run(scenario())


def test_a_how_to_card_shows_until_the_cook_closes_it(tmp_path):
    from server import apply_action, saved_toolbox

    toolbox = make_toolbox(tmp_path)
    steps = [
        {"title": "Fill to the valve", "text": "Water to the bottom of the valve."},
        {"title": "Pull it off", "text": "Off the heat when it sputters."},
    ]
    assert "error" in call(toolbox, "show_how", {"topic": "Moka pot", "clip": None, "steps": [], "watch_out": None})
    result = call(
        toolbox,
        "show_how",
        {"topic": "Moka pot coffee", "clip": "coffee rising", "steps": steps, "watch_out": "Don't tamp the grounds."},
    )
    assert result["ok"] and toolbox.snapshot()["how_to"]["steps"][1]["title"] == "Pull it off"
    assert Kitchen.load(tmp_path / "kitchen.json").how_to["topic"] == "Moka pot coffee"  # a reload still shows it

    note = asyncio.run(
        apply_action(saved_toolbox(tmp_path / "kitchen.json"), {"type": "action", "action": "close_how"})
    )
    assert Kitchen.load(tmp_path / "kitchen.json").how_to is None and "Say nothing" in note["system_message"]


def test_everything_on_screen_can_be_done_by_voice(tmp_path):
    async def scenario() -> Toolbox:
        toolbox = make_toolbox(tmp_path)
        toolbox.set_plan("pasta", PASTA)
        toolbox.set_timer("sauce", minutes=10, alert="Stir.")
        toolbox.remind("wine", "Open the wine.", in_minutes=30)
        steps = [{"title": "Fill to the valve", "text": "Water to the valve."}]
        toolbox.show_how("Moka pot", steps)

        # "Thanks, got it": the card goes; the screen goes back to the step.
        assert (await toolbox.call("show_on_screen", {"view": "nothing", "step_id": None, "dish": None}))["ok"]
        assert toolbox.kitchen.how_to is None
        # "Show me the toss", "show me the pasta plan"; a step or dish that isn't there is an error, not a guess.
        shown = await toolbox.call("show_on_screen", {"view": "step", "step_id": "toss", "dish": None})
        assert shown["step_id"] == "toss"
        assert "error" in await toolbox.call("show_on_screen", {"view": "step", "step_id": "nope", "dish": None})
        assert "error" in await toolbox.call("show_on_screen", {"view": "plan", "step_id": None, "dish": "soup"})
        # "Take the cream off, I used it up last week": off the list, unknown again.
        toolbox.update_kitchen(items=[{"name": "cream", "have": "a pint", "where": "fridge"}])
        toolbox.update_kitchen(remove=["Cream"])
        assert "cream" not in toolbox.kitchen.inventory
        assert (await toolbox.call("set_listening", {"always": True})) == {"ok": True, "always": True}
        # "Start over": the plan and its timers go; the reminder they asked for stays.
        toolbox.clear_plan(None)
        return toolbox

    toolbox = asyncio.run(scenario())
    assert toolbox.kitchen.steps == [] and [t.label for t in toolbox.kitchen.timers] == ["wine"]
    assert "natural_conversation_ending" in tool_definitions(shopping=False, thinking=False)  # "that's all for now"


def test_basil_is_heard_and_says_his_french_words_right():
    from session import BOOSTED_KEYWORDS, PRONUNCIATIONS

    assert "Basil" in BOOSTED_KEYWORDS
    # Phonic's limits: words up to 30 characters, pronunciations up to 50, keywords up to 50.
    assert all(len(w) <= 30 and len(say) <= 50 for w, say in PRONUNCIATIONS.items())
    assert all(len(k) <= 50 for k in BOOSTED_KEYWORDS)


def test_the_ingredient_list_can_be_edited_by_hand_or_by_voice(tmp_path):
    from server import apply_action, saved_toolbox

    toolbox = make_toolbox(tmp_path)
    ingredients = [{"name": "cream", "amount": "2 cups"}, {"name": "anchovies", "amount": "4"}]
    toolbox.set_plan("pasta", PASTA, ingredients)
    # By voice: "only half a cup of cream", "skip the anchovies", "add a pinch of chili".
    result = call(
        toolbox,
        "update_ingredients",
        {
            "dish": "pasta",
            "remove": ["Anchovies"],
            "items": [{"name": "Cream", "amount": "1/2 cup"}, {"name": "chili flakes", "amount": "a pinch"}],
        },
    )
    assert result["ingredients"] == [
        {"name": "cream", "amount": "1/2 cup"},
        {"name": "chili flakes", "amount": "a pinch"},
    ]
    assert "error" in call(toolbox, "update_ingredients", {"dish": "soup", "items": None, "remove": None})

    # By hand on the screen: renaming is a remove and an add, and Basil hears about it quietly.
    tap = {
        "type": "action",
        "action": "ingredients",
        "dish": "pasta",
        "remove": ["chili flakes"],
        "items": [{"name": "Aleppo pepper", "amount": "a pinch"}],
    }
    note = asyncio.run(apply_action(saved_toolbox(tmp_path / "kitchen.json"), tap))
    assert [i["name"] for i in Kitchen.load(tmp_path / "kitchen.json").recipes["pasta"]] == ["cream", "Aleppo pepper"]
    assert "Say nothing" in note["system_message"]


def test_a_long_conversation_starts_fresh_only_at_a_real_pause():
    import session as session_module
    from session import Session

    async def ignore(_: dict) -> None:
        pass

    class Stub:
        cart_job = None

    async def scenario() -> list[bool]:
        session = Session(socket=None, on_event=ignore)
        session.toolbox = Stub()
        session_module.FRESH_QUIET_SECONDS = 0.05
        watcher = asyncio.create_task(session.watch_for_a_fresh_start(after_turns=3, check_seconds=0.02))
        seen = []
        session.turns = 2  # not long yet
        await asyncio.sleep(0.15)
        seen.append(session.fresh_due.is_set())
        session.turns = 3
        session.user_speaking = True  # mid-sentence: wait
        await asyncio.sleep(0.15)
        seen.append(session.fresh_due.is_set())
        session.user_speaking = False
        session.announcing = True  # a timer alert is waiting to be said: wait
        await asyncio.sleep(0.15)
        seen.append(session.fresh_due.is_set())
        session.announcing = False
        await asyncio.sleep(0.2)
        seen.append(session.fresh_due.is_set())
        watcher.cancel()
        return seen

    original = session_module.FRESH_QUIET_SECONDS
    try:
        assert asyncio.run(scenario()) == [False, False, False, True]
    finally:
        session_module.FRESH_QUIET_SECONDS = original


def test_a_tool_name_said_out_loud_is_flagged_in_the_log(tmp_path, caplog):
    from session import Session

    async def ignore(_: dict) -> None:
        pass

    session = Session(socket=None, on_event=ignore)
    session.toolbox = make_toolbox(tmp_path)
    with caplog.at_level("WARNING"):
        session.remember("basil", "Tool choose not to respond.")
        session.remember("basil", "Heard. Fish next.")
    assert [r.message for r in caplog.records if "tool name" in r.message] == [
        "Basil said a tool name out loud: ['choose_not_to_respond']"
    ]


def test_timers_that_go_off_together_are_said_together():
    from session import Session, Urgency

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    async def scenario() -> list[str]:
        async def ignore(_: dict) -> None:
            pass

        socket = Socket()
        session = Session(socket, ignore)
        session.user_speaking = True  # all three come due while the cook is talking
        speaker = asyncio.create_task(session.speak_announcements())
        for text in ["[Pasta's up.]", "[Sauce timer.]", "[Bread's out.]"]:
            await session.announce(text, Urgency(quiet=0, patience=5))
        await asyncio.sleep(0.1)
        session.user_speaking = False
        await asyncio.sleep(0.3)
        speaker.cancel()
        return [m["system_message"] for m in socket.sent]

    sent = asyncio.run(scenario())
    assert len(sent) >= 1 and all(word in sent[0] for word in ("Pasta", "Sauce", "Bread"))


def test_with_only_an_openai_key_basil_still_thinks_and_shops(monkeypatch):
    import session
    import shopping

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert type(session.advisor()).__name__ == "OpenAIAdvisor"

    class Args:
        instacart = "browser"

    monkeypatch.setattr(session, "sandbox_running", lambda: True)
    assert isinstance(session.instacart(Args()), shopping.OpenAIShopper)


def test_openai_computer_actions_become_browser_input():
    from shopping import OpenAIShopper

    done: list[tuple] = []

    class Mouse:
        async def move(self, x, y, steps=1):
            done.append(("move", x, y))

        async def down(self, button="left", click_count=1):
            done.append(("down", button, click_count))

        async def up(self, button="left", click_count=1):
            done.append(("up", button))

        async def wheel(self, dx, dy):
            done.append(("wheel", dx, dy))

    class Keyboard:
        async def press(self, key):
            done.append(("press", key))

        async def type(self, text, delay=0):
            done.append(("type", text))

        async def down(self, key):
            done.append(("hold", key))

        async def up(self, key):
            done.append(("let go", key))

    class Page:
        mouse, keyboard = Mouse(), Keyboard()

    async def scenario() -> None:
        shopper = OpenAIShopper(viewer_url=None)
        for action in [
            {"type": "click", "button": "left", "x": 10, "y": 20},
            {"type": "type", "text": "butter"},
            {"type": "keypress", "keys": ["CTRL", "A"]},
            {"type": "keypress", "keys": ["ENTER"]},
            {"type": "scroll", "x": 5, "y": 5, "scroll_x": 0, "scroll_y": 300},
        ]:
            await shopper._do_openai(Page(), action)

    asyncio.run(scenario())
    assert ("type", "butter") in done and ("press", "Control+A") in done and ("press", "Enter") in done
    assert ("wheel", 0, 300) in done and ("down", "left", 1) in done


def test_plan_dish_has_the_planner_write_and_save_the_whole_plan(tmp_path):
    class Planner:
        async def plan(self, dish, notes, context):
            assert "Kitchen state" in context and notes == "for two"
            return {"ingredients": [{"name": "spaghetti", "amount": "200 g"}], "steps": PASTA}

    toolbox = make_toolbox(tmp_path)
    toolbox.advisor = Planner()
    result = asyncio.run(toolbox.call("plan_dish", {"dish": "Pasta", "notes": "for two"}))
    assert result["planned"] == "Pasta" and {s["id"] for s in result["do_now"]} == {"boil", "chop"}
    assert toolbox.kitchen.recipes["Pasta"] == [{"name": "spaghetti", "amount": "200 g"}]


def test_quick_tools_stay_silent_and_slow_ones_run_alongside_the_conversation():
    tools = {
        t["tool_schema"]["function"]["name"]: t
        for t in tool_definitions(shopping=True, thinking=True)
        if isinstance(t, dict)
    }
    assert tools["update_step"]["require_speech_before_tool_call"] is False  # Phonic's default would force a line
    for slow in ("plan_dish", "think_it_through"):
        assert tools[slow]["execution_mode"] == "async" and tools[slow]["wait_for_response"]
        assert tools[slow]["require_speech_before_tool_call"] and not tools[slow]["allow_tool_chaining"]
    assert "plan_dish" not in {
        t["tool_schema"]["function"]["name"] for t in tool_definitions(False, False) if isinstance(t, dict)
    }


def test_a_finished_meal_clears_by_tap_or_by_voice(tmp_path):
    from server import apply_action, saved_toolbox

    toolbox = make_toolbox(tmp_path)
    toolbox.set_plan("pasta", PASTA)
    for step in PASTA:
        toolbox.update_step(step["id"], "done")
    assert toolbox.kitchen.finished_at is not None
    call(toolbox, "show_on_screen", {"view": "nothing", "step_id": None, "dish": None})  # "thanks, clear the screen"
    assert toolbox.kitchen.finished_at is None and toolbox.kitchen.steps == []

    toolbox.set_plan("pasta", PASTA)
    for step in PASTA:
        toolbox.update_step(step["id"], "done")
    asyncio.run(apply_action(saved_toolbox(tmp_path / "kitchen.json"), {"type": "action", "action": "clear_finished"}))
    saved = Kitchen.load(tmp_path / "kitchen.json")
    assert saved.finished_at is None and saved.steps == []


def test_a_shopping_chrome_with_no_window_gets_one_before_connecting(monkeypatch):
    import io

    import shopping

    opened: list[str] = []

    def fake_urlopen(request, timeout=0):
        url = request if isinstance(request, str) else request.full_url
        if url.endswith("/json/list"):
            return io.BytesIO(b"[]" if not opened else b'[{"type": "page"}]')
        opened.append(url)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(shopping.urllib.request, "urlopen", fake_urlopen)
    assert shopping.ensure_window("http://127.0.0.1:9223") is True  # closed last window: open one on Instacart
    assert "json/new?https%3A%2F%2Fwww.instacart.com%2Fstore" in opened[0]
    assert shopping.ensure_window("http://127.0.0.1:9223") is False  # has a window: leave it be


def test_the_shopping_list_can_be_cleared_and_the_store_is_remembered(tmp_path):
    async def scenario() -> Toolbox:
        toolbox = make_toolbox(tmp_path, shopper=FakeShopper({"added": [], "missing": [], "needs_login": False}))
        # No store yet: Basil is told to ask, rather than the agent taking whatever Instacart lists first.
        assert "store" in (await toolbox.call("fill_instacart_cart", {"store": None, "items": ITEMS}))["error"]
        toolbox.update_kitchen(store="Safeway")
        assert (await toolbox.call("fill_instacart_cart", {"store": None, "items": ITEMS}))["started"]
        assert toolbox.kitchen.orders[-1]["store"] == "Safeway"
        await toolbox.cart_job
        toolbox.kitchen.orders.append({"items": ["eggs"], "status": "ready", "at": 1.0, "added": [], "missing": []})
        toolbox.clear_carts(at=1.0)  # one cart
        assert len(toolbox.kitchen.orders) == 1
        toolbox.clear_carts()  # "clear the shopping list"
        return toolbox

    assert asyncio.run(scenario()).kitchen.orders == []


def test_changes_the_screen_shows_get_no_spoken_reply():
    tools = {t["tool_schema"]["function"]["name"]: t for t in tool_definitions(True, True) if isinstance(t, dict)}
    for name in ("show_on_screen", "update_kitchen", "clear_carts", "set_timer"):
        assert tools[name]["forbid_speech_after_tool_call"] is True
    # Finishing a step still gets the next thing said, and a slow plan still gets the first step.
    assert not tools["update_step"].get("forbid_speech_after_tool_call")
    assert not tools["plan_dish"].get("forbid_speech_after_tool_call")


def test_a_cart_still_filling_can_be_stopped_and_a_quoted_store_is_cleaned(tmp_path):
    class SlowShopper(FakeShopper):
        async def fill_cart(self, items, store, known=()):
            self.calls.append((items, store))
            await asyncio.sleep(10)

    async def scenario() -> tuple:
        shopper = SlowShopper(None)
        done: list[dict] = []
        toolbox = make_toolbox(tmp_path, shopper=shopper, carts_done=done)
        await toolbox.call("fill_instacart_cart", {"store": '"Wegmans"', "items": ITEMS})
        at = toolbox.kitchen.orders[0]["at"]
        await asyncio.sleep(0.05)
        toolbox.clear_carts(at=at)  # Stop
        await asyncio.sleep(0.05)
        return toolbox, shopper, done

    toolbox, shopper, done = asyncio.run(scenario())
    assert toolbox.kitchen.orders == [] and toolbox.cart_job.cancelled() and done == []  # stopped, and not announced
    assert shopper.calls[0][1] == "Wegmans" and toolbox.kitchen.profile.store == "Wegmans"


def test_openai_calls_ride_out_a_network_blip(monkeypatch):
    import ssl

    import openai_api

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    real_sleep = asyncio.sleep
    monkeypatch.setattr(openai_api.asyncio, "sleep", lambda s: real_sleep(0))
    tries = []

    class Response:
        status = 200

        async def __aenter__(self):
            tries.append(1)
            if len(tries) < 3:
                raise ssl.SSLError("bad record mac")
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self, content_type=None):
            return {"output": []}

    class Http:
        def post(self, *args, **kwargs):
            return Response()

    assert asyncio.run(openai_api.respond(Http(), {"input": "hi"})) == {"output": []} and len(tries) == 3


def test_gather_and_check_steps_are_left_out_of_plans(tmp_path):
    toolbox = make_toolbox(tmp_path)
    check = {
        "id": "pasta_check_inventory",
        "title": "Check ingredients and tools",
        "text": "Make sure you have it all.",
        "minutes": 2,
        "after": [],
        "equipment": "none",
        "hands_on": True,
    }
    rest = [{**s, "after": ["pasta_check_inventory", *s["after"]]} if s["id"] == "chop" else s for s in PASTA]
    toolbox.set_plan("pasta", [check, *rest])
    ids = [s.id for s in toolbox.kitchen.steps]
    assert "pasta_check_inventory" not in ids and toolbox.kitchen.step("chop").after == []  # rewired, not stranded
    # A "plan" that's only the check step is no plan; Basil is pointed at plan_dish.
    assert "plan_dish" in call(toolbox, "set_plan", {"dish": "corn", "steps": [check], "ingredients": None})["error"]
    # Saved kitchens lose theirs on the next load.
    kitchen = Kitchen.load(tmp_path / "kitchen.json")
    kitchen.steps.append(Step(**check, dish="corn"))
    kitchen.save(tmp_path / "k2.json")
    assert all(s.id != "pasta_check_inventory" for s in Kitchen.load(tmp_path / "k2.json").steps)


def test_basil_can_change_the_cart_not_just_add_to_it(tmp_path):
    from shopping import describe_change

    report = {
        "added": [],
        "changed": ["milk: removed", "lemons: 1 to 2"],
        "missing": [],
        "needs_login": False,
        "in_cart": ["lemons: 2", "eggs: 1 dozen"],
        "note": "",
    }
    changes = [
        {"name": "milk", "change": "remove", "quantity": None, "unit": None},
        {"name": "lemons", "change": "set_quantity", "quantity": 2, "unit": None},
    ]

    async def scenario() -> tuple:
        done: list[dict] = []
        shopper = FakeShopper(report)
        toolbox = make_toolbox(tmp_path, shopper=shopper, carts_done=done)
        toolbox.update_kitchen(store="Safeway")
        started = await toolbox.call("change_cart", {"store": None, "changes": changes})
        await toolbox.cart_job
        return started, shopper, done

    started, shopper, done = asyncio.run(scenario())
    assert started["started"] and shopper.calls == [(changes, "Safeway")]
    assert done[0]["kind"] == "change" and done[0]["items"] == ["remove milk", "lemons to 2"]
    assert done[0]["in_cart"] == ["lemons: 2", "eggs: 1 dozen"]  # Basil can say what's in the cart now
    assert describe_change({"name": "parsley", "change": "add", "quantity": None}) == "add parsley"


def test_only_one_thing_drives_the_shopping_browser_at_a_time(tmp_path, monkeypatch):
    import shopping

    monkeypatch.setattr(shopping, "LOCK_FILE", tmp_path / "shopper.lock")
    with shopping.driving():
        with pytest.raises(shopping.BrowserBusy):  # a second run, from anywhere, is turned away
            with shopping.driving():
                pass
        # The sign-in check doesn't wait or barge in: it just says it can't tell right now.
        assert asyncio.run(shopping.InstacartShopper(None, viewer_url=None).signed_in()) is None
    with shopping.driving():  # free again afterwards
        pass


def test_basil_can_shop_a_store_site_and_say_how_shopping_is_going(tmp_path, monkeypatch):
    import shopping

    class SiteShopper(FakeShopper):
        async def shop_site(self, site, items, note, known=()):
            self.calls.append((site, items, note))
            return {**self.report, "cart_url": "https://murrayscheese.com/cart", "shot": "123.jpg"}

    report = {
        "added": ["prosciutto: San Daniele 1/4 lb, $18"],
        "changed": [],
        "missing": [],
        "in_cart": [],
        "needs_login": False,
        "note": "",
    }

    async def fake_describe() -> str:
        return "on Murray's search results for prosciutto"

    monkeypatch.setattr(shopping, "describe_frame", fake_describe)

    async def scenario() -> tuple:
        shopper = SiteShopper(report)
        toolbox = make_toolbox(tmp_path, shopper=shopper)
        items = [{"name": "prosciutto di San Daniele", "quantity": 0.25, "unit": "lb"}]
        await toolbox.call(
            "shop_online", {"site": "https://murrayscheese.com/", "items": items, "note": "the good one"}
        )
        while_going = await toolbox.call("check_shopping", {})
        await toolbox.cart_job
        after = await toolbox.call("check_shopping", {})
        return shopper, toolbox, while_going, after

    shopper, toolbox, while_going, after = asyncio.run(scenario())
    assert shopper.calls[0][0] == "murrayscheese.com"  # no Instacart store needed for a site of its own
    order = toolbox.kitchen.orders[0]
    assert order["kind"] == "site" and order["shot"] == "123.jpg" and order["cart_url"].endswith("/cart")
    assert while_going["status"] == "filling" and "prosciutto" in while_going["looking_at"]
    assert after["status"] == "ready" and after["added"] == report["added"]
    assert shopping.site_url("murrayscheese.com") == "https://murrayscheese.com"


def test_the_screen_shows_one_cart_per_store_not_one_per_run(tmp_path):
    from tools import carts_by_store

    runs = [
        {
            "kind": "site",
            "store": "dartagnan.com",
            "items": ["chicken"],
            "status": "ready",
            "at": 1.0,
            "in_cart": ["chicken: 1"],
            "shot": "a.jpg",
            "cart_url": "https://dartagnan.com/cart",
            "signed_in": False,
        },
        {
            "kind": "site",
            "store": "dartagnan.com",
            "items": ["duck fat"],
            "status": "ready",
            "at": 2.0,
            "missing": ["duck fat"],
        },
        {"kind": "add", "store": "Safeway", "items": ["milk"], "status": "ready", "at": 1.5, "in_cart": ["milk: 1"]},
        {"kind": "site", "store": "dartagnan.com", "items": ["butter"], "status": "filling", "at": 3.0},
    ]
    carts = carts_by_store(runs)
    assert [c["store"] for c in carts] == ["dartagnan.com", "Safeway"]  # newest first, one each
    dart = carts[0]
    assert dart["runs"] == 3 and dart["status"] == "filling" and dart["items"] == ["butter"]
    assert dart["in_cart"] == ["chicken: 1"] and dart["shot"] == "a.jpg"  # last known contents and picture carry over

    # The next run on that store is told what's already in it, so it adjusts instead of duplicating.
    toolbox = make_toolbox(tmp_path, shopper=FakeShopper(None))
    toolbox.kitchen.orders = runs[:3]
    assert toolbox._known("dartagnan.com") == ["chicken: 1"] and toolbox._known("Whole Foods") == []
    toolbox.clear_carts(store="dartagnan.com")
    assert [o["store"] for o in toolbox.kitchen.orders] == ["Safeway"]
