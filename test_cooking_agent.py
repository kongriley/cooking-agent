import asyncio
import json
import time
from pathlib import Path

import pytest

from kitchen import Kitchen, Profile, schedule
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


def test_in_progress_steps_count_down(tmp_path):
    toolbox = make_toolbox(tmp_path)
    toolbox.set_plan("pasta", PASTA)
    toolbox.kitchen.step("boil").status = "in_progress"
    toolbox.kitchen.step("boil").started_at = time.time() - 4 * 60
    slots = {s.step.id: s for s in schedule(toolbox.kitchen, now=time.time())}
    assert slots["boil"].end == pytest.approx(6, abs=0.01)
    assert slots["pasta"].start == pytest.approx(6, abs=0.01)


class FakeShopper:
    def __init__(self, report: dict | None) -> None:
        self.report = report
        self.calls: list[tuple] = []

    async def fill_cart(self, items: list[dict], store: str | None) -> dict:
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
    assert "Already filling" in again["error"] and len(calls) == 1 and calls[0][1] == "Safeway"
    assert cart["status"] == "ready" and cart["missing"] == ["flaky salt"]
    assert done[0]["added"] == ["butter: Land O Lakes 1 lb"]


def test_a_broken_shopping_browser_is_reported_not_swallowed(tmp_path):
    async def scenario() -> list[dict]:
        done: list[dict] = []
        toolbox = make_toolbox(tmp_path, shopper=FakeShopper(None), carts_done=done)
        await toolbox.call("fill_instacart_cart", {"store": None, "items": ITEMS})
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
    names = {t["tool_schema"]["function"]["name"] for t in tool_definitions(shopping=False, thinking=False)}
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


def test_step_pictures_prefer_a_vetted_photo_then_illustrate_and_cache(tmp_path):
    from images import StepPictures

    class Fake(StepPictures):
        calls: list[str] = []

        async def _commons(self, http, queries):
            self.calls.append("search")
            return ["https://photo/a.jpg", "https://photo/b.jpg"]

        async def _vet(self, http, candidates, dish, title, text):
            self.calls.append("vet")
            return candidates[1] if "laminate" in title.lower() else None

        async def _generate(self, http, key, dish, text):
            self.calls.append("generate")
            return f"{key}.webp"

    async def scenario():
        pictures = Fake(tmp_path / "pics", claude=object(), openai_key="k")
        photo = await pictures.picture("Croissants", "Laminate", "Fold three times.")
        drawn = await pictures.picture("Croissants", "Make the butter block", "Pound the butter.")
        again = await pictures.picture("Croissants", "Make the butter block", "Pound the butter.")
        no_keys = await Fake(tmp_path / "none", claude=None, openai_key=None).picture("x", "y", "z")
        return photo, drawn, again, no_keys, pictures.calls

    photo, drawn, again, no_keys, calls = asyncio.run(scenario())
    assert photo == "https://photo/b.jpg" and drawn.endswith(".webp") and again == drawn
    assert calls == ["search", "vet", "search", "vet", "generate"]  # the repeat came from the cache
    assert no_keys is None
