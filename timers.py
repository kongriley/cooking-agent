"""Kitchen timers that outlive any single turn: each one is an asyncio task that alerts when it fires."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from kitchen import Kitchen, Timer

# Timers at least this long get a heads-up shortly before they go off, so the cook can get ready (colander in the
# sink, oven mitts out) instead of scrambling when the alarm rings.
HEADS_UP_MIN_SECONDS = 4 * 60
HEADS_UP_LEAD_SECONDS = 60


class Timers:
    """Timers persisted on the kitchen, so a reconnect mid-bake picks them back up.

    A timer can be queued behind another (sear, then rest) and starts when that one goes off or is cancelled.
    Reminders are timers too, without the dial: at their time, Basil says what the cook asked to be reminded of.
    """

    def __init__(
        self,
        kitchen: Kitchen,
        save: Callable[[], None],
        alert: Callable[[str, str], Awaitable[None]],
        nudge: Callable[[str], Awaitable[None]],
        remind: Callable[[str, str], Awaitable[None]] | None = None,
        heads_up_min_seconds: float = HEADS_UP_MIN_SECONDS,
        heads_up_lead_seconds: float = HEADS_UP_LEAD_SECONDS,
    ) -> None:
        self.kitchen = kitchen
        self.save = save
        self.alert = alert
        self.nudge = nudge
        self.remind = remind or alert
        self.heads_up_min_seconds = heads_up_min_seconds
        self.heads_up_lead_seconds = heads_up_lead_seconds
        self.tasks: dict[str, asyncio.Task] = {}

    def restore(self) -> None:
        for timer in self.kitchen.timers:
            if timer.paused_left is None and timer.follows is None:
                self._schedule(timer)

    def set(
        self, label: str, minutes: float, alert: str, step_id: str | None = None, follows: str | None = None
    ) -> Timer:
        if follows is not None:
            self.get(follows)  # it must exist to be followed
        self._remove(label)
        seconds = minutes * 60
        timer = Timer(label=label, seconds=seconds, fire_at=time.time() + seconds, alert=alert, step_id=step_id)
        timer.follows = follows
        self.kitchen.timers.append(timer)
        self.save()
        if follows is None:
            self._schedule(timer)
        return timer

    def remind_at(self, label: str, at: float, message: str) -> Timer:
        """Have Basil say `message` at the time `at` (epoch seconds)."""
        self._remove(label)
        timer = Timer(label=label, seconds=max(at - time.time(), 1), fire_at=at, alert=message, kind="reminder")
        self.kitchen.timers.append(timer)
        self.save()
        self._schedule(timer)
        return timer

    def get(self, label: str) -> Timer:
        for timer in self.kitchen.timers:
            if timer.label == label:
                return timer
        raise KeyError(f"no timer {label!r}; running: {[t.label for t in self.kitchen.timers]}")

    def pause(self, label: str) -> None:
        timer = self.get(label)
        self._not_queued(timer)
        if timer.paused_left is None:
            self._unschedule(label)
            timer.paused_left = max(timer.fire_at - time.time(), 0)
            self.save()

    def resume(self, label: str) -> None:
        timer = self.get(label)
        if timer.paused_left is not None:
            timer.fire_at, timer.paused_left = time.time() + timer.paused_left, None
            self.save()
            self._schedule(timer)

    def add(self, label: str, minutes: float) -> None:
        """Add time to a timer (negative takes time off), running or paused."""
        timer = self.get(label)
        extra = minutes * 60
        timer.seconds = max(timer.seconds + extra, 1)
        if timer.follows is not None:
            pass  # queued: it just runs longer once it starts
        elif timer.paused_left is not None:
            timer.paused_left = max(timer.paused_left + extra, 0)
        else:
            timer.fire_at += extra
            self._unschedule(label)
            self._schedule(timer)
        self.save()

    def cancel(self, label: str) -> bool:
        """Stop a timer. Anything queued behind it starts now, the way it would have when it rang."""
        removed = self._remove(label)
        self._start_followers(label)
        return removed

    def _remove(self, label: str) -> bool:
        self._unschedule(label)
        before = len(self.kitchen.timers)
        self.kitchen.timers = [t for t in self.kitchen.timers if t.label != label]
        self.save()
        return len(self.kitchen.timers) < before

    def _start_followers(self, label: str) -> None:
        for timer in self.kitchen.timers:
            if timer.follows == label:
                timer.follows, timer.fire_at = None, time.time() + timer.seconds
                self._schedule(timer)
        self.save()

    @staticmethod
    def _not_queued(timer: Timer) -> None:
        if timer.follows is not None:
            raise ValueError(f"{timer.label!r} hasn't started; it starts after {timer.follows!r}")

    def remaining(self) -> list[dict]:
        now = time.time()
        timers = [
            {
                "label": t.label,
                "seconds": t.seconds,
                "seconds_left": max(round(left(t, now)), 0),
                "paused": t.paused_left is not None,
                "step_id": t.step_id,
                "follows": t.follows,
                "kind": t.kind,
                **({"says": t.alert} if t.kind == "reminder" else {}),
            }
            for t in self.kitchen.timers
        ]
        return sorted(timers, key=lambda t: t["seconds_left"])

    def _schedule(self, timer: Timer) -> None:
        self.tasks[timer.label] = asyncio.create_task(self._run(timer))

    def _unschedule(self, label: str) -> None:
        task = self.tasks.pop(label, None)
        if task is not None:
            task.cancel()

    async def _run(self, timer: Timer) -> None:
        heads_up_at = timer.fire_at - self.heads_up_lead_seconds
        if timer.kind == "timer" and timer.seconds >= self.heads_up_min_seconds and heads_up_at > time.time():
            await asyncio.sleep(heads_up_at - time.time())
            await self.nudge(
                f"Heads-up: the '{timer.label}' timer has {round(self.heads_up_lead_seconds)} seconds left"
                f" ({timer.alert}). Tell the cook what to get ready, in a few words."
            )
        await asyncio.sleep(max(timer.fire_at - time.time(), 0))
        self.tasks.pop(timer.label, None)
        self.kitchen.timers = [t for t in self.kitchen.timers if t is not timer]
        followers = [t for t in self.kitchen.timers if t.follows == timer.label]
        self._start_followers(timer.label)
        if timer.kind == "reminder":
            await self.remind(timer.label, timer.alert)
            return
        step = f" (step {timer.step_id})" if timer.step_id else ""
        started = "".join(f" '{t.label}' ({round(t.seconds / 60, 1)} min) has started after it." for t in followers)
        await self.alert(timer.label, f"Timer '{timer.label}'{step} just went off. {timer.alert}{started}")


def left(timer: Timer, now: float) -> float:
    """Seconds until it goes off: all of it while queued, what was left while paused, else until fire_at."""
    if timer.follows is not None:
        return timer.seconds
    return timer.fire_at - now if timer.paused_left is None else timer.paused_left
