"""Kitchen state (what's in it, the cook's setup, the cooking plan) and the plan scheduler."""

import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Literal

Equipment = Literal["burner", "oven", "none"]
Where = Literal["fridge", "freezer", "pantry", "spices", "tools"]
Skill = Literal["beginner", "intermediate", "advanced"]
StepStatus = Literal["pending", "in_progress", "done"]

# Enough of the recent conversation for a reconnected agent to pick up the thread.
HISTORY_LIMIT = 30

# Beginners take longer on hands-on work and shouldn't juggle as many things at once.
HANDS_ON_SPEED = {"beginner": 1.5, "intermediate": 1.0, "advanced": 0.8}
MAX_PARALLEL_STEPS = {"beginner": 2, "intermediate": 3, "advanced": 4}


@dataclass
class Profile:
    """What the cook has told us; None means unknown, which is different from zero."""

    burners: int | None = None
    ovens: int | None = None
    cooks: int | None = None
    skill: Skill | None = None
    dietary_notes: str = ""

    def for_planning(self) -> "Profile":
        """Fill unknowns with a typical home kitchen so a plan can still be scheduled."""
        return Profile(
            burners=TYPICAL.burners if self.burners is None else self.burners,
            ovens=TYPICAL.ovens if self.ovens is None else self.ovens,
            cooks=TYPICAL.cooks if self.cooks is None else self.cooks,
            skill=self.skill or TYPICAL.skill,
            dietary_notes=self.dietary_notes,
        )

    def assumed(self) -> dict:
        """The typical values standing in for what the cook hasn't told us."""
        typical = vars(TYPICAL)
        return {k: typical[k] for k in ("burners", "ovens", "cooks", "skill") if getattr(self, k) is None}


TYPICAL = Profile(burners=4, ovens=1, cooks=1, skill="intermediate")


@dataclass
class Step:
    id: str
    dish: str  # several dishes can cook at once; their steps share the kitchen's burners, oven and hands
    title: str  # what the screen shows, glanceable from across the kitchen
    text: str  # the full instruction, which the agent speaks
    minutes: float
    after: list[str]
    equipment: Equipment
    # Hands-on steps occupy a cook; passive ones (simmering, baking, resting) just run.
    hands_on: bool
    status: StepStatus = "pending"
    started_at: float | None = None
    uses: list[str] = field(default_factory=list)  # names from the dish's ingredient list


@dataclass
class Timer:
    label: str
    seconds: float
    fire_at: float
    alert: str
    step_id: str | None = None
    paused_left: float | None = None  # seconds remaining while paused; None while running


@dataclass
class Kitchen:
    profile: Profile = field(default_factory=Profile)
    # What the cook has told us is in their kitchen: name -> {"have": their words ("2 sticks", "half a bag",
    # "none"), "where": a Where}. Anything not listed is unknown, which is different from having none.
    inventory: dict[str, dict] = field(default_factory=dict)
    recipes: dict[str, list[dict]] = field(default_factory=dict)  # dish -> [{"name", "amount"}]
    steps: list[Step] = field(default_factory=list)
    timers: list[Timer] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)  # recent {"who": "cook" | "basil", "text": ...} lines
    finished_at: float | None = None  # when the last step of the plan was done

    @classmethod
    def load(cls, path: Path) -> "Kitchen":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        # Files saved by earlier versions lack newer fields: fill them from defaults, and move the old single-dish
        # name onto its steps, so an existing kitchen keeps loading as the format grows.
        legacy_dish = data.pop("dish", None) or "Dinner"
        inventory = data.setdefault("inventory", {})
        for name, have in data.pop("pantry", {}).items():
            inventory[name] = {"have": have, "where": "pantry"}
        for name in data.pop("equipment", []):
            inventory[name] = {"have": "yes", "where": "tools"}
        for name in data.pop("lacking", []):
            inventory[name] = {"have": "none", "where": "pantry"}
        # Orders from before carts had a status were from retired fake stores; drop them.
        data["orders"] = [o for o in data.get("orders", []) if "status" in o]
        steps = [
            Step(**{"dish": legacy_dish, "title": s["id"].replace("_", " ").capitalize(), **s})
            for s in data.get("steps", [])
        ]
        timers = [Timer(**{"seconds": max(t["fire_at"] - time.time(), 1), **t}) for t in data.get("timers", [])]
        rest = {
            f.name: data[f.name] for f in fields(cls) if f.name in data and f.name not in ("profile", "steps", "timers")
        }
        return cls(profile=Profile(**data.get("profile", {})), steps=steps, timers=timers, **rest)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(path)

    def remember(self, who: str, text: str) -> None:
        self.history = [*self.history, {"who": who, "text": text}][-HISTORY_LIMIT:]

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(f"no step {step_id!r}; known steps: {[s.id for s in self.steps]}")

    @property
    def dishes(self) -> list[str]:
        return list(dict.fromkeys(s.dish for s in self.steps))

    def stock(self, name: str) -> tuple[str, dict] | None:
        """The inventory entry for an ingredient, matching loosely ('unsalted butter' finds 'butter')."""
        name = name.lower()
        if name in self.inventory:
            return name, self.inventory[name]
        for known, entry in self.inventory.items():
            if known in name or name in known:
                return known, entry
        return None

    def set_plan(self, dish: str, steps: list[Step], ingredients: list[dict] | None = None) -> None:
        """Save or replace one dish's steps, keeping progress on steps whose id carries over (so replanning mid-cook
        works). Other unfinished dishes keep cooking alongside; finished ones are cleared away."""
        others = [
            s
            for s in self.steps
            if s.dish != dish and not all(o.status == "done" for o in self.steps if o.dish == s.dish)
        ]
        ids = [s.id for s in others + steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step ids must be unique across every dish; prefix them, e.g. 'fish_sear'")
        for s in steps:
            missing = [d for d in s.after if d not in ids]
            if missing:
                raise ValueError(f"step {s.id!r} depends on unknown steps {missing}")
            if s.equipment == "burner" and self.profile.burners == 0:
                raise ValueError(f"step {s.id!r} needs a burner but the kitchen has none")
            if s.equipment == "oven" and self.profile.ovens == 0:
                raise ValueError(f"step {s.id!r} needs an oven but the kitchen has none")
        previous = {s.id: s for s in self.steps if s.dish == dish}
        for s in steps:
            if s.id in previous:
                s.status, s.started_at = previous[s.id].status, previous[s.id].started_at
        kept = self.steps
        self.steps = others + steps
        try:
            schedule(self, now=time.time())  # raises on dependency cycles
        except ValueError:
            self.steps = kept
            raise
        # Finished dishes were dropped above; their ingredient lists go with them.
        self.recipes = {d: r for d, r in self.recipes.items() if d == dish or d in {s.dish for s in others}}
        if ingredients is not None:
            self.recipes[dish] = ingredients
        self.finished_at = None

    def clear_plan(self, dish: str | None) -> None:
        """Drop one dish's steps, or every dish's with None."""
        if dish is not None and dish not in self.dishes:
            raise KeyError(f"no dish {dish!r}; cooking: {self.dishes}")
        self.steps = [s for s in self.steps if dish is not None and s.dish != dish]
        self.recipes = {d: r for d, r in self.recipes.items() if dish is not None and d != dish}


@dataclass
class Slot:
    step: Step
    start: float  # minutes from now
    end: float


def duration(step: Step, profile: Profile) -> float:
    skill = profile.for_planning().skill
    return step.minutes * HANDS_ON_SPEED[skill] if step.hands_on else step.minutes


def schedule(kitchen: Kitchen, now: float) -> list[Slot]:
    """Earliest-start list schedule of the unfinished steps under burner/oven/cook/attention limits.

    Ready steps are prioritized by their longest remaining path to the end of the plan, so long chains
    (dough proofing, braises) start first and short side tasks fill the gaps.
    """
    profile = kitchen.profile.for_planning()
    todo = {s.id: s for s in kitchen.steps if s.status != "done"}
    children: dict[str, list[str]] = {sid: [] for sid in todo}
    for s in todo.values():
        for d in s.after:
            if d in todo:
                children[d].append(s.id)

    tail: dict[str, float] = {}

    def critical_path(sid: str, visiting: frozenset[str] = frozenset()) -> float:
        if sid in visiting:
            raise ValueError(f"dependency cycle through step {sid!r}")
        if sid not in tail:
            tail[sid] = duration(todo[sid], profile) + max(
                (critical_path(c, visiting | {sid}) for c in children[sid]), default=0.0
            )
        return tail[sid]

    for sid in todo:
        critical_path(sid)

    capacity = {"burner": profile.burners, "oven": profile.ovens}
    finished_at: dict[str, float] = {}
    running: list[Slot] = []
    slots: list[Slot] = []
    for s in todo.values():
        if s.status == "in_progress":
            elapsed = (now - s.started_at) / 60
            slot = Slot(s, start=0.0, end=max(duration(s, profile) - elapsed, 0.0))
            running.append(slot)
            slots.append(slot)
    pending = [s for s in todo.values() if s.status == "pending"]

    t = 0.0
    while pending:
        for slot in [r for r in running if r.end <= t]:
            running.remove(slot)
            finished_at[slot.step.id] = slot.end
        ready = [s for s in pending if all(d not in todo or finished_at.get(d, float("inf")) <= t for d in s.after)]
        ready.sort(key=lambda s: -tail[s.id])
        for s in ready:
            in_use = [r.step for r in running]
            if len(in_use) >= MAX_PARALLEL_STEPS[profile.skill]:
                break
            if s.hands_on and sum(r.hands_on for r in in_use) >= profile.cooks:
                continue
            if s.equipment != "none" and sum(r.equipment == s.equipment for r in in_use) >= capacity[s.equipment]:
                continue
            slot = Slot(s, start=t, end=t + duration(s, profile))
            running.append(slot)
            slots.append(slot)
            pending.remove(s)
        if pending:
            if not running:
                raise ValueError(f"steps {[s.id for s in pending]} can never start")
            t = min((r.end for r in running if r.end > t), default=t)
    return sorted(slots, key=lambda sl: (sl.start, sl.end))
