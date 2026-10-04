# Basil

A voice cooking coach on [Phonic](https://phonic.ai)'s speech-to-speech API. Tell him what you have and what you want
to eat. He picks a recipe, plans the cook around your stove and your guests' arrival time, walks you through it, keeps
the timers, and shops for what's missing.

## Run

```bash
echo 'PHONIC_API_KEY=ph_...' > .env                 # a prod Phonic key
infisical run --env=dev -- uv run server.py         # open http://localhost:8000 in Chrome
```

Run it in a terminal of its own, not from an app's Run button: another Run can stop it. Basil's planner, supervisor,
shopper and step pictures need `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (OpenAI alone covers everything; override its
model with `BASIL_OPENAI_MODEL`). Without either, Basil still cooks, using Phonic's built-in supervisor. Chrome only
allows the mic on `localhost`, so on a remote box forward the port: `ssh -L 8000:localhost:8000 <host>`.

| Flag | Default | |
|---|---|---|
| `--voice` / `--speed` | `jerome` / `1.15` | Phonic voice and speaking speed (0.5–1.5) |
| `--fresh-after` | `5` | Turns before a fresh Phonic conversation, started at the next real pause and briefed with a recap; `0` never |
| `--kitchen` | `kitchen.json` | Where the kitchen, plan, timers, carts and conversation persist |
| `--instacart` | `auto` | `local`: a headless Chrome on this computer. `browser`: the Docker sandbox. `off`. `auto`: the sandbox if it's running, else local |
| `--port`, `--host`, `--api-base` | | |

`system_prompt.md` is re-read for every conversation, so prompt edits need only a page reload; Python changes need a
restart. `uv run client.py` is a typed version for scripted runs. Tests: `uv run pytest`. Lint:
`uv run ruff check . && uv run ruff format --check .`.

## What it does

**Basil.** French, from outside Lyon: a Lyon bouchon, the line in Paris, then ten years at a bistro abroad, where he
picked up the kitchen English ("Heard." "Behind you."). Particular because he cares (good butter, salt early, taste
often, cook what's good this week), never a caricature. Dry, terse, a step ahead; he never reads out what's on
screen. Phonic carries the emotion from the persona and the example exchanges; French kitchen words are pronounced
properly (`PRONUNCIATIONS` in `session.py`). The character is "Who you are" in `system_prompt.md`.

**Listening.** Silence is his default. He answers when you say "Basil", answer his question, or plainly ask him
something about the cooking; cooks talking to each other get nothing (his `stay_quiet` tool). Timers, reminders and
steps coming due always come through. *Always on* in the Kitchen sheet keeps him listening and quietly reconnecting.

**Voice for everything.** Anything you can tap, you can say: "thanks, got it" finishes what's in front of you (a step,
a card, a rung timer); "go back", "show me the soup plan", "take the cream off my list", "clear the screen", "start
over" all work. After an action the screen already shows, Basil says nothing; the screen is the confirmation.

**Your kitchen.** Basil assumes nothing and records what you mention: each item, how much, and where it lives, plus
burners, ovens, who's cooking, skill, diet and your Instacart store. All editable in the Kitchen sheet.

**Planning.** `plan_dish` has a stronger model (Claude Opus or OpenAI) write the whole plan in about 20 seconds while
the conversation carries on; Basil says a line first. Steps read like a chef's prep list ("Garlic in. Pale gold, 2
min."), with no gather-and-check busywork. The code schedules, not the model: long chains first, one hands-on step per
cook, no more burners than you have, two things per oven, beginners get more time. Give a time to eat and the plan works
back from it so every dish lands together; when a step's start time comes, Basil says so. The **Plan** sheet is the run
sheet: every remaining step on a timeline, colour-coded by dish, with free time marked.

**The screen.** The step is an order ticket on a rail; **Done** stamps it DONE, **Start** stamps it FIRED. With two or
more cooks the screen is the pass: a compact ticket per cook, each with its own Done. Beside the step: a picture
(generated with `gpt-image-2.5-flare`, four at a time as soon as the plan exists) and the ingredient list, editable.
Timers are oven knobs; a timer can queue behind another ("sear, then rest"). "How do I use a moka pot?" gets a how-to
card of numbered steps with pictures. A finished meal shows "Plates up." until Done or 20 minutes.

**Timers and reminders.** They run in the server. Everything Basil says unprompted goes through one queue: each line
waits for a pause (a timer up to 20 seconds, a reminder up to three minutes), and things that come due together are said
together.

**Memory.** `kitchen.json` holds the kitchen, the plan, timers, carts and the last 30 lines of conversation. A reload
shows where things stand, and taps work without Basil. Every few turns (`--fresh-after`) the Phonic conversation is
swapped for a fresh one, briefed with the same recap, so a long cook stays quick. **Clear all** in the Kitchen sheet
starts over and keeps the kitchen.

**Shopping.** A computer-use agent (`shopping.py`, Claude or OpenAI) shops in a Chrome of its own, headless, with its
own profile in `~/.basil/shopper-profile`; your everyday Chrome is never touched. It stops at the cart: a guard backs out
of checkout, so you always review and pay. Sign in to Instacart once with **Sign in**: a window opens, then closes
itself once you're through. Screenshots go to the model as JPEG, since some networks break large uploads.
- **Instacart:** fills your usual store's cart (Basil asks once), or changes it ("take the milk out", "two lemons").
  **Review** opens it in your own browser.
- **Any store's site:** for something premium ("the good prosciutto from Murray's"), Basil shops that site as a guest.
  That cart lives in the shopping browser: the sheet shows a picture of it, and **Open** shows it in a window.
- **One cart per store.** Every run is told what's already in that store's cart, so it sets quantities instead of
  duplicating; the Shopping sheet shows one entry per store, with the shopper's screen live while it works. "How's
  the shopping going?" gets a real answer (`check_shopping`). Only one run drives the browser at a time.

This drives websites the way a person would, which some stores' terms don't allow; it runs on your accounts, at your
own risk. For a stricter sandbox, run the Docker browser (`shopper/`) and sign in at `http://localhost:6080/vnc.html`:

```bash
cd shopper && docker build -t basil-shopper . && cd ..
docker run -d --name basil-shopper --restart unless-stopped -p 127.0.0.1:9223:9223 -p 127.0.0.1:6080:6080 \
  -v basil-shopper-profile:/profile --shm-size=1g basil-shopper
```

## How it's built

| File | What it does |
|---|---|
| `server.py` + `index.html` | Serves the page; bridges each tab to its Phonic conversation, swapping in fresh ones |
| `session.py` | One Phonic conversation: config, tools, the unprompted-speech queue, the due-step watcher |
| `system_prompt.md` | Basil's character and principles |
| `tools.py` | Tool schemas (with per-tool Phonic speech settings) and handlers |
| `kitchen.py` | Kitchen state, persistence and the scheduler |
| `timers.py` | Persistent timers, queued timers and reminders |
| `advisor.py` | The planner and supervisor (Claude or OpenAI) |
| `openai_api.py` | OpenAI's Responses API over plain HTTP, with retries |
| `images.py` | Ingredient and dish photos (TheMealDB, Wikipedia) and generated step pictures |
| `shopping.py`, `shopper/` | The shopping agent, its headless Chrome, and the Docker browser |
| `client.py` | Terminal front end |

- **Phonic.** `wss://api.phonic.ai/v1/sts/ws` with every tool sent inline, so nothing is set up in the dashboard.
  Quick tools are silent (Phonic otherwise speaks before every call); screen actions get no reply after; slow ones
  (`plan_dish`, `think_it_through`) run async, with a line first.
- **Speaking unprompted.** Timers, reminders, due steps and cart updates are server tasks that send `generate_reply`.

## Known rough edges

- **Inferred amounts.** Basil sometimes records an amount you never gave ("plenty" of butter).
- **Leaked tool names.** Phonic occasionally speaks a tool's name; it's kept out of captions and history.
- **Shopping is slow.** A cart of 15–20 items takes several minutes.
