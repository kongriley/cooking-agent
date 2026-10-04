// Basil's screen, minus the drawing: the connection, the voice, the plan, where everything sits in the kitchen, the
// sheets, and the sounds. Each page (glass, clay, diorama) is a renderer on top: it fills in `View` and draws.
//
// A page sets, before or after loading this:
//   View.render()            draw everything from `state` (called on every change)
//   View.tick(now)           optional: per-second updates beyond [data-until] text
//   View.pilot(mode)         optional: Basil's state: asleep | ready | listening | you | thinking | talking | off
//   View.said(who, text, live) / View.stream(text)   optional: captions
//   View.ignite()            optional: Basil just connected
// Its CSS defines --bg --top --ink --muted --line --line-2 --alarm --flame --hot --display --mono and --d0..--d7.

const View = window.View || {};
window.View = View;
const $ = (id) => document.getElementById(id);
// Text always goes in via textContent, so nothing the model writes can inject markup.
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    k.startsWith("on") ? el.addEventListener(k.slice(2), v) : el.setAttribute(k, v === true ? "" : v);
  }
  el.append(...kids.flat().filter((k) => k != null && k !== false));
  return el;
}
const SVG_NS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    k.startsWith("on") ? el.addEventListener(k.slice(2), v) : el.setAttribute(k, v);
  }
  el.append(...kids.flat().filter((k) => k != null && k !== false));
  return el;
}
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

const RATE = 16000, FRAME = 320; // 20 ms frames; the API rejects chunks over 40 ms
let ws = null, screen = null, ctx, mic, muted = false, state = null, stateAt = 0;
let talking = false, hearing = false, thinking = false, consulting = false, playhead = 0;
const playing = new Set();
const rang = new Map(); // timer label -> {at, step_id}
let closedBecause = "";
let viewing = null; // a step the cook tapped to look at; null follows what's due
let basilText = "", youText = "";

// ---------- connection, audio ----------
// Two connections: the screen's own, always open, which shows the saved kitchen and takes taps; and the conversation
// with Basil, which starts only when the cook asks. While a conversation is open, it carries both.
function connectScreen() {
  screen = new WebSocket(`ws://${location.host}/ws?talk=0`);
  screen.onopen = refresh;
  screen.onmessage = (e) => { if (!ws) onEvent(JSON.parse(e.data)); };
  screen.onclose = () => setTimeout(connectScreen, 1000);
}
function refresh() { if (screen?.readyState === 1) screen.send(JSON.stringify({ type: "refresh" })); }

// How Basil listens: always on, tap to start and mute, or hold (the pilot or Space) to talk.
const ALWAYS_KEY = "basil.alwaysOn", HOLD_KEY = "basil.pushToTalk";
let alwaysOn = false, pushToTalk = false, holding = false, retryMs = 1000;
const stayConnected = () => alwaysOn || pushToTalk;
try { alwaysOn = localStorage.getItem(ALWAYS_KEY) === "1"; pushToTalk = !alwaysOn && localStorage.getItem(HOLD_KEY) === "1"; } catch {}
function setListen(how) {
  const wasHold = pushToTalk;
  alwaysOn = how === "always"; pushToTalk = how === "hold";
  try { localStorage.setItem(ALWAYS_KEY, alwaysOn ? "1" : "0"); localStorage.setItem(HOLD_KEY, pushToTalk ? "1" : "0"); } catch {}
  if (ws && wasHold !== pushToTalk) ws.close();
  else if (stayConnected() && !ws) wake(true);
  pilot(pilotMode);
  renderSheet();
}
const setAlwaysOn = (on) => setListen(on ? "always" : "tap");
function hold(on) {
  holding = on;
  mic?.getAudioTracks().forEach((t) => (t.enabled = on));
  refreshPilot();
}
document.addEventListener("pointerdown", () => {
  sfxUnlock();
  if (ctx?.state === "suspended") ctx.resume();
  if (dictationBlocked && ws?.readyState === 1) { dictationBlocked = false; try { recognizer?.start(); } catch {} }
});

let outLevel = null, inLevel = null; // analysers: Basil's voice and the cook's
async function wake(quiet = false) {
  if (ws) return;
  ctx ||= new AudioContext({ sampleRate: RATE });
  if (!outLevel) { outLevel = ctx.createAnalyser(); outLevel.fftSize = 256; outLevel.connect(ctx.destination); }
  muted = false;
  const opened = Date.now();
  const query = [quiet && "quiet=1", pushToTalk && "ptt=1"].filter(Boolean).join("&");
  ws = new WebSocket(`ws://${location.host}/ws${query ? `?${query}` : ""}`);
  ws.onmessage = (e) => onEvent(JSON.parse(e.data));
  ws.onopen = () => { sound.ignite(); View.ignite?.(); };
  ws.onclose = () => {
    ws = null; talking = hearing = thinking = consulting = false; busyWith = null; renderBusy(); hush();
    recognizer?.abort(); mic?.getTracks().forEach((t) => t.stop()); mic = null; inLevel = null;
    pilot("asleep");
    if (closedBecause) toast(h("span", {}, closedBecause));
    refresh();
    if (stayConnected()) {
      retryMs = Date.now() - opened < 15000 ? Math.min(retryMs * 2, 60000) : 1000;
      setTimeout(() => stayConnected() && !ws && wake(true), retryMs);
    }
    closedBecause = "";
  };
  pilot("listening");
  try { await startMic(); startDictation(); } catch { pilot("off"); toast(h("span", {}, "No microphone")); }
}

let worklet = null;
async function startMic() {
  mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
  const src = `registerProcessor("mic", class extends AudioWorkletProcessor {
    constructor() { super(); this.buf = new Int16Array(${FRAME}); this.n = 0; }
    process([input]) {
      for (const s of input[0] || []) {
        this.buf[this.n++] = Math.max(-1, Math.min(1, s)) * 0x7fff;
        if (this.n === ${FRAME}) { this.port.postMessage(this.buf.slice().buffer); this.n = 0; }
      }
      return true;
    }
  });`;
  worklet ||= ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
  await worklet;
  const node = new AudioWorkletNode(ctx, "mic");
  node.port.onmessage = (e) => {
    if (muted || ws?.readyState !== 1) return;
    let str = "";
    for (const b of new Uint8Array(e.data)) str += String.fromCharCode(b);
    ws.send(JSON.stringify({ type: "audio", audio: btoa(str) }));
  };
  const source = ctx.createMediaStreamSource(mic);
  source.connect(node);
  inLevel = ctx.createAnalyser(); inLevel.fftSize = 256; source.connect(inLevel);
  if (pushToTalk) mic.getAudioTracks().forEach((t) => (t.enabled = holding));
  refreshPilot();
}

// The page's talk control: call talkPress() on click, and bindHold(el) for hold-to-talk on pointer.
function talkPress() {
  if (pushToTalk) return;
  if (!ws) return wake();
  if (!mic) return;
  muted = !muted; mic.getAudioTracks().forEach((t) => (t.enabled = !muted)); refreshPilot();
}
function bindTalk(el) {
  el.addEventListener("click", talkPress);
  el.addEventListener("pointerdown", (e) => {
    if (!pushToTalk) return;
    el.setPointerCapture(e.pointerId);
    hold(true);
    if (!ws) wake();
  });
  for (const end of ["pointerup", "pointercancel", "lostpointercapture"]) el.addEventListener(end, () => pushToTalk && holding && hold(false));
}

function play(audio) {
  const bin = atob(audio), n = bin.length >> 1, buf = ctx.createBuffer(1, n, RATE), out = buf.getChannelData(0);
  for (let i = 0; i < n; i++) { const v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8); out[i] = (v > 32767 ? v - 65536 : v) / 32768; }
  const node = ctx.createBufferSource(); node.buffer = buf; node.connect(outLevel);
  playhead = Math.max(playhead, ctx.currentTime + 0.08);
  node.start(playhead); playhead += buf.duration;
  playing.add(node); node.onended = () => playing.delete(node);
}
function hush() { playing.forEach((n) => n.stop()); playing.clear(); playhead = 0; }

// How loud whoever's talking is, 0..1, for anything that moves with the voice.
const levelBuf = new Uint8Array(256);
function voiceLevel() {
  const an = pilotMode === "talking" ? outLevel : pilotMode === "you" ? inLevel : null;
  if (!an) return 0;
  an.getByteTimeDomainData(levelBuf);
  let m = 0;
  for (const v of levelBuf) m = Math.max(m, Math.abs(v - 128));
  return m / 128;
}

// Live words while the cook talks: the browser's own recognition fills the gap before Phonic's transcript.
let recognizer = null, dictationBlocked = false;
function startDictation() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) return;
  recognizer = new Recognition();
  recognizer.continuous = true; recognizer.interimResults = true;
  recognizer.onresult = (e) => {
    if (!hearing) return;
    let text = "";
    for (let i = e.resultIndex; i < e.results.length; i++) text += e.results[i][0].transcript;
    said("you", text.trim(), true);
  };
  recognizer.onend = () => { if (ws?.readyState === 1 && !dictationBlocked) setTimeout(() => recognizer?.start(), 250); };
  recognizer.onerror = (e) => { if (e.error === "not-allowed" || e.error === "service-not-allowed") dictationBlocked = true; };
  try { recognizer.start(); } catch { dictationBlocked = true; }
}

const SLOW_TOOLS = ["think_it_through", "plan_dish"];
let busyWith = null, busySince = 0, busyTicker = null;
function busyLabel(tool, parameters = {}) {
  if (tool === "plan_dish") return parameters.dish ? `Planning ${parameters.dish.toLowerCase()}…` : "Planning…";
  return "Thinking…";
}
const THINKING_MS = 10000;
let thinkingTimer = null;
function setThinking(on) {
  thinking = on;
  clearTimeout(thinkingTimer);
  if (on) thinkingTimer = setTimeout(() => setThinking(false), THINKING_MS);
  refreshPilot();
}
const busyText = () => {
  const secs = Math.floor((Date.now() - busySince) / 1000);
  return busyWith && secs >= 3 ? `${busyWith} ${secs}s` : busyWith || "";
};
function renderBusy() {
  clearInterval(busyTicker);
  busySince = busyWith ? Date.now() : 0;
  if (busyWith) busyTicker = setInterval(() => { for (const el of document.querySelectorAll("[data-busy]")) el.textContent = busyText(); }, 1000);
  render();
}

function onEvent(m) {
  switch (m.type) {
    case "audio_chunk": play(m.audio); if (m.text) stream(m.text); break;
    case "input_text": said("you", m.text, false); break;
    case "tool_started":
      if (SLOW_TOOLS.includes(m.tool_name)) { consulting = true; busyWith = busyLabel(m.tool_name, m.parameters); renderBusy(); }
      break;
    case "tool_result":
      if (m.silent) setThinking(false);
      if (m.tool_name === "show_on_screen" && m.output.ok) showOnScreen(m.output);
      if (m.tool_name === "set_listening" && m.output.ok) setAlwaysOn(m.output.always);
      if (SLOW_TOOLS.includes(m.tool_name)) { consulting = false; busyWith = null; renderBusy(); }
      break;
    case "assistant_started_speaking": talking = true; setThinking(false); said("basil", "", true); break;
    case "assistant_finished_speaking": talking = false; View.said?.("basil", basilText, false); break;
    case "interrupted_response": talking = false; hush(); break;
    case "user_started_speaking": hearing = true; hush(); said("you", "", true); break;
    case "user_finished_speaking": hearing = false; View.said?.("you", youText, false); setThinking(true); break;
    case "timer_fired": ring(m.label); break;
    case "phonic_closed": closedBecause = m.reason; break;
    case "state":
      state = m; stateAt = Date.now();
      if (!hearing && (m.said.cook || "") !== youText) said("you", m.said.cook || "", false);
      if (!talking && !consulting && (m.said.basil || "") !== basilText) said("basil", m.said.basil || "", false);
      for (const [label, r] of rang) if (r.step_id && m.steps.find((x) => x.id === r.step_id)?.status === "done") rang.delete(label);
      render();
      break;
  }
  refreshPilot();
}

function said(who, text, live) {
  if (who === "basil") basilText = text; else youText = text;
  View.said?.(who, text, live);
}
function stream(text) {
  text = text.replace(/\btool_[a-z_]+\b\.?/g, "");
  if (!text) return;
  if (basilText && !/\s$/.test(basilText) && !/^\s/.test(text) && (/[.!?,:;]$/.test(basilText) || /^[A-Z]/.test(text))) text = " " + text;
  basilText += text;
  View.stream ? View.stream(text, basilText) : View.said?.("basil", basilText, true);
}

let pilotMode = "asleep";
function pilot(mode) { pilotMode = mode; View.pilot?.(mode); }
function refreshPilot() {
  if (!ws || ws.readyState > 1) return;
  if (!mic) return pilot("off");
  const idle = pushToTalk && !holding ? "ready" : "listening";
  pilot(muted ? "off" : holding ? "you" : talking ? "talking" : hearing ? "you" : thinking || consulting ? "thinking" : idle);
}

// ---------- sound ----------
// Small, quiet sounds, all made here: a click for a tap, a tock for done, the igniter for Basil arriving.
let sfx = null;
function sfxUnlock() { try { sfx ||= new AudioContext(); if (sfx.state === "suspended") sfx.resume(); } catch {} }
function noise(dur) {
  const b = sfx.createBuffer(1, Math.ceil(sfx.sampleRate * dur), sfx.sampleRate), d = b.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  const n = sfx.createBufferSource(); n.buffer = b; return n;
}
function envelope(g, t, peak, a, d) { g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(peak, t + a); g.gain.exponentialRampToValueAtTime(0.0001, t + a + d); }
function click(at = 0, freq = 3200, vol = 0.05) {
  if (!sfx) return;
  const t = sfx.currentTime + at, n = noise(0.03), f = sfx.createBiquadFilter(), g = sfx.createGain();
  f.type = "bandpass"; f.frequency.value = freq; f.Q.value = 4;
  envelope(g, t, vol, 0.002, 0.03); n.connect(f).connect(g).connect(sfx.destination); n.start(t); n.stop(t + 0.05);
}
function tone(freq, at, vol, dur, type = "sine") {
  if (!sfx) return;
  const t = sfx.currentTime + at, o = sfx.createOscillator(), g = sfx.createGain();
  o.type = type; o.frequency.value = freq; envelope(g, t, vol, 0.006, dur);
  o.connect(g).connect(sfx.destination); o.start(t); o.stop(t + dur + 0.05);
}
function woosh(at, dur, vol = 0.09) {
  if (!sfx) return;
  const t = sfx.currentTime + at, n = noise(dur), f = sfx.createBiquadFilter(), g = sfx.createGain();
  f.type = "lowpass"; f.frequency.setValueAtTime(300, t); f.frequency.exponentialRampToValueAtTime(1400, t + 0.12); f.frequency.exponentialRampToValueAtTime(500, t + dur);
  g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(vol, t + 0.08); g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  n.connect(f).connect(g).connect(sfx.destination); n.start(t); n.stop(t + dur);
}
const sound = {
  tap: () => click(0, 2400, 0.04),
  done: () => { tone(196, 0, 0.09, 0.12, "triangle"); tone(784, 0.06, 0.035, 0.5); tone(1175, 0.11, 0.025, 0.6); },
  start: () => { click(0); click(0.07); woosh(0.12, 0.6); },
  ignite: () => { click(0, 3600, 0.07); click(0.11, 3600, 0.07); click(0.22, 3600, 0.07); woosh(0.3, 0.9); },
  ring: () => { [[880, 0], [1175, 0.16], [880, 0.5], [1175, 0.66]].forEach(([f, at]) => tone(f, at, 0.07, 0.5)); },
  boot: () => { tone(110, 0, 0.05, 0.6, "sine"); tone(220, 0.08, 0.03, 0.7); tone(440, 0.2, 0.02, 0.9); },
  whoosh: () => woosh(0, 0.5, 0.05),
};

// ---------- helpers ----------
function hello() {
  const hour = new Date().getHours();
  if (hour >= 5 && hour < 11) return "Morning. What are we making?";
  if (hour >= 11 && hour < 15) return "What's for lunch?";
  if (hour >= 15 && hour < 22) return "What's for dinner?";
  return "Late one. What are we making?";
}
function act(action, extra = {}) {
  const message = JSON.stringify({ type: "action", action, ...extra });
  if (ws?.readyState === 1) ws.send(message);
  else if (ws) ws.addEventListener("open", () => ws.send(message), { once: true });
  else if (screen?.readyState === 1) screen.send(message);
}
function hours(m) {
  m = Math.max(1, Math.round(m));
  if (m < 60) return `${m} min`;
  return m % 60 ? `${Math.floor(m / 60)} hr ${m % 60} min` : `${m / 60} hr`;
}
// Short durations for small spaces: "12m", "1h 20".
function short(m) {
  m = Math.max(1, Math.round(m));
  if (m < 60) return `${m}m`;
  return m % 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}` : `${m / 60}h`;
}
function clock(sec) {
  sec = Math.max(0, Math.round(sec));
  const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = String(sec % 60).padStart(2, "0");
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}
const words = (label) => { const w = label.replaceAll("_", " "); return w.charAt(0).toUpperCase() + w.slice(1); };
function listing(items) {
  const text = items.length < 3 ? items.join(" & ") : `${items.slice(0, -1).join(", ")} & ${items.at(-1)}`;
  return text.charAt(0).toUpperCase() + text.slice(1);
}
const none = (have) => have != null && /^(none|no|0|out|nothing)\b/i.test(String(have).trim());
const atClock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const dishes = () => [...new Set((state?.steps || []).map((x) => x.dish))];
const dishIndex = (d) => Math.max(0, dishes().indexOf(d)) % 8;
const dishColor = (d) => `var(--d${dishIndex(d)})`;
const cookName = (st) => (state.cooks.length > 1 && st.hands_on && st.cook != null ? state.cooks[st.cook] : null);
const unblocked = (st) => st.after.every((id) => (state.steps.find((x) => x.id === id)?.status ?? "done") === "done");
const stepNumber = (st) => state.steps.filter((x) => x.dish === st.dish).indexOf(st) + 1;
const sameWords = (a, b) => a.toLowerCase().replace(/[^a-z0-9]+/g, "") === b.toLowerCase().replace(/[^a-z0-9]+/g, "");
const abbreviate = (text, n) => (text.length > n ? `${text.slice(0, n - 1).trim()}…` : text);
// An instruction, made to scan: one sentence per line, the numbers you act on in bold, what-to-look-for lines lighter.
const CUE = /^(if|when|once|until|the|it|it's|they|you'll|you should|this)\b/i;
const MEASURE = /(\d+(?:[.,/]\d+)?(?:\s*(?:–|-|to)\s*\d+(?:[.,/]\d+)?)?\s*(?:°\s*[FC]?|degrees(?: [FC])?|minutes?|mins?|min|seconds?|hours?|inch(?:es)?|-inch|cm|mm|cups?|tbsp|tsp|tablespoons?|teaspoons?|oz|ounces?|g|grams?|kg|lbs?|pounds?|ml|l)\b|\d+\s*°[CF]?)/g;
function instructionLines(text, cls = "instruction") {
  const sentences = text.trim().split(/(?<=[.!?])\s+(?=[A-Z])/);
  return h("div", { class: cls }, sentences.map((line) =>
    h("p", { class: CUE.test(line) ? "cue" : false }, line.split(MEASURE).map((part, i) => (i % 2 ? h("b", {}, part) : part)))));
}

// ---------- the plan ----------
const READY_FOR_MS = 20 * 60 * 1000;
function plan() {
  const finished = state?.finished_at != null;
  const eating = finished && Date.now() - state.finished_at * 1000 < READY_FOR_MS;
  const steps = finished && !eating ? [] : state?.steps || [];
  const open = steps.filter((x) => x.status !== "done");
  const ready = open.filter((x) => x.status === "pending" && x.start === 0);
  const hands = (state?.cooks || [null]).map((_, i) =>
    ready.find((x) => x.hands_on && (x.cook ?? 0) === i) || open.find((x) => x.hands_on && x.status === "in_progress" && (x.cook ?? 0) === i));
  const current = (hands[0]?.status === "pending" && hands[0]) || ready.find((x) => !x.hands_on) || hands[0] || hands.find(Boolean);
  const running = open.find((x) => x.status === "in_progress");
  const upcoming = open.filter((x) => x.status === "pending" && x.start > 0).sort((a, b) => a.start - b.start)[0];
  const shown = steps.find((x) => x.id === viewing) || current || running || upcoming;
  return { steps, open, ready, hands, current, running, upcoming, shown, eating };
}
const LATE_AFTER = 5;
const readyAt = () => stateAt + Math.max(0, ...plan().open.map((x) => x.end ?? 0)) * 60000;
const lateBy = () => (state?.serve_at && plan().open.length ? (readyAt() - state.serve_at * 1000) / 60000 : 0);

// What needs the cook, most urgent first: a timer that's gone off, then each cook's work, then what can be started.
// With nothing to do yet, the next thing coming. Each: {kind: "rang" | "step" | "eat", key, s?, cook?, anyone?, label?}
function objectives() {
  const out = [];
  for (const [label, r] of rang) out.push({ kind: "rang", key: `rang:${label}`, label, r, s: r.step_id && state?.steps.find((x) => x.id === r.step_id) });
  if (!state) return out;
  const { steps, open, ready, hands, current, upcoming, shown, eating } = plan();
  if (viewing && shown) return [...out, { kind: "step", key: shown.id, s: shown, viewing: true }];
  const has = (x) => out.some((o) => o.s === x);
  if ((state.cooks?.length || 1) > 1) {
    const nextFor = (i) => open.filter((x) => x.hands_on && x.status === "pending" && (x.cook ?? 0) === i && unblocked(x)).sort((a, b) => a.start - b.start)[0];
    hands.forEach((x, i) => { const st = x || nextFor(i); if (st && !has(st)) out.push({ kind: "step", key: st.id, s: st, cook: i }); });
    for (const st of ready.filter((x) => !x.hands_on && !has(x)).slice(0, 1)) out.push({ kind: "step", key: st.id, s: st, anyone: true });
  } else if (current && !has(current)) out.push({ kind: "step", key: current.id, s: current });
  if (!out.some((o) => o.kind === "step") && upcoming && !has(upcoming)) out.push({ kind: "step", key: upcoming.id, s: upcoming, waiting: true });
  if (!out.length && eating && steps.length) out.push({ kind: "eat", key: "eat" });
  return out;
}
// The next few things coming, not already in front of the cook.
function comingUp(n = 3) {
  const skip = new Set(objectives().map((o) => o.s).filter(Boolean));
  return plan().open.filter((x) => !skip.has(x) && x.status === "pending" && x.start > 0).sort((a, b) => a.start - b.start).slice(0, n);
}

// What a step can be told: done if it's in your hands or cooking, started if it's due and cooks by itself, or started
// early once nothing holds it up.
function stepAction(st) {
  const due = st.status === "pending" && st.start === 0;
  if (st.status === "in_progress" || (due && st.hands_on)) return { label: "Done", status: "done", primary: true };
  if (due) return { label: "Start", status: "started", primary: true };
  if (st.status === "pending" && unblocked(st)) return { label: "Start now", status: "started", primary: false };
  if (st.status === "done") return { label: "Undo", status: "not_started", primary: false };
  return null;
}
// The one thing a tap or Enter does for an objective.
function objectiveAction(o) {
  if (o.kind === "rang") return { label: "Done", run: () => { rang.delete(o.label); if (o.r.step_id) act("step", { step_id: o.r.step_id, status: "done" }); sound.done(); render(); } };
  if (o.kind === "eat") return { label: "Done", run: () => { sound.done(); act("clear_finished"); } };
  const a = stepAction(o.s);
  if (!a) return null;
  return { ...a, run: () => {
    (a.status === "done" ? sound.done : a.status === "started" ? sound.start : sound.tap)();
    if (viewing === o.s.id && a.status !== "not_started") viewing = null;
    act("step", { step_id: o.s.id, status: a.status });
  } };
}
const moreTime = (o) => { rang.delete(o.label); act("timer_again", { label: o.label, minutes: 1 }); sound.tap(); render(); };
function timerFor(st) { return (state.timers || []).find((t) => t.step_id === st.id && t.kind !== "reminder"); }
// A step's time on the stove: until (ms), total (s), from its timer when it has one.
function timing(st) {
  const t = timerFor(st);
  if (t) return { until: stateAt + t.seconds_left * 1000, total: t.seconds, paused: t.paused, left: t.seconds_left };
  if (st.status === "in_progress" && !st.hands_on && st.end != null) return { until: stateAt + st.end * 60000, total: st.minutes * 60 };
  return null;
}
// 0..1 of the time left; 1 with no timing.
function remaining(tm, now = Date.now()) {
  if (!tm) return 1;
  if (tm.paused) return Math.max(0, Math.min(1, tm.left / (tm.total || 1)));
  return Math.max(0, Math.min(1, (tm.until - now) / 1000 / (tm.total || 1)));
}
function look(delta) {
  const { steps, current, shown } = plan();
  if (!shown) return;
  const i = steps.indexOf(shown) + delta;
  if (i < 0 || i >= steps.length) return;
  viewing = steps[i] === current ? null : steps[i].id;
  sound.tap();
  render();
}
function view(stepId) { sound.tap(); viewing = viewing === stepId ? null : stepId; render(); }
function backToNow() { sound.tap(); viewing = null; render(); }

// ---------- where everything is ----------
// The kitchen laid out: what's on each burner (a dish keeps its pan where it was; upcoming steps take the burner that
// frees up first, shown as a ghost), in each oven, and on the counter.
const burnerOf = new Map(); // step id -> burner index, so a pan stays where it was put
const PREHEAT = /\b(heat|preheat)\b/i;
const POT_WORDS = /\b(boil|blanch|simmer|braise|stock|pasta|poach|pot|soup|potatoes|water|beans)\b/i;
function kitchenSize() {
  const p = state?.kitchen?.profile || {};
  return { B: Math.min(p.burners ?? 4, 8), O: Math.min(p.ovens ?? 1, 2) };
}
function placement() {
  const { B, O } = kitchenSize();
  const { steps, open } = plan();
  const active = (x) => x.status === "in_progress" || (x.status === "pending" && x.start === 0);
  const rungSteps = new Set([...rang.values()].map((r) => r.step_id).filter(Boolean));
  const objs = objectives();
  const focus = new Map();
  objs.forEach((o, i) => o.s && !focus.has(o.s.id) && focus.set(o.s.id, { primary: i === 0, cook: o.s.cook, who: o.anyone ? "Anyone" : cookName(o.s), rung: o.kind === "rang" }));
  const info = (x) => ({ step: x, timing: timing(x), rung: rungSteps.has(x.id), focus: focus.get(x.id) || null, viewing: viewing === x.id, pot: POT_WORDS.test(`${x.title} ${x.text}`) });

  // Burners
  const taken = new Array(B).fill(null), freeAt = new Array(B).fill(0), ghosts = new Array(B).fill(null);
  const burnerSteps = steps.filter((x) => x.equipment === "burner");
  const lastFor = (dish) => {
    for (const x of [...burnerSteps].reverse()) if (x.dish === dish && x.status === "done" && burnerOf.has(x.id)) return burnerOf.get(x.id);
    return null;
  };
  const place = (x, ok) => {
    const want = [burnerOf.get(x.id), lastFor(x.dish)].filter((b) => b != null && b < B);
    for (const b of want) if (ok(b)) return b;
    for (let b = 0; b < B; b++) if (ok(b)) return b;
    return null;
  };
  // A step that follows straight on from one still in a pan (finish the sauce after the braise) is that pan's next.
  const inPan = (x) => taken.some((t) => t && t.dish === x.dish && x.after.includes(t.id));
  const now = burnerSteps.filter(active).sort((a, b) => (a.status === "in_progress" ? 0 : 1) - (b.status === "in_progress" ? 0 : 1));
  const queued = new Map(); // burner -> the step waiting in its pan
  for (const x of now) {
    if (inPan(x)) { const b = taken.findIndex((t) => t && t.dish === x.dish && x.after.includes(t.id)); queued.set(b, x); burnerOf.set(x.id, b); continue; }
    const b = place(x, (b) => !taken[b]);
    if (b == null) continue;
    taken[b] = x; freeAt[b] = x.end ?? 0; burnerOf.set(x.id, b);
  }
  for (const x of burnerSteps.filter((x) => x.status === "pending" && x.start > 0).sort((a, b) => a.start - b.start)) {
    const b = place(x, (b) => freeAt[b] <= x.start + 0.01);
    if (b == null) continue;
    if (!taken[b] && !ghosts[b]) ghosts[b] = x;
    freeAt[b] = x.end ?? x.start + x.minutes; burnerOf.set(x.id, b);
  }
  const burners = taken.map((x, b) => ({
    index: b,
    pan: x ? { ...info(x), key: `pan:${x.dish}:${b}`, next: queued.get(b) || null } : null,
    ghost: !x && ghosts[b] ? { step: ghosts[b], at: stateAt + ghosts[b].start * 60000 } : null,
  }));

  // Ovens: two racks each; preheating warms it.
  const ovenSteps = open.filter((x) => x.equipment === "oven" && active(x));
  const ovens = Array.from({ length: O }, (_, i) => {
    const mine = ovenSteps.filter((_, k) => O === 1 || k % O === i);
    const heating = mine.find((x) => PREHEAT.test(x.title));
    const items = mine.filter((x) => x !== heating).slice(0, 2).map(info);
    return { index: i, heating: heating ? info(heating) : null, items, hot: !!heating || items.length > 0 };
  });
  const ovenNext = open.filter((x) => x.equipment === "oven" && x.status === "pending" && x.start > 0).sort((a, b) => a.start - b.start)[0];

  // The counter: prep in hand, things resting or chilling, and timers that aren't any one step's.
  const counterSteps = open.filter((x) => x.equipment === "none" && active(x));
  const loose = (state?.timers || []).filter((t) => t.kind !== "reminder" && !t.follows && (!t.step_id || !steps.some((x) => x.id === t.step_id)));
  const counter = [
    ...counterSteps.map((x) => ({ kind: "step", key: `board:${x.id}`, ...info(x) })),
    ...loose.map((t) => ({ kind: "timer", key: `timer:${t.label}`, timer: t, label: words(t.label), rung: rang.has(t.label),
      timing: { until: stateAt + t.seconds_left * 1000, total: t.seconds, paused: t.paused, left: t.seconds_left } })),
  ];
  return { B, O, burners, ovens, ovenNext, counter, focus };
}

// ---------- shopping ----------
const NOTICE_MS = 15 * 1000;
const cartUrl = (o) => o.cart_url || "https://www.instacart.com/store";
const VIEWER_URL = `http://${location.hostname}:6080/vnc.html?autoconnect=1&resize=scale`;
const setAside = new Set(), noticed = new Set();
const itemName = (added) => added.split(":")[0].trim();
function link(url, label, cls) { return h("a", { class: cls, href: url, target: "_blank", rel: "noopener" }, label); }
function shopperLink(label, cls) {
  if (state.shopping.viewer) return link(VIEWER_URL, label, cls);
  if (label === "Watch") return h("button", { class: cls, onclick: () => openSheet("shopping") }, label);
  return h("button", { class: cls, title: "Opens a window to sign in to Instacart; it closes itself once you're in", onclick: () => act("show_shopper") }, label);
}
function cartActions(o, cls) {
  if (o.status === "needs_login") return [shopperLink("Sign in", cls)];
  if (o.status === "filling") return state.shopping.viewer ? [shopperLink("Watch", cls)] : [];
  if (o.status !== "ready") return [];
  if (o.kind !== "site") return [link(cartUrl(o), "Review", cls)];
  return [h("button", { class: cls, onclick: () => act("open_cart", { url: o.cart_url }) }, "Open")];
}
function storeName(o) {
  const name = o.store || "Instacart";
  if (!name.includes(".")) return name;
  const base = name.split(".").slice(-2, -1)[0] || name;
  return base.charAt(0).toUpperCase() + base.slice(1);
}
function cartTitle(o) {
  const n = (o.in_cart?.length ? o.in_cart : o.status === "ready" ? o.added || [] : o.items).length;
  return [storeName(o), n && `${n} item${n === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
}
function cartItems(o) {
  if (o.status === "ready" && o.in_cart?.length) return listing(o.in_cart.map(itemName)).toLowerCase();
  return listing(o.status === "ready" && o.kind !== "change" ? (o.added || []).map(itemName) : o.items).toLowerCase();
}
function cartLine(o) {
  if (o.status === "filling") return "Shopping…";
  if (o.status === "needs_login") return "Sign in to Instacart";
  if (o.status === "interrupted") return "Stopped";
  if (o.status === "failed") return "Couldn't shop";
  const done = o.runs > 1 || o.kind === "change" ? "Updated" : "Ready";
  return o.missing?.length ? `${done} · ${o.missing.length} not found` : done;
}
function renderNotice() {
  const done = (state?.carts || []).find((o) => ["ready", "needs_login", "failed"].includes(o.status) && !setAside.has(o.at) && !noticed.has(o.at)
    && Date.now() - (o.done_at ?? o.at) * 1000 < NOTICE_MS);
  if (!done) return;
  noticed.add(done.at);
  toast(h("span", {}, h("b", {}, storeName(done)), ` · ${cartLine(done)}`), ...cartActions(done, ""));
}

// What Basil puts on screen when asked, the same as the taps that do it.
function showOnScreen({ showing, step_id, dish }) {
  if (showing === "now" || (showing === "step" && !step_id)) { viewing = null; planDish = null; openSheet("nothing"); }
  else if (showing === "nothing") { viewing = null; planDish = null; rang.clear(); for (const o of state?.carts || []) setAside.add(o.at); $("b-toast").hidden = true; openSheet("nothing"); }
  else if (showing === "step") { viewing = step_id; openSheet("nothing"); }
  else if (showing === "plan") { planDish = dish; openSheet("plan"); }
  else openSheet(showing);
  render();
}

// ---------- chrome shared by every page: drawer, toast, how-to ----------
const CORE_CSS = `
  .b-mono { font-family: var(--mono); }
  #b-toast { position: fixed; z-index: 60; left: 50%; bottom: max(110px, calc(env(safe-area-inset-bottom) + 110px)); transform: translateX(-50%);
    display: flex; align-items: center; gap: 14px; padding: 10px 12px 10px 18px; border-radius: 999px; background: var(--ink); color: var(--bg);
    font: 500 15px var(--display); animation: b-toast .3s cubic-bezier(.3,1.5,.5,1); max-width: calc(100% - 32px); }
  #b-toast b { font-weight: 700; }
  #b-toast a, #b-toast button { font: inherit; border: 0; cursor: pointer; padding: 6px 14px; border-radius: 999px; font-weight: 700; font-size: 14px; color: var(--ink); background: var(--bg); text-decoration: none; }
  #b-toast button.x { background: none; color: inherit; opacity: .6; padding: 6px; }
  @keyframes b-toast { from { transform: translate(-50%, 12px); opacity: 0; } }
  @keyframes b-fade { from { opacity: 0; } }
  @keyframes b-drawer { from { transform: translateX(28px); opacity: 0; } }
  @keyframes b-breathe { 50% { opacity: .45; } }
  #b-veil { position: fixed; inset: 0; z-index: 40; background: color-mix(in srgb, var(--bg) 55%, transparent); backdrop-filter: blur(4px); animation: b-fade .2s; }
  #b-sheet { position: fixed; z-index: 50; top: 12px; right: 12px; bottom: 12px; width: min(520px, calc(100% - 24px)); padding: 24px 26px 28px; overflow-y: auto;
    background: var(--top); color: var(--ink); font-family: var(--display); border-radius: 22px; box-shadow: 0 0 0 1px var(--line), 0 30px 80px -20px rgb(0 0 0 / .45);
    display: grid; align-content: start; gap: 24px; animation: b-drawer .3s cubic-bezier(.2,.9,.25,1); scrollbar-width: thin; user-select: none; }
  #b-sheet button { font: inherit; color: inherit; background: none; border: 0; cursor: pointer; padding: 0; }
  #b-sheet .top { display: flex; justify-content: space-between; align-items: center; }
  #b-sheet h2 { margin: 0; font-size: 30px; letter-spacing: -.03em; }
  #b-sheet .top button { width: 36px; height: 36px; border-radius: 50%; font-size: 16px; color: var(--muted); }
  #b-sheet .top button:hover { color: var(--ink); background: color-mix(in srgb, var(--ink) 6%, transparent); }
  #b-sheet section { display: grid; gap: 10px; }
  #b-sheet p { margin: 0; line-height: 1.45; }
  #b-sheet .kicker { margin: 0; font: 500 12px var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--muted); }
  #b-sheet .row { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  #b-sheet .row > button { font-size: 15px; color: var(--muted); }
  #b-sheet .row > button:hover { color: var(--alarm); }
  #b-sheet .pill { justify-self: start; padding: 9px 16px; border-radius: 999px; font-size: 15px; font-weight: 500; color: var(--muted); box-shadow: inset 0 0 0 1.5px var(--line-2); text-decoration: none; }
  #b-sheet .pill:hover { color: var(--ink); box-shadow: inset 0 0 0 1.5px var(--ink); }
  #b-sheet .pill.solid { background: var(--ink); color: var(--top); box-shadow: none; font-weight: 700; }
  #b-sheet .pill.danger:hover { color: var(--alarm); box-shadow: inset 0 0 0 1.5px var(--alarm); }
  #b-sheet .pill.armed { background: var(--alarm); color: #fff; box-shadow: none; }
  #b-sheet .serve-row { display: flex; align-items: baseline; gap: 12px; }
  #b-sheet .serve-row label { font: 500 12px var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--muted); }
  #b-sheet .serve-time { position: relative; font: 700 30px var(--mono); letter-spacing: -.03em; padding: 2px 10px; margin-left: -10px; border-radius: 12px; }
  #b-sheet .serve-time:hover { background: color-mix(in srgb, var(--ink) 6%, transparent); }
  #b-sheet .serve-time.unset { font: 500 15px var(--display); color: var(--muted); box-shadow: inset 0 0 0 1.5px var(--line-2); padding: 8px 16px; margin-left: 0; }
  #b-sheet .serve-time input { position: absolute; inset: 0; opacity: 0; pointer-events: none; }
  #b-sheet .verdict { margin: 0; display: flex; align-items: center; gap: 8px; font-size: 15px; color: var(--muted); }
  #b-sheet .verdict::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  #b-sheet .verdict.behind { color: var(--alarm); }
  #b-sheet .key { display: flex; flex-wrap: wrap; gap: 8px; }
  #b-sheet .key button { display: flex; align-items: center; gap: 8px; max-width: 100%; padding: 7px 12px 7px 10px; border-radius: 999px; font-size: 14px; font-weight: 500; box-shadow: inset 0 0 0 1.5px var(--line-2); }
  #b-sheet .key button[aria-pressed="true"] { box-shadow: inset 0 0 0 2px var(--ink); }
  #b-sheet .key .swatch { flex: none; width: 10px; height: 10px; border-radius: 50%; background: var(--dish); }
  #b-sheet .key .label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 15em; }
  #b-sheet .key .count { color: var(--muted); font: 500 12px var(--mono); }
  #b-sheet .key.filtering button[aria-pressed="false"] { color: var(--muted); }
  #b-sheet .agenda { --time: 4.6em; position: relative; list-style: none; margin: 0; padding: 0; display: grid; }
  #b-sheet .agenda::before { content: ""; position: absolute; left: calc(var(--time) + 14px + 5.25px); top: 22px; bottom: 22px; width: 1.5px; background: var(--line); }
  #b-sheet .agenda li > * { width: 100%; display: grid; grid-template-columns: var(--time) 12px minmax(0, 1fr) auto; column-gap: 14px; align-items: center; text-align: left; }
  #b-sheet .agenda .item button { padding: 9px 4px 9px 0; border-radius: 12px; }
  #b-sheet .agenda .item button:hover { background: color-mix(in srgb, var(--ink) 5%, transparent); }
  #b-sheet .agenda .at { justify-self: end; font: 500 13px var(--mono); color: var(--muted); white-space: nowrap; }
  #b-sheet .agenda .at small { font-size: 11px; margin-left: 2px; }
  #b-sheet .agenda .now-at { color: var(--ink); font-weight: 700; }
  #b-sheet .agenda .dot { position: relative; z-index: 1; width: 12px; height: 12px; border-radius: 50%; background: var(--top); box-shadow: inset 0 0 0 2px var(--dish); }
  #b-sheet .agenda .now .dot, #b-sheet .agenda .cooking .dot { background: var(--dish); }
  #b-sheet .agenda .cooking .dot { box-shadow: 0 0 0 4px color-mix(in srgb, var(--dish) 25%, transparent); }
  #b-sheet .agenda .reminder .dot { border-radius: 2px; transform: rotate(45deg) scale(.8); box-shadow: inset 0 0 0 2px var(--flame); }
  #b-sheet .agenda .what { display: grid; gap: 1px; min-width: 0; }
  #b-sheet .agenda .what b { font-weight: 500; font-size: 16px; line-height: 1.3; }
  #b-sheet .agenda .what span { font-size: 13px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #b-sheet .agenda .what .by { color: var(--ink); font-weight: 500; }
  #b-sheet .agenda .here .dot { width: 8px; height: 8px; margin-left: 2px; background: var(--ink); box-shadow: 0 0 0 4px var(--top); }
  #b-sheet .agenda .here .what b { font-weight: 400; color: var(--muted); }
  #b-sheet .agenda .left { font: 500 13px var(--mono); color: var(--hot); }
  #b-sheet .agenda .gap > div { padding: 6px 0; }
  #b-sheet .agenda .gap span { grid-column: 3; font-size: 13px; color: var(--muted); font-style: italic; }
  #b-sheet .agenda.filtered::before { display: none; }
  #b-sheet .done-count { margin: 0; font-size: 15px; color: var(--muted); }
  #b-sheet .shelf { list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; }
  #b-sheet .stock { display: grid; grid-template-columns: 7em minmax(0, 1fr) 28px; gap: 16px; align-items: center; }
  #b-sheet .stock b { font-size: 16px; font-weight: 400; }
  #b-sheet .stock.out b, #b-sheet .stock.out input { color: var(--alarm); }
  #b-sheet input { font: inherit; font-size: 15px; color: var(--ink); background: none; border: 0; border-radius: 0; padding: 7px 0; min-width: 0; box-shadow: inset 0 -1px 0 transparent; user-select: text; }
  #b-sheet input::placeholder { color: var(--muted); opacity: .7; }
  #b-sheet input:hover { box-shadow: inset 0 -1px 0 var(--line-2); }
  #b-sheet input:focus { outline: none; box-shadow: inset 0 -1.5px 0 var(--ink); }
  #b-sheet .stock input.have { color: var(--muted); font-family: var(--mono); font-size: 13px; }
  #b-sheet .x { font-size: 14px; color: var(--muted); opacity: .5; padding: 4px; }
  #b-sheet .x:hover { color: var(--alarm); opacity: 1; }
  #b-sheet .setup { display: grid; grid-template-columns: 6.5em minmax(0, 1fr); column-gap: 16px; row-gap: 12px; align-items: center; }
  #b-sheet .setup > span { font: 500 12px var(--mono); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
  #b-sheet .stepper, #b-sheet .seg { display: flex; align-items: center; gap: 4px; justify-self: start; flex-wrap: wrap; }
  #b-sheet .stepper b { min-width: 2.2em; text-align: center; font: 500 16px var(--mono); }
  #b-sheet .stepper button, #b-sheet .seg button { padding: 5px 12px; border-radius: 999px; font-size: 15px; color: var(--muted); box-shadow: inset 0 0 0 1.5px var(--line-2); }
  #b-sheet .stepper button:hover, #b-sheet .seg button:hover { color: var(--ink); }
  #b-sheet .stepper button:disabled { opacity: .35; cursor: default; }
  #b-sheet .seg button[aria-pressed="true"] { background: var(--ink); color: var(--top); box-shadow: none; }
  #b-sheet .setup input.wide { justify-self: stretch; }
  #b-sheet .recipe { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: fit-content(40%) minmax(0, 1fr) 24px; column-gap: 16px; row-gap: 2px; }
  #b-sheet .recipe li { display: grid; grid-column: 1 / -1; grid-template-columns: subgrid; align-items: center; font-size: 16px; }
  #b-sheet .recipe .amt { font: 500 13px var(--mono); color: var(--muted); }
  #b-sheet .recipe li.out input { color: var(--alarm); }
  #b-sheet .order { display: grid; gap: 4px; padding-bottom: 18px; border-bottom: 1px solid var(--line); }
  #b-sheet .order:last-of-type { border: 0; }
  #b-sheet .order .actions { display: flex; gap: 8px; margin-top: 8px; }
  #b-sheet .cart-title { font-size: 17px; font-weight: 500; }
  #b-sheet .cart-items { margin: 2px 0 0; font-size: 14px; line-height: 1.45; color: var(--muted); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  #b-sheet .order img { margin-top: 10px; width: 100%; aspect-ratio: 16 / 10; object-fit: cover; object-position: top; border-radius: 10px; }
  #b-sheet .sub-meta { font-size: 14px; color: var(--muted); }
  #b-howto { position: fixed; z-index: 30; inset: 0; display: grid; place-items: center; padding: 24px; background: color-mix(in srgb, var(--bg) 70%, transparent); backdrop-filter: blur(8px); animation: b-fade .25s; font-family: var(--display); color: var(--ink); }
  #b-howto .howto { width: min(1100px, 100%); max-height: 100%; overflow-y: auto; display: grid; gap: 32px; padding: clamp(24px, 4vw, 48px); border-radius: 26px; background: var(--top); box-shadow: 0 0 0 1px var(--line), 0 40px 100px -30px rgb(0 0 0 / .45); animation: b-drawer .35s cubic-bezier(.2,.9,.25,1); }
  #b-howto .howto-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: end; gap: 16px 32px; }
  #b-howto h1 { margin: 0; font-size: clamp(40px, 5vw, 72px); letter-spacing: -.04em; line-height: 1; }
  #b-howto .watch { margin: 8px 0 0; font-size: 19px; }
  #b-howto .watch b { display: block; font: 700 12px var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--alarm); }
  #b-howto .howto-steps { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(var(--cols, 3), minmax(0, 1fr)); gap: 28px 24px; }
  #b-howto .howto-steps li { display: grid; gap: 10px; align-content: start; }
  #b-howto figure { margin: 0; aspect-ratio: 3 / 2; border-radius: 14px; overflow: hidden; background: var(--line); }
  #b-howto figure.loading { animation: b-breathe 1.6s ease-in-out infinite; }
  #b-howto img { width: 100%; height: 100%; object-fit: cover; display: block; }
  #b-howto h3 { margin: 4px 0 0; display: flex; gap: 10px; font-size: 21px; line-height: 1.2; letter-spacing: -.01em; }
  #b-howto .n { flex: none; padding-top: 4px; font: 700 13px var(--mono); color: var(--muted); }
  #b-howto .howto-steps p { margin: 0; font-size: 17px; line-height: 1.4; }
  #b-howto .done { font: 700 20px var(--display); border: 0; cursor: pointer; height: 58px; padding: 0 34px; border-radius: 999px; background: var(--ink); color: var(--bg); }
  @media (max-width: 640px) { #b-howto .howto-steps { grid-template-columns: 1fr !important; } }
  [hidden] { display: none !important; }
`;
function mountChrome() {
  document.head.append(h("style", {}, CORE_CSS));
  document.body.append(
    h("div", { id: "b-toast", role: "status", hidden: true }),
    h("div", { id: "b-veil", hidden: true, onclick: () => openSheet("nothing") }),
    h("aside", { id: "b-sheet", hidden: true }),
    h("div", { id: "b-howto", hidden: true }));
}
function toast(...kids) {
  const t = $("b-toast");
  t.replaceChildren(...kids, h("button", { class: "x", "aria-label": "Dismiss", onclick: () => (t.hidden = true) }, "✕"));
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 8000);
}

// ---------- sheets ----------
let sheetView = null;
function openSheet(view) { sheetView = view === "nothing" ? null : view; renderSheet(); View.render?.(); }
// The ways into everything else, for the page to draw however it likes: [{view, label, flag}]
function navItems() {
  const carts = state?.carts || [];
  const filling = carts.some((o) => o.status === "filling");
  return [
    plan().steps.length > 0 && { view: "plan", label: "Plan", flag: lateBy() > LATE_AFTER },
    carts.length > 0 && { view: "shopping", label: "Shop", flag: filling, busy: filling },
    state && { view: "kitchen", label: "Kitchen", flag: state.shopping.on && state.shopping.signed_in === false },
  ].filter(Boolean);
}
const PLACES = [["fridge", "Fridge"], ["freezer", "Freezer"], ["pantry", "Pantry"], ["spices", "Spices"], ["tools", "Tools"]];
function renderSheet() {
  const sheet = $("b-sheet");
  if (!sheet) return;
  sheet.hidden = $("b-veil").hidden = !sheetView || !state;
  if (sheet.hidden) return;
  const typing = sheet.contains(document.activeElement) && document.activeElement.dataset.key ? document.activeElement : null;
  const title = { plan: "Plan", shopping: "Shopping", kitchen: "Kitchen", ingredients: "Ingredients" }[sheetView];
  let body;
  if (sheetView === "shopping") {
    body = state.carts.length ? [...state.carts.map((o) => h("div", { class: "order" },
      h("div", { class: "row" }, h("span", { class: "cart-title" }, cartTitle(o)),
        h("button", { class: "x", "aria-label": "Remove", onclick: () => act("clear_carts", { store: o.store }) }, o.status === "filling" ? "Stop" : "✕")),
      h("span", { class: "sub-meta" }, cartLine(o)),
      h("p", { class: "cart-items" }, cartItems(o)),
      o.status === "filling" && !state.shopping.viewer && h("img", { alt: "The shopping browser", "data-live": "", src: `/shopper-frame?${Date.now()}`,
        onerror: (e) => { e.target.style.visibility = "hidden"; }, onload: (e) => { e.target.style.visibility = ""; } }),
      o.shot && o.status !== "filling" && h("img", { src: `/cart-shots/${o.shot}`, alt: "The cart" }),
      h("div", { class: "actions" }, ...cartActions(o, "pill solid")))),
      state.carts.some((o) => o.status !== "filling") && h("button", { class: "pill danger", onclick: () => act("clear_carts") }, "Clear")] : [];
  } else if (sheetView === "plan") {
    body = runSheet();
  } else if (sheetView === "ingredients") {
    body = dishes().map((d) => h("section", {}, h("p", { class: "kicker" }, d), ingredientList(d)));
  } else {
    const { profile: p, inventory } = state.kitchen;
    const cooking = dishes();
    const shelves = PLACES.map(([where, name]) => {
      const items = Object.entries(inventory).filter(([, e]) => e.where === where);
      return h("section", {}, h("p", { class: "kicker" }, name), h("ul", { class: "shelf" },
        items.map(([item, e]) => h("li", { class: `stock${none(e.have) ? " out" : ""}` },
          field({ class: "have", key: `have:${item}`, value: e.have, label: `How much ${item}` },
            (have) => have && have !== e.have && act("kitchen", { items: [{ name: item, have, where }] })),
          h("b", {}, item),
          h("button", { class: "x", "aria-label": `Remove ${item}`, onclick: () => act("forget_item", { name: item }) }, "✕"))),
        addRow(where, name)));
    });
    body = [
      state.shopping.on && state.shopping.signed_in === false && h("section", {}, h("div", { class: "row" }, h("p", {}, "Instacart"), shopperLink("Sign in", "pill solid"))),
      cooking.length > 0 && h("section", {}, h("p", { class: "kicker" }, "Cooking"), ...cooking.map((dish) =>
        h("div", { class: "row" }, h("p", {}, dish), h("button", { onclick: () => act("clear_dish", { dish }) }, "Stop")))),
      cooking.length > 0 && h("button", { class: "pill", onclick: () => openSheet("ingredients") }, "Ingredients"),
      h("section", {}, h("p", { class: "kicker" }, "Setup"), setupGrid(p)),
      ...shelves,
      (state.steps.length > 0 || state.timers.length > 0 || basilText || youText) && clearAll(),
    ];
  }
  const scroll = sheet.scrollTop;
  sheet.replaceChildren(h("div", { class: "top" }, h("h2", {}, title), h("button", { "aria-label": "Close", onclick: () => openSheet("nothing") }, "✕")),
    ...[body].flat().filter(Boolean));
  sheet.scrollTop = scroll;
  tick();
  const again = typing && sheet.querySelector(`[data-key="${CSS.escape(typing.dataset.key)}"]`);
  if (again) { again.value = typing.value; again.focus(); try { again.setSelectionRange(typing.selectionStart, typing.selectionEnd); } catch {} }
}
function field({ class: cls, key, value = "", label, placeholder }, save) {
  const input = h("input", { class: cls, "data-key": key, value, "aria-label": label, placeholder, autocomplete: "off", spellcheck: "false" });
  input.addEventListener("blur", () => { if (input.isConnected && input.value.trim() !== value) save(input.value.trim()); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); if (e.key === "Escape") { input.value = value; input.blur(); } });
  return input;
}
function addRow(where, shelf) {
  const what = h("input", { class: "what", "data-key": `add:${where}`, placeholder: `Add to ${shelf.toLowerCase()}`, "aria-label": `Add to ${shelf}`, autocomplete: "off" });
  const have = h("input", { class: "have", "data-key": `addhave:${where}`, placeholder: "how much", "aria-label": "How much", autocomplete: "off" });
  const add = () => {
    const name = what.value.trim().toLowerCase();
    if (!name) return;
    act("kitchen", { items: [{ name, have: have.value.trim() || (where === "tools" ? "yes" : "some"), where }] });
    what.value = have.value = "";
    what.focus();
  };
  for (const input of [what, have]) input.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
  return h("li", { class: "stock add" }, have, what, h("span"));
}
function setupGrid(p) {
  const stepper = (key, label, min) => {
    const v = p[key];
    const set = (n) => act("kitchen", { [key]: n });
    return [h("span", {}, label), h("div", { class: "stepper" },
      h("button", { "aria-label": `Fewer ${label.toLowerCase()}`, disabled: v == null || v <= min, onclick: () => set(v - 1) }, "−"),
      h("b", { title: v == null ? "Not told yet" : false }, v ?? "?"),
      h("button", { "aria-label": `More ${label.toLowerCase()}`, onclick: () => set((v ?? min) + 1) }, "+"))];
  };
  return h("div", { class: "setup" },
    stepper("burners", "Burners", 0), stepper("ovens", "Ovens", 0), stepper("cooks", "Cooks", 1),
    (p.cooks ?? 1) > 1 && [h("span", {}, "Who"), field({ class: "wide", key: "cooks", value: (p.cook_names || []).join(", "), label: "Who's cooking", placeholder: "You, Sam" },
      (names) => act("kitchen", { cook_names: names.split(",").map((n) => n.trim()).filter(Boolean) }))],
    h("span", {}, "Skill"), h("div", { class: "seg" }, ["beginner", "intermediate", "advanced"].map((skill) =>
      h("button", { "aria-pressed": String(p.skill === skill), onclick: () => act("kitchen", { skill }) }, words(skill)))),
    h("span", {}, "Basil"), h("div", { class: "seg" },
      h("button", { "aria-pressed": String(alwaysOn), onclick: () => setListen("always") }, "Always on"),
      h("button", { "aria-pressed": String(!alwaysOn && !pushToTalk), onclick: () => setListen("tap") }, "Tap"),
      h("button", { "aria-pressed": String(pushToTalk), title: "Hold to talk, or hold Space", onclick: () => setListen("hold") }, "Hold")),
    h("span", {}, "Store"), field({ class: "wide", key: "store", value: p.store || "", label: "Instacart store", placeholder: "Your Instacart store" },
      (store) => store && act("kitchen", { store })),
    h("span", {}, "Diet"), field({ class: "wide", key: "diet", value: p.dietary_notes, label: "Diet", placeholder: "Allergies, likes, dislikes" },
      (dietary_notes) => dietary_notes !== p.dietary_notes && act("kitchen", { dietary_notes })));
}
function clearAll() {
  const button = h("button", { class: "pill danger" }, "Clear all");
  let armed = null;
  button.onclick = () => {
    if (!armed) {
      button.textContent = "Clear everything"; button.classList.add("armed");
      armed = setTimeout(() => { armed = null; button.textContent = "Clear all"; button.classList.remove("armed"); }, 3000);
      return;
    }
    clearTimeout(armed);
    act("clear_all");
    said("you", "", false); said("basil", "", false); rang.clear(); viewing = null;
    openSheet("nothing");
  };
  return button;
}
function ingredientList(dish) {
  const all = state.recipes[dish] || [];
  const change = (extra) => act("ingredients", { dish, ...extra });
  const missing = all.filter((i) => none(i.have));
  const amount = h("input", { class: "amt", "data-key": `add-amt:${dish}`, placeholder: "how much", "aria-label": "How much", autocomplete: "off" });
  const name = h("input", { "data-key": `add-name:${dish}`, placeholder: "Add", "aria-label": "Ingredient", autocomplete: "off" });
  const add = () => { if (!name.value.trim()) return; change({ items: [{ name: name.value.trim(), amount: amount.value.trim() }] }); amount.value = name.value = ""; amount.focus(); };
  for (const input of [amount, name]) input.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
  return [h("ul", { class: "recipe" },
    all.map((i) => h("li", { class: none(i.have) ? "out" : false },
      field({ class: "amt", key: `amt:${dish}:${i.name}`, value: i.amount, label: `How much ${i.name}`, placeholder: "—" }, (value) => change({ items: [{ name: i.name, amount: value }] })),
      field({ key: `name:${dish}:${i.name}`, value: i.name, label: "Ingredient" }, (value) => change(value ? { remove: [i.name], items: [{ name: value, amount: i.amount }] } : { remove: [i.name] })),
      h("button", { class: "x", "aria-label": `Remove ${i.name}`, onclick: () => change({ remove: [i.name] }) }, "✕"))),
    h("li", {}, amount, name, h("span"))),
    missing.length > 0 && state.shopping.on && h("button", { class: "pill solid", onclick: () => act("buy_missing", { dish }) }, "Buy what's missing")];
}
// What a step uses, with amounts, for showing beside it: [{name, amount, out}]
function stepUses(st) {
  return (st.uses || []).map((name) => {
    const i = (state.recipes[st.dish] || []).find((x) => x.name === name);
    return { name, amount: i?.amount || "", out: !!i && none(i.have) };
  });
}

// ---------- the run sheet ----------
let planDish = null;
const FREE_GAP_MS = 20 * 60000;
function runSheet() {
  const { steps, open } = plan();
  const ds = dishes();
  if (!ds.includes(planDish)) planDish = null;
  const toTime = (ms) => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
  const input = h("input", { type: "time", tabindex: -1, "aria-hidden": "true", value: state.serve_at ? toTime(state.serve_at * 1000) : "" });
  input.addEventListener("change", () => act("serve", { at: input.value || null }));
  const serve = h("button", { class: `serve-time${state.serve_at ? "" : " unset"}`, id: "serve-pick", onclick: () => { try { input.showPicker(); } catch { input.focus(); } } },
    state.serve_at ? atClock(state.serve_at * 1000) : "Set a time", input);
  const late = lateBy();
  const verdict = !open.length ? "" : !state.serve_at ? `Ready ${atClock(readyAt())}` : late > LATE_AFTER ? `${hours(late)} late · ready ${atClock(readyAt())}` : "On time";
  const key = ds.length > 1 && h("div", { class: `key${planDish ? " filtering" : ""}`, role: "group", "aria-label": "Show one dish" },
    ds.map((d) => {
      const mine = steps.filter((x) => x.dish === d), done = mine.filter((x) => x.status === "done").length;
      return h("button", { "aria-pressed": String(planDish === d), title: d, style: `--dish: ${dishColor(d)}`, onclick: () => { planDish = planDish === d ? null : d; renderSheet(); } },
        h("span", { class: "swatch" }), h("span", { class: "label" }, d), h("span", { class: "count" }, `${done}/${mine.length}`));
    }));
  const items = [
    ...open.filter((x) => !planDish || x.dish === planDish).map((x) => ({ s: x, at: x.status === "in_progress" || x.start === 0 ? 0 : stateAt + x.start * 60000, end: stateAt + (x.end ?? 0) * 60000 })),
    ...(planDish ? [] : (state.timers || []).filter((t) => t.kind === "reminder")).map((t) => ({ r: t, at: stateAt + t.seconds_left * 1000, end: stateAt + t.seconds_left * 1000 })),
  ].sort((a, b) => a.at - b.at || a.end - b.end);
  const rows = [];
  const first = items[0];
  if (first && first.at > 0) rows.push(h("li", { class: "item here" }, h("div", {},
    h("span", { class: "at" }, h("span", { class: "now-at" }, "Now")), h("span", { class: "dot" }),
    h("span", { class: "what" }, h("b", {}, "Nothing yet"), h("span", { "data-until": first.at, "data-in": "" }, `in ${hours((first.at - Date.now()) / 60000)}`)), h("span"))));
  let lastAt = null, lastMeridiem = null, lastDish = null, busyUntil = Date.now();
  for (const item of items) {
    const start = item.at || Date.now();
    if (!planDish && start - busyUntil >= FREE_GAP_MS) rows.push(h("li", { class: "gap" }, h("div", {}, h("span"), h("span"), h("span", {}, `${hours((start - busyUntil) / 60000)} free`))));
    if (item.s?.hands_on) busyUntil = Math.max(busyUntil, item.end); else busyUntil = Math.max(busyUntil, start);
    let at = "";
    if (item.at === 0) at = lastAt === 0 ? "" : h("span", { class: "now-at" }, "Now");
    else if (lastAt !== atClock(item.at)) {
      const [time, meridiem] = atClock(item.at).split(" ");
      at = [time, meridiem !== lastMeridiem && h("small", {}, meridiem)];
      lastMeridiem = meridiem;
    }
    lastAt = item.at === 0 ? 0 : atClock(item.at);
    rows.push(agendaRow(item, at, item.s && item.s.dish !== lastDish));
    if (item.s) lastDish = item.s.dish;
  }
  return [
    h("section", {}, h("div", { class: "serve-row" }, h("label", { for: "serve-pick" }, "Eat at"), serve),
      verdict && h("p", { class: `verdict${late > LATE_AFTER ? " behind" : ""}` }, verdict)),
    key,
    rows.length ? h("ol", { class: `agenda${planDish ? " filtered" : ""}` }, rows) : h("p", { class: "done-count" }, "All done."),
  ];
}
function agendaRow({ s: st, r }, at, newDish) {
  if (r) return h("li", { class: "item reminder" }, h("div", {}, h("span", { class: "at" }, at), h("span", { class: "dot" }), h("span", { class: "what" }, h("b", {}, r.says), h("span", {}, "Reminder")), h("span")));
  const going = st.status === "in_progress";
  const kind = going && !st.hands_on ? "cooking" : going || st.start === 0 ? "now" : "later";
  const length = !st.hands_on && st.minutes >= 30 ? `until ${atClock(stateAt + st.end * 60000)}` : hours(st.minutes);
  return h("li", { class: ["item", kind].join(" "), style: `--dish: ${dishColor(st.dish)}` },
    h("button", { onclick: () => { viewing = st.id; openSheet("nothing"); render(); } },
      h("span", { class: "at" }, at), h("span", { class: "dot" }),
      h("span", { class: "what" }, h("b", {}, st.title),
        h("span", {}, cookName(st) && h("span", { class: "by" }, cookName(st)), [cookName(st) && "", length, !planDish && newDish && st.dish].filter((x) => x !== false && x != null).join(" · "))),
      h("span", {}, kind === "cooking" && h("span", { class: "left", "data-until": stateAt + st.end * 60000, "data-clock": "" }, clock(st.end * 60)))));
}

// ---------- how-to ----------
const pictures = new Map();
function picture(st) {
  const key = `/steppic?${new URLSearchParams({ dish: st.dish, title: st.title, text: st.text })}`;
  if (!pictures.has(key)) pictures.set(key, fetch(key).then((r) => r.json()).catch(() => ({ url: null })));
  return pictures.get(key);
}
function renderHowTo() {
  const how = state?.how_to;
  const box = $("b-howto");
  if (!box) return;
  box.hidden = !how;
  if (!how) { box.replaceChildren(); box.dataset.topic = ""; return; }
  if (box.dataset.topic === how.topic) return;
  box.dataset.topic = how.topic;
  const fig = (st) => {
    const f = h("figure", { class: "loading" });
    picture(st).then(({ url }) => { if (!url) return f.remove(); const img = h("img", { src: url, alt: "" }); img.onload = () => { f.classList.remove("loading"); f.replaceChildren(img); }; });
    return f;
  };
  box.replaceChildren(h("section", { class: "howto" },
    h("div", { class: "howto-head" },
      h("div", {}, h("h1", {}, how.topic), how.watch_out && h("p", { class: "watch" }, h("b", {}, "Watch out"), how.watch_out)),
      h("button", { class: "done", onclick: () => { sound.done(); act("close_how"); } }, "Done")),
    h("ol", { class: "howto-steps", style: `--cols: ${how.steps.length === 4 ? 4 : Math.min(how.steps.length, 3)}` }, how.steps.map((st, i) => h("li", {},
      fig({ dish: how.topic, title: st.title, text: st.text }),
      h("h3", {}, h("span", { class: "n" }, String(i + 1).padStart(2, "0")), st.title),
      h("p", {}, st.text))))));
}

// ---------- timers ----------
function ring(label) {
  const step_id = (state?.timers || []).find((t) => t.label === label)?.step_id || null;
  rang.set(label, { at: Date.now(), step_id });
  sound.ring();
  render();
}
// Without Basil, the server keeps time but rings nothing: a timer that runs out on screen rings here.
const rungHere = new Set();
function ringDueTimers(now) {
  for (const t of state?.timers || []) {
    if (t.kind === "reminder" || t.paused || t.follows) continue;
    if (stateAt + t.seconds_left * 1000 > now) { rungHere.delete(t.label); continue; }
    if (!ws && !rungHere.has(t.label) && !rang.has(t.label)) { rungHere.add(t.label); ring(t.label); }
  }
}

// ---------- keys ----------
document.addEventListener("keydown", (e) => {
  if (e.target.matches?.("input, textarea")) return;
  if (e.key === "Escape") { if (sheetView) openSheet("nothing"); else if (viewing) backToNow(); }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z" && !e.shiftKey) { e.preventDefault(); act("undo"); sound.tap(); }
  if (e.key === "Enter" && !sheetView && !state?.how_to && !(e.target instanceof HTMLButtonElement || e.target instanceof HTMLAnchorElement)) {
    const o = objectives()[0], a = o && objectiveAction(o);
    if (a) { e.preventDefault(); View.pressed?.(o); a.run(); }
  }
  if (!sheetView && e.key === "ArrowRight") look(1);
  if (!sheetView && e.key === "ArrowLeft") look(-1);
  if (pushToTalk && e.code === "Space" && !e.repeat) { e.preventDefault(); hold(true); if (!ws) wake(); }
});
document.addEventListener("keyup", (e) => { if (pushToTalk && e.code === "Space" && holding) { e.preventDefault(); hold(false); } });

// ---------- render & tick ----------
function render() {
  if (!state) return;
  renderNotice();
  View.render?.();
  renderHowTo();
  if (sheetView) renderSheet();
}
// Countdowns tick in place, so nothing is rebuilt under a finger.
function tick() {
  const now = Date.now();
  ringDueTimers(now);
  if (now % 2000 < 1000) for (const el of document.querySelectorAll("img[data-live]")) el.src = `/shopper-frame?${now}`;
  for (const el of document.querySelectorAll("[data-until]")) {
    const sec = (el.dataset.until - now) / 1000;
    if ("in" in el.dataset) el.textContent = `in ${hours(sec / 60)}`;
    else if ("short" in el.dataset) el.textContent = short(sec / 60);
    else if ("clock" in el.dataset) el.textContent = clock(sec);
  }
  View.tick?.(now);
}

// Start once the page's DOM is ready.
function startBasil() {
  mountChrome();
  connectScreen();
  setInterval(tick, 1000);
  pilot("asleep");
  if (stayConnected()) wake(true);
}
