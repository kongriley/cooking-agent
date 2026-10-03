# Basil

A voice cooking coach built on [Phonic](https://phonic.ai)'s speech-to-speech API. Tell it what you have and what you
want to eat. It picks a recipe, works out what to buy, plans the cook around your stove, walks you through each step,
keeps the timers, and speaks up on its own when one goes off. It can also fill your Instacart cart.

## Run

```bash
echo 'PHONIC_API_KEY=ph_...' > .env                 # a prod Phonic key
infisical run --env=dev -- uv run server.py         # open http://localhost:8000 in Chrome
```

Infisical supplies `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. Without them Basil still runs, but without step pictures,
the "thinking" indicator, or shopping. Chrome only allows the mic on `localhost`, so on a remote box forward the port:
`ssh -L 8000:localhost:8000 -L 6080:localhost:6080 <host>`.

| Flag | Default | |
|---|---|---|
| `--voice` / `--speed` | `jerome` / `1.5` | Phonic voice and speaking speed (0.5–1.5) |
| `--kitchen` | `kitchen.json` | Where the kitchen, plan, timers and conversation history persist |
| `--instacart` | `auto` | `browser` requires the shopping sandbox, `off` disables it, `auto` uses it when it's there |
| `--port`, `--host`, `--api-base` | | |

`uv run client.py` is a typed version of the same agent for scripted runs (`/wait <seconds>`, `/quit`).
`system_prompt.md` is re-read at the start of every conversation, so prompt edits need only a page reload.

Tests: `uv run pytest`. Lint: `uv run ruff check . && uv run ruff format --check .`.

## What it does

**Conversation.** Basil talks like a terse chef: one sentence, concrete cues ("You'll smell it go nutty"), no
praise, no "let me check". It opens with "Basil here. What are we cooking?" and leads: it says what to do next
before you ask.

**Your kitchen.** Basil starts knowing nothing. It doesn't assume an empty pantry or a full one. It records what you
mention: each item with how much you have ("half a lemon", "2 packets") and where it lives (fridge, freezer, pantry,
spices, tools), plus burners, ovens, number of cooks, skill and diet. It asks about the unknowns that matter. The
Kitchen sheet shows all of this as shelves of photos.

**Recipes and planning.** For decisions worth getting right, such as choosing a recipe, planning, or recovering when
something goes wrong, Basil consults Claude Opus (`think_it_through`). Meanwhile the screen shows "Thinking it
through…". Basil then saves a plan: cookbook-voice steps, each with a duration, dependencies, the burner or oven it
takes, whether it's hands-on, and which ingredients it uses. The code does the scheduling, not the model:
- the critical path goes first;
- one cook never gets two hands-on steps at once;
- no more burners or ovens are used than you have;
- beginners get 1.5× time on hands-on work.

Several dishes can cook at once and are scheduled together. Each dish can be replanned or stopped on its own.

**Cooking.** The middle of the screen is the current step: its title, the instruction in large type, what it needs,
a countdown if it's running, and **Done** with the next step's name. The header shows every step as a numbered pill.
Tap one to read ahead or go back. Beside the step are:
- a picture of that step: a real Wikimedia Commons photo when Claude confirms it shows the step, otherwise a
  generated illustration labelled as one;
- the dish's ingredient list, cookbook style, with what's missing gathered at the top and an "Add to Instacart cart"
  button.

**Timers.** These run in the server, not the model. Starting a hands-off step sets one automatically. Basil gives a
heads-up a minute before timers of four minutes or more. When one goes off, Basil speaks without being asked, waiting
a few seconds if someone is mid-sentence. The dials at the bottom pause and resume on tap. A timer that has gone off
offers "+1 min" and "Done". You can also pause, extend or cancel timers by voice.

**Captions.** The last thing you and Basil said streams in under the dials. While nothing is cooking, the
conversation takes the middle of the screen.

**Memory.** Everything persists in `kitchen.json`, including the last 30 lines of conversation. On a page refresh or
reconnect, Basil gets a recap and picks up where you were instead of greeting you again. A finished meal shows
"Ready to eat." for an hour, then clears.

**Shopping.** Instacart has no public cart API, so a Claude computer-use agent (`shopping.py`) uses the website in a
sandboxed Chromium (`shopper/`, Docker). It searches each item, adds a sensible match, and stops at the cart: a guard
backs out of any checkout page, so you always review and pay yourself. It runs in the background while you keep
cooking, and Basil tells you when it's done or what it couldn't find. This drives the website the way a person
would, which Instacart's terms likely don't allow, so it runs on your own account at your own risk.

```bash
cd shopper && docker build -t basil-shopper . && cd ..
docker run -d --name basil-shopper --restart unless-stopped -p 127.0.0.1:9223:9223 -p 127.0.0.1:6080:6080 \
  -v basil-shopper-profile:/profile --shm-size=1g basil-shopper
```

Sign in to Instacart once at `http://localhost:6080/vnc.html`, the sandbox's screen. The login persists in the
volume, and you can watch the agent shop there.

## How it's built

| File | What it does |
|---|---|
| `server.py` + `index.html` | Web app: serves the page and bridges each browser tab to its own Phonic conversation |
| `session.py` | One Phonic conversation: config, tool dispatch, unprompted turns (timer alerts, cart updates), recap |
| `system_prompt.md` | Basil's voice and rules |
| `tools.py` | Tool schemas sent inline in the config, and their handlers |
| `kitchen.py` | Kitchen state, persistence and the step scheduler |
| `timers.py` | Persistent, pausable timers with heads-up nudges |
| `advisor.py` | The Claude Opus consult behind `think_it_through` |
| `images.py` | Ingredient/tool photos (TheMealDB, Wikipedia) and step pictures (Commons + Claude vetting, else gpt-image-2) |
| `shopping.py`, `shopper/` | The computer-use Instacart agent and its Docker browser |
| `client.py` | Terminal front end |

- **Phonic usage.** The server opens `wss://api.phonic.ai/v1/sts/ws` with `phonic_model: phonic_v1` and sends every
  tool inline as a custom websocket tool, so nothing needs setting up in the Phonic dashboard. The browser streams
  16 kHz PCM in 20 ms frames from an AudioWorklet and plays back the agent's audio. The API keys, tools and timers
  all stay on the server.
- **Speaking up unprompted.** Phonic's async tools can't make the agent talk later. Waiting on the tool leaves the
  agent stuck until it returns, and not waiting means the result is read only on your next turn. So timers and
  cart updates are asyncio tasks in the server that send `generate_reply` with a system message when they fire.
- **Our own supervisor.** `think_it_through` replaces Phonic's built-in supervisor because the server can't see
  when that one starts and stops, and it needs to in order to show that Basil is thinking.
- **Tool arguments are cleaned up.** The model sometimes sends the string `"null"` or leaves out nullable fields.
- **Old files still load.** `kitchen.json` from earlier formats loads without error.

## Known rough edges

- **Filler speech before tools.** Basil sometimes says "Let me think through…" or "Saving what you've got" before
  a tool call, despite the prompt. This comes from Phonic's model; the prompt can't fully stop it.
- **Commons rate limits.** Wikimedia Commons rate-limits bursts, so most step pictures so far are illustrations. The
  real-photo path works, but this machine has rarely reached it.
- **Inferred amounts.** Basil sometimes fills in an amount you never gave ("plenty" of butter).
- **Shopping untested at checkout.** The shopping agent is tested up to the point where it needs you to sign in. A
  full cart run still needs a signed-in sandbox.
