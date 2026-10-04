"""Kitchens to look at the screens against: python .claude/fixture.py <busy|solo|rung|finished|empty|party> out.json"""

import json
import sys
import time

now = time.time()


def step(id, dish, title, text, minutes, after=(), equipment="none", hands_on=True, status="pending", ago=None, cook=None, uses=()):
    return {"id": id, "dish": dish, "title": title, "text": text, "minutes": minutes, "after": list(after),
            "equipment": equipment, "hands_on": hands_on, "status": status, "uses": list(uses), "cook": cook,
            "started_at": now - ago * 60 if ago is not None else None}


def timer(label, minutes_left, step_id=None, total=None, kind="timer"):
    return {"label": label, "seconds": (total or minutes_left) * 60, "fire_at": now + minutes_left * 60,
            "alert": f"{label} is done", "step_id": step_id, "kind": kind}


RECIPES = {
    "Seared salmon": [{"name": "salmon fillets", "amount": "2"}, {"name": "olive oil", "amount": "1 tbsp"}, {"name": "lemon", "amount": "1"}],
    "Herb rice": [{"name": "jasmine rice", "amount": "1 cup"}, {"name": "butter", "amount": "1 tbsp"}, {"name": "parsley", "amount": "1 bunch"}],
    "Roast broccoli": [{"name": "broccoli", "amount": "1 head"}, {"name": "garlic", "amount": "3 cloves"}],
    "Chocolate pots": [{"name": "dark chocolate", "amount": "150 g"}, {"name": "cream", "amount": "300 ml"}],
}


def busy():
    return {
        "profile": {"burners": 4, "ovens": 1, "cooks": 2, "skill": "intermediate", "cook_names": ["Riley", "Sam"]},
        "recipes": RECIPES,
        "inventory": {"salmon fillets": {"have": "2", "where": "fridge"}, "cream": {"have": "none", "where": "fridge"}},
        "steps": [
            step("rice_rinse", "Herb rice", "Rinse the rice", "Rinse the rice until the water runs clear.", 3, status="done", ago=20),
            step("rice_simmer", "Herb rice", "Simmer the rice", "Bring to a boil, then cover and simmer on low for 15 minutes.", 15, ["rice_rinse"], "burner", False, "in_progress", ago=9, uses=["jasmine rice", "butter"]),
            step("broc_roast", "Roast broccoli", "Roast the broccoli", "Roast at 220°C until the edges char, about 20 minutes.", 20, [], "oven", False, "in_progress", ago=6, uses=["broccoli", "garlic"]),
            step("salmon_sear", "Seared salmon", "Sear the salmon", "Skin side down in a hot pan, 4 minutes, don't move it. When the flesh turns opaque halfway up, flip it.", 4, [], "burner", True, "in_progress", ago=1, cook=0, uses=["salmon fillets", "olive oil"]),
            step("choc_melt", "Chocolate pots", "Melt the chocolate", "Melt the chocolate with the cream over a pan of barely simmering water.", 6, [], "burner", True, "in_progress", ago=2, cook=1, uses=["dark chocolate", "cream"]),
            step("salmon_flip", "Seared salmon", "Flip and baste", "Flip, add butter and baste for 1 minute.", 2, ["salmon_sear"], "burner", True),
            step("rice_herbs", "Herb rice", "Fold in the herbs", "Fluff with a fork and fold in chopped parsley.", 2, ["rice_simmer"]),
            step("choc_set", "Chocolate pots", "Chill the pots", "Pour into cups and chill.", 30, ["choc_melt"], "none", False),
            step("plate", "Seared salmon", "Plate up", "Rice, broccoli, salmon on top, a squeeze of lemon.", 3, ["salmon_flip", "rice_herbs", "broc_roast"]),
        ],
        "timers": [timer("rice", 6, "rice_simmer", 15), timer("broccoli", 14, "broc_roast", 20), timer("salmon", 3, "salmon_sear", 4)],
        "serve_at": now + 32 * 60,
        "history": [{"who": "cook", "text": "Is the salmon ready to flip?"}, {"who": "basil", "text": "Not yet — give it three more minutes, until it's opaque halfway up."}],
    }


def solo():
    k = busy()
    k["profile"] = {"burners": 4, "ovens": 1, "cooks": 1, "skill": "intermediate"}
    k["steps"] = [s for s in k["steps"] if s["dish"] != "Chocolate pots"]
    for s in k["steps"]:
        s["cook"] = 0 if s["cook"] is not None else None
    return k


def rung():
    k = busy()
    k["timers"][0] = timer("rice", -0.4, "rice_simmer", 15)
    return k


def finished():
    k = busy()
    for s in k["steps"]:
        s["status"] = "done"
        s["started_at"] = s["started_at"] or now - 600
    k["timers"] = []
    k["finished_at"] = now - 30
    return k


def party():
    k = busy()
    k["profile"]["cooks"] = 3
    k["profile"]["cook_names"] = ["Riley", "Sam", "Ana"]
    k["steps"].append(step("salad_toss", "Green salad", "Dress the salad", "Toss leaves with vinaigrette right before serving.", 2, [], "none", True, "in_progress", ago=0.5, cook=2))
    k["recipes"]["Green salad"] = [{"name": "leaves", "amount": "1 bag"}]
    return k


def empty():
    return {"profile": {"burners": 4, "ovens": 1, "cooks": 1}}


name, out = sys.argv[1], sys.argv[2]
with open(out, "w") as f:
    json.dump(globals()[name](), f, indent=2)
