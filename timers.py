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
    """Timers persisted on the kitchen, so a reconnect mid-bake picks them back up."""

    def __init__(
        self,
        kitchen: Kitchen,
        save: Callable[[], None],
        alert: Callable[[str, str], Awaitable[None]],
        nudge: Callable[[str], Awaitable[None]],
        heads_up_min_seconds: float = HEADS_UP_MIN_SECONDS,
        heads_up_lead_seconds: float = HEADS_UP_LEAD_SECONDS,
    ) -> None:
        self.kitchen = kitchen
        self.save = save
        self.alert = alert
        self.nudge = nudge
        self.heads_up_min_seconds = heads_up_min_seconds
        self.heads_up_lead_seconds = heads_up_lead_seconds
        self.tasks: dict[str, asyncio.Task] = {}

    def restore(self) -> None:
        for timer in self.kitchen.timers:
            if timer.paused_left is None:
                self._schedule(timer)

    def set(self, label: str, minutes: float, alert: str, step_id: str | None = None) -> Timer:
        self.cancel(label)
        seconds = minutes * 60
        timer = Timer(label=label, seconds=seconds, fire_at=time.time() + seconds, alert=alert, step_id=step_id)
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
        if timer.paused_left is not None:
            timer.paused_left = max(timer.paused_left + extra, 0)
        else:
            timer.fire_at += extra
            self._unschedule(label)
            self._schedule(timer)
        self.save()

    def cancel(self, label: str) -> bool:
        self._unschedule(label)
        before = len(self.kitchen.timers)
        self.kitchen.timers = [t for t in self.kitchen.timers if t.label != label]
        self.save()
        return len(self.kitchen.timers) < before

    def remaining(self) -> list[dict]:
        now = time.time()
        timers = [
            {
                "label": t.label,
                "seconds": t.seconds,
                "seconds_left": max(round(t.fire_at - now if t.paused_left is None else t.paused_left), 0),
                "paused": t.paused_left is not None,
                "step_id": t.step_id,
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
        if timer.seconds >= self.heads_up_min_seconds and heads_up_at > time.time():
            await asyncio.sleep(heads_up_at - time.time())
            await self.nudge(
                f"Heads-up: the '{timer.label}' timer has {round(self.heads_up_lead_seconds)} seconds left"
                f" ({timer.alert}). Tell the cook what to get ready, in a few words."
            )
        await asyncio.sleep(max(timer.fire_at - time.time(), 0))
        self.tasks.pop(timer.label, None)
        self.kitchen.timers = [t for t in self.kitchen.timers if t is not timer]
        self.save()
        step = f" (step {timer.step_id})" if timer.step_id else ""
        await self.alert(timer.label, f"Timer '{timer.label}'{step} just went off. {timer.alert}")
