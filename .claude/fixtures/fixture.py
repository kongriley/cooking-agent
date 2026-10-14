"""Write a kitchen state for previewing the stove page: fixture.py <solo|party|empty> <out.json>."""

import json
import sys
import time

now = time.time()


def step(id, dish, title, text, minutes, after=(), equipment="none", hands_on=True, status="pending", started=None, uses=(), cook=None):
    return {
        "id": id, "dish": dish, "title": title, "text": text, "minutes": minutes, "after": list(after),
        "equipment": equipment, "hands_on": hands_on, "status": status,
        "started_at": None if started is None else now - started * 60, "uses": list(uses), "cook": cook,
    }


recipes = {
    "Tarte Tatin": [{"name": "apples", "amount": "6"}, {"name": "puff pastry", "amount": "1 sheet"}, {"name": "sugar", "amount": "150 g"}],
    "Pommes purée": [{"name": "potatoes", "amount": "1 kg"}, {"name": "butter", "amount": "200 g"}],
    "Poulet au vinaigre": [{"name": "chicken thighs", "amount": "8"}, {"name": "shallots", "amount": "4"}, {"name": "butter", "amount": "30 g"}],
    "Haricots verts": [{"name": "haricots verts", "amount": "500 g"}],
}
inventory = {
    "chicken thighs": {"have": "8", "where": "fridge"},
    "butter": {"have": "1 stick", "where": "fridge"},
    "crème fraîche": {"have": "none", "where": "fridge"},
    "haricots verts": {"have": "1 lb", "where": "fridge"},
}

party = [
    step("caramel", "Tarte Tatin", "Make the caramel", "Sugar and butter in the pan.", 8, status="done", cook=0),
    step("heat_oven", "Tarte Tatin", "Heat the oven", "Oven to 200°C.", 15, equipment="oven", hands_on=False, status="in_progress", started=9),
    step("pack", "Tarte Tatin", "Pack the apples", "Apples in tight, rounded side down. Pastry over, tuck.", 6, after=["caramel"], uses=["apples", "puff pastry"], cook=0),
    step("bake", "Tarte Tatin", "Bake the tarte", "Into the oven, 35 min.", 35, after=["pack", "heat_oven"], equipment="oven", hands_on=False),
    step("boil", "Pommes purée", "Boil the potatoes", "Salted water, until a knife slides in.", 25, equipment="burner", hands_on=False, status="in_progress", started=11),
    step("pass", "Pommes purée", "Pass the potatoes", "Through the ricer, then the butter.", 10, after=["boil"], uses=["potatoes", "butter"]),
    step("sear", "Poulet au vinaigre", "Sear the chicken", "Skin down, medium-high. Deep gold, 8 min. Don't move them.", 8, equipment="burner", status="in_progress", started=2, uses=["chicken thighs", "butter"], cook=1),
    step("shallots", "Poulet au vinaigre", "Soften the shallots", "Shallots in the fat. Soft, 3 min.", 3, after=["sear"], equipment="burner", uses=["shallots"]),
    step("trim", "Haricots verts", "Trim the beans", "Tops off, tails on.", 8),
    step("blanch", "Haricots verts", "Blanch the beans", "Boiling salted water, 4 min, then ice.", 15, after=["trim"], equipment="burner", hands_on=False),
]
solo = [s for s in party if s["dish"] == "Poulet au vinaigre"] + [
    step("deglaze", "Poulet au vinaigre", "Deglaze with vinegar", "Vinegar in, scrape.", 4, after=["shallots"], equipment="burner"),
    step("braise", "Poulet au vinaigre", "Braise", "Lid on, low, 25 min.", 25, after=["deglaze"], equipment="burner", hands_on=False),
]
for s in solo:
    s["cook"] = None

kind, out = sys.argv[1], sys.argv[2]
steps = {"party": party, "solo": solo, "empty": []}[kind]
cooks = 2 if kind == "party" else 1
timers = []
if kind == "party":
    timers = [
        {"label": "check_the_wine", "seconds": 300, "fire_at": now + 130, "alert": "Check the wine."},
        {"label": "heat_oven", "seconds": 900, "fire_at": now + 360, "alert": "Oven's hot.", "step_id": "heat_oven"},
        {"label": "boil", "seconds": 1500, "fire_at": now + 840, "alert": "Potatoes.", "step_id": "boil"},
    ]
data = {
    "profile": {"burners": 4, "ovens": 1, "cooks": cooks, "skill": "intermediate", "cook_names": ["You", "Sam"][:cooks], "store": "Safeway"},
    "inventory": inventory if steps else {},
    "recipes": {d: r for d, r in recipes.items() if any(s["dish"] == d for s in steps)},
    "steps": steps,
    "timers": timers,
    "orders": [],
    "history": [{"who": "cook", "text": "Sam's here, they'll do the tarte."}, {"who": "basil", "text": "Heard. Sam, apples."}] if kind == "party" else [],
    "finished_at": None,
    "serve_at": now + 75 * 60 if kind == "party" else None,
    "how_to": None,
}
open(out, "w").write(json.dumps(data, indent=2))
