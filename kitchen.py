"""Kitchen state (what's in it, the cook's setup, the cooking plan) and the plan scheduler."""

import json
import re
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

# Beginners take longer on hands-on work and shouldn't juggle as many things at once. The juggling limit is per
# cook: two people can keep more going than one.
HANDS_ON_SPEED = {"beginner": 1.5, "intermediate": 1.0, "advanced": 0.8}
MAX_PARALLEL_STEPS = {"beginner": 2, "intermediate": 3, "advanced": 4}
# How many times to pull a late plan earlier and try again.
RELEASE_PASSES = 4
# An oven takes two things at once (two racks); a burner, one pot.
OVEN_RACKS = 2


@dataclass
class Profile:
    """What the cook has told us; None means unknown, which is different from zero."""

    burners: int | None = None
    ovens: int | None = None
    cooks: int | None = None
    skill: Skill | None = None
    dietary_notes: str = ""
    cook_names: list[str] = field(default_factory=list)  # who the cooks are, in order; the first is at the screen
    store: str | None = None  # their Instacart store, once they've said it

    def for_planning(self) -> "Profile":
        """Fill unknowns with a typical home kitchen so a plan can still be scheduled."""
        return Profile(
            burners=TYPICAL.burners if self.burners is None else self.burners,
            ovens=TYPICAL.ovens if self.ovens is None else self.ovens,
            cooks=TYPICAL.cooks if self.cooks is None else self.cooks,
            skill=self.skill or TYPICAL.skill,
            dietary_notes=self.dietary_notes,
            cook_names=self.cook_names,
            store=self.store,
        )

    def cook_name(self, index: int | None) -> str | None:
        """What to call a cook; None when there's only one, since then nobody needs telling apart."""
        cooks = self.for_planning().cooks
        if index is None or cooks < 2:
            return None
        return self.cook_names[index] if index < len(self.cook_names) else f"Cook {index + 1}"

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
    cook: int | None = None  # which cook's hands it's in, once a hands-on step is started
    cue: str | None = None  # no longer set; kept so plans saved when steps had cue clips still load


@dataclass
class Timer:
    label: str
    seconds: float
    fire_at: float
    alert: str
    step_id: str | None = None
    paused_left: float | None = None  # seconds remaining while paused; None while running
    follows: str | None = None  # the timer this one starts after; while set, it's queued and fire_at means nothing
    kind: str = "timer"  # "timer" rings and shows a dial; "reminder" is something Basil says at a set time


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
    # When the cook wants to eat; the plan works back from it so everything lands together.
    serve_at: float | None = None
    how_to: dict | None = None  # a how-to card on screen ("how do I use a moka pot"), until the cook closes it

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
        steps = drop_busywork(
            [
                Step(**{"dish": legacy_dish, "title": s["id"].replace("_", " ").capitalize(), **s})
                for s in data.get("steps", [])
            ]
        )
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
        if not self.steps:
            self.serve_at = None


# Steps that aren't cooking: gathering or checking what's needed. The ingredient list already covers that.
BUSYWORK = re.compile(
    r"\b(check|gather|get out|set out|lay out|collect|assemble|confirm|review|prepare)\b.{0,30}"
    r"\b(ingredients?|tools?|equipment|inventory|supplies)\b|\bmise en place\b|\binventory\b",
    re.IGNORECASE,
)


def is_busywork(title: str, step_id: str = "") -> bool:
    return bool(BUSYWORK.search(title) or BUSYWORK.search(step_id.replace("_", " ")))


def drop_busywork(steps: list[Step]) -> list[Step]:
    """Leave out gather-and-check steps that haven't been done, rewiring whatever waited on one to what it waited on."""
    gone = {s.id: s.after for s in steps if s.status == "pending" and is_busywork(s.title, s.id)}
    for s in steps:
        if s.id not in gone:
            s.after = list(dict.fromkeys(d for dep in s.after for d in (gone.get(dep, [dep]))))
    return [s for s in steps if s.id not in gone]


@dataclass
class Slot:
    step: Step
    start: float  # minutes from now
    end: float
    cook: int | None = None  # whose hands, for hands-on steps


def duration(step: Step, profile: Profile) -> float:
    skill = profile.for_planning().skill
    return step.minutes * HANDS_ON_SPEED[skill] if step.hands_on else step.minutes


def schedule(kitchen: Kitchen, now: float) -> list[Slot]:
    """When each unfinished step happens, and whose hands it's in, under burner/oven/cook/attention limits.

    With no serve time, everything starts as early as it can. With one, the plan works back from it, the way a good
    host plans a dinner party: the same scheduler run on the reversed plan says how late each step can start and still
    have every dish ready together, and those become the earliest each step is started. If there isn't time, it all
    starts now and simply finishes late.
    """
    profile = kitchen.profile.for_planning()
    todo = {s.id: s for s in kitchen.steps if s.status != "done"}
    after = {sid: [d for d in s.after if d in todo] for sid, s in todo.items()}
    release: dict[str, float] = {}
    if kitchen.serve_at is not None:
        pending = {sid: s for sid, s in todo.items() if s.status == "pending"}
        before = {sid: [c for c in pending if sid in after[c]] for sid in pending}  # the plan, reversed
        horizon = (kitchen.serve_at - now) / 60
        for slot in _list_schedule(pending, before, [], {}, profile):
            # Within a dish, each step follows straight on from the one before (the chicken rests the moment it's out,
            # the potatoes are mashed while hot), so only the first steps of a dish, and waits on other dishes, hold back.
            own = [d for d in after[slot.step.id] if todo[d].dish == slot.step.dish]
            if not own:
                release[slot.step.id] = max(horizon - slot.end, 0.0)
    running = []
    taken: set[int] = set()
    for s in todo.values():
        if s.status == "in_progress":
            elapsed = (now - s.started_at) / 60
            cook = None
            if s.hands_on:
                free = [c for c in range(profile.cooks) if c not in taken]
                cook = s.cook if s.cook is not None and s.cook not in taken else (free[0] if free else None)
                taken.add(cook)
            running.append(Slot(s, start=0.0, end=max(duration(s, profile) - elapsed, 0.0), cook=cook))
    pending = {sid: s for sid, s in todo.items() if s.status == "pending"}
    best = _list_schedule(pending, after, running, release, profile)
    # Working back is a guess: going forward, hands can be busy just when a step was meant to start, while they sat
    # idle earlier. If the plan comes out late, start everything that much earlier, so the waiting work fills those
    # gaps, and keep whichever plan lands closest to the serve time.
    for _ in range(RELEASE_PASSES if release else 0):
        late = max((sl.end for sl in best), default=0) - horizon
        if late <= 0.5:
            break
        release = {sid: max(r - late, 0.0) for sid, r in release.items()}
        tried = _list_schedule(pending, after, running, release, profile)
        if max((sl.end for sl in tried), default=0) >= max((sl.end for sl in best), default=0):
            break
        best = tried
    return best


def _list_schedule(
    todo: dict[str, Step],
    after: dict[str, list[str]],
    running: list[Slot],
    release: dict[str, float],
    profile: Profile,
) -> list[Slot]:
    """Earliest-start list schedule of `todo` (with `running` already under way), no step before its release time.

    Ready steps go in order of release time, then by their longest remaining path to the end of the plan, so long
    chains (dough proofing, braises) start first and short side tasks fill the gaps.
    """
    children: dict[str, list[str]] = {sid: [] for sid in todo}
    for sid in todo:
        for d in after.get(sid, []):
            if d in children:
                children[d].append(sid)

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

    capacity = {"burner": profile.burners, "oven": profile.ovens * OVEN_RACKS}
    attention = MAX_PARALLEL_STEPS[profile.skill] * profile.cooks
    finished_at: dict[str, float] = {}
    running = list(running)
    slots: list[Slot] = list(running)
    pending = list(todo.values())
    unfinished = set(todo) | {r.step.id for r in running}

    t = 0.0
    while pending:
        for slot in [r for r in running if r.end <= t]:
            running.remove(slot)
            finished_at[slot.step.id] = slot.end
        ready = [
            s
            for s in pending
            if release.get(s.id, 0.0) <= t
            and all(d not in unfinished or finished_at.get(d, float("inf")) <= t for d in after.get(s.id, []))
        ]
        ready.sort(key=lambda s: (release.get(s.id, 0.0), -tail[s.id]))
        for s in ready:
            in_use = running
            if len(in_use) >= attention:
                break
            cook = None
            if s.hands_on:
                busy = {r.cook for r in in_use if r.step.hands_on}
                free = [c for c in range(profile.cooks) if c not in busy]
                if not free:
                    continue
                cook = free[0]
            if s.equipment != "none" and sum(r.step.equipment == s.equipment for r in in_use) >= capacity[s.equipment]:
                continue
            slot = Slot(s, start=t, end=t + duration(s, profile), cook=cook)
            running.append(slot)
            slots.append(slot)
            pending.remove(s)
        if pending:
            later = [r.end for r in running if r.end > t] + [release[s.id] for s in pending if release.get(s.id, 0) > t]
            if not later:
                raise ValueError(f"steps {[s.id for s in pending]} can never start")
            t = min(later)
    return sorted(slots, key=lambda sl: (sl.start, sl.end))
