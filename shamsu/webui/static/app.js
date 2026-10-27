/* SHAMSU web portal. No framework, no build step, no CDN - this file is the
 * whole client.
 *
 * Two rules worth naming, because both are easy to get wrong and both were got
 * wrong somewhere before:
 *
 * 1. STICKY BOTTOM. Auto-scroll only when the view is already at the bottom.
 *    If you have scrolled up to read something, an arriving line must not yank
 *    you back down; it increments a "N new" button instead. During a long turn
 *    output is near-continuous, which is exactly when holding your place
 *    matters most. SmallCTL's TUI snapped to bottom on every append and made
 *    its own scrollback useless.
 *
 * 2. NEVER innerHTML. Every string here is somebody's file path, prompt or
 *    model output. `textContent` throughout, so a transcript containing markup
 *    is text, not markup.
 */

const BOTTOM_SLACK_PX = 48;
const QUEUE_POLL_MS = 2500;
const APPROVAL_POLL_MS = 2000;

const state = {
  token: "",
  workspace: null,
  session: null,
  base: "",
  stream: null,
  pendingNew: 0,
  turnId: "",
  running: false,
  seenApprovals: new Set(),
  commands: [],
  paletteIndex: 0,
  telegram: null,
  selectionEpoch: 0,
  pendingPrompts: [],
  sending: false,
  drafts: new Map(),
  runtime: null,
};

const el = (id) => document.getElementById(id);

/* --- transport ---------------------------------------------------------- */

function resolveToken() {
  const fromUrl = new URLSearchParams(location.search).get("t");
  if (fromUrl) {
    sessionStorage.setItem("shamsu-token", fromUrl);
    // Out of the address bar, the history entry and any Referer.
    history.replaceState({}, "", location.pathname);
    return fromUrl;
  }
  return sessionStorage.getItem("shamsu-token") || "";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-Shamsu-Token": state.token,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(payload.error || `HTTP ${response.status}`); error.status = response.status; throw error; }
  return payload;
}

const post = (path, body) =>
  api(path, { method: "POST", body: JSON.stringify(body || {}) });

/* --- sidebar ------------------------------------------------------------ *
 *
 * Collapsed by default and loaded lazily. With 126 threads in one project,
 * fetching every session for every workspace at boot meant a wall of names
 * nobody asked for and one request that dominated startup. A group fetches its
 * threads the first time it is opened, and not before.
 */

const THREADS_SHOWN = 25;
const OPEN_KEY = "shamsu-open-workspaces";

function openGroups() {
  try {
    return new Set(JSON.parse(localStorage.getItem(OPEN_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function rememberGroups(open) {
  try {
    localStorage.setItem(OPEN_KEY, JSON.stringify([...open]));
  } catch {
    /* private mode, or a full quota. The sidebar still works, it just forgets. */
  }
}

function rememberOpenWorkspace(workspaceId) {
  const open = openGroups();
  open.add(workspaceId);
  rememberGroups(open);
}

async function loadWorkspaces() {
  const { workspaces } = await api("/api/workspaces");
  state.workspaces = workspaces;
  if (state.workspace) {
    state.workspace =
      workspaces.find((workspace) => workspace.id === state.workspace.id) || state.workspace;
  }
  const tree = el("tree");
  tree.replaceChildren();

  if (!workspaces.length) {
    const empty = document.createElement("p");
    empty.className = "ws-empty";
    empty.textContent = "No projects yet. Use Add a project to choose a folder.";
    tree.append(empty);
    return;
  }

  const open = openGroups();
  // Nothing remembered yet: open the one you worked in last, which is now
  // first. Opening all of them would be the wall this replaces.
  const isFirstVisit = open.size === 0;

  workspaces.forEach((workspace, index) => {
    const group = document.createElement("section");
    group.className = "group";
    group.dataset.workspaceId = workspace.id;

    const header = document.createElement("button");
    header.type = "button";
    header.className = "ws";
    header.dataset.workspaceId = workspace.id;
    const chevron = document.createElement("span");
    chevron.className = "chev";
    chevron.textContent = "\u203a";
    const name = document.createElement("span");
    name.className = "ws-name";
    name.textContent = workspace.name;
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = String(workspace.session_count);
    header.append(chevron, name, count);
    header.title = workspace.path;

    const body = document.createElement("div");
    body.className = "group-body";

    const shouldOpen = isFirstVisit ? index === 0 : open.has(workspace.id);
    setGroupOpen(group, shouldOpen);
    if (shouldOpen) loadGroup(workspace, body);

    header.addEventListener("click", () => {
      selectWorkspace(workspace);
      const nowOpen = !group.classList.contains("open");
      setGroupOpen(group, nowOpen);
      const remembered = openGroups();
      if (nowOpen) {
        remembered.add(workspace.id);
        if (!body.dataset.loaded) loadGroup(workspace, body);
      } else {
        remembered.delete(workspace.id);
      }
      rememberGroups(remembered);
    });

    group.append(header, body);
    tree.append(group);
  });
  markActiveWorkspace();
}

function setGroupOpen(group, open) {
  group.classList.toggle("open", open);
  group.querySelector(".group-body")?.toggleAttribute("hidden", !open);
}

async function loadGroup(workspace, body) {
  body.dataset.loaded = "1";
  const loading = document.createElement("p");
  loading.className = "ws-empty";
  loading.textContent = "Loading\u2026";
  body.replaceChildren(loading);

  let sessions = [];
  try {
    ({ sessions } = await api(`/api/workspaces/${workspace.id}/sessions`));
  } catch (error) {
    loading.textContent = `Could not read: ${error.message}`;
    return;
  }
  body.replaceChildren();
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "ws-empty";
    empty.textContent = "No threads here yet.";
    body.append(empty);
    return;
  }
  renderThreads(body, sessions, workspace, THREADS_SHOWN);
  applyFilter();
}

function renderThreads(body, sessions, workspace, limit) {
  body.replaceChildren();
  for (const session of sessions.slice(0, limit)) {
    body.append(threadButton(session, workspace));
  }
  if (sessions.length > limit) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "more";
    more.textContent = `Show all ${sessions.length}`;
    more.addEventListener("click", () => {
      renderThreads(body, sessions, workspace, sessions.length);
      applyFilter();
    });
    body.append(more);
  }
}

function threadButton(session, workspace) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "thread";
  button.dataset.sessionId = session.session_id;
  button.dataset.search =
    `${session.title} ${session.last_user_prompt} ${workspace.name}`.toLowerCase();

  const label = document.createElement("span");
  label.className = "label";
  label.textContent = session.title || session.session_id;
  const when = document.createElement("span");
  when.className = "when";
  when.textContent = relativeTime(session.updated_at);

  button.append(label, when);
  button.title = session.last_user_prompt || session.title || "";
  button.addEventListener("click", () => openSession(session, workspace));
  return button;
}

function relativeTime(stamp) {
  if (!stamp) return "";
  const then = Date.parse(stamp);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 90) return "now";
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  if (hours < 24) return `${Math.round(hours)}h`;
  const days = hours / 24;
  if (days < 7) return `${Math.round(days)}d`;
  return new Date(then).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function applyFilter() {
  const needle = el("filter").value.trim().toLowerCase();
  for (const group of document.querySelectorAll(".group")) {
    let visible = 0;
    for (const button of group.querySelectorAll(".thread")) {
      const hidden = Boolean(needle) && !button.dataset.search.includes(needle);
      button.hidden = hidden;
      if (!hidden) visible += 1;
    }
    // While filtering, a group with nothing matching is noise - but an
    // unopened group has nothing to match yet, so it stays.
    const body = group.querySelector(".group-body");
    const loaded = Boolean(body?.dataset.loaded);
    group.hidden = Boolean(needle) && loaded && visible === 0;
  }
}

async function openWorkspace(workspace) {
  selectWorkspace(workspace);
  const group = document.querySelector(`.group[data-workspace-id="${workspace.id}"]`);
  if (!group) return;
  const body = group.querySelector(".group-body");
  setGroupOpen(group, true);
  if (body && !body.dataset.loaded) await loadGroup(workspace, body);
}

function selectWorkspace(workspace, updateHistory = true) {
  beginSelection();
  state.workspace = workspace;
  state.session = null;
  state.base = "";
  rememberOpenWorkspace(workspace.id);
  markActiveWorkspace();
  el("thread-title").textContent = workspace.name;
  el("thread-sub").textContent = workspace.path;
  el("composer-scope").textContent = `Working in ${workspace.name}`;
  el("prompt").value = "";
  el("prompt").disabled = false;
  el("send").disabled = false;
  el("new-thread").hidden = false;
  el("conversation").replaceChildren(workspaceEmpty(workspace));
  if (updateHistory) rememberLocation(workspace.id, "");
}

function markActiveWorkspace() {
  for (const header of document.querySelectorAll(".ws")) {
    header.setAttribute(
      "aria-current",
      String(Boolean(state.workspace) && header.dataset.workspaceId === state.workspace.id),
    );
  }
}

function workspaceEmpty(workspace) {
  const empty = document.createElement("div");
  empty.className = "welcome";
  const title = document.createElement("h2");
  title.textContent = `Let's work on ${workspace.name}`;
  const description = document.createElement("p");
  description.textContent = "Start a conversation with a task, an error, or a question about this project.";
  const action = document.createElement("button");
  action.type = "button";
  action.className = "btn primary";
  action.textContent = "New conversation";
  action.addEventListener("click", () => newThread(""));
  empty.append(title, description, action);
  return empty;
}

/* --- opening a thread --------------------------------------------------- */

async function openSession(session, workspace, updateHistory = true) {
  const startingDraft = !state.base ? el("prompt").value : "";
  const epoch = beginSelection();
  state.session = session;
  state.workspace = workspace;
  const base = `/api/workspaces/${workspace.id}/sessions/${session.session_id}`;
  state.base = base;
  for (const button of document.querySelectorAll(".thread")) {
    button.setAttribute("aria-current", String(button.dataset.sessionId === session.session_id));
  }
  markActiveWorkspace();
  el("thread-title").textContent = session.title || "New conversation";
  el("thread-sub").textContent = workspace.path;
  el("composer-scope").textContent = `Working in ${workspace.name}`;
  el("prompt").disabled = false;
  el("send").disabled = false;
  el("prompt").value = state.drafts.get(base) || startingDraft;
  el("new-thread").hidden = false;
  el("conversation").replaceChildren();
  const loading = document.createElement("p");
  loading.className = "ws-empty";
  loading.textContent = "Loading conversation";
  el("conversation").append(loading);
  if (updateHistory) rememberLocation(workspace.id, session.session_id);
  try {
    const { messages, stream_cursor } = await api(`${base}/messages`);
    if (epoch !== state.selectionEpoch || base !== state.base) return;
    renderMessages(messages);
    state.streamCursor = stream_cursor;
    scrollToBottom(true);
    subscribe();
    refreshQueue();
    closeMobileRail();
    el("prompt").focus();
  } catch (error) {
    if (epoch !== state.selectionEpoch) return;
    loading.textContent = `Could not load this conversation: ${error.message}`;
    const retry = document.createElement("button");
    retry.className = "btn"; retry.type = "button"; retry.textContent = "Try again";
    retry.addEventListener("click", () => openSession(session, workspace, false));
    el("conversation").append(retry);
  }
}

function renderMessages(messages) {
  const pane = el("conversation");
  pane.replaceChildren();
  // The server already decided what counts as conversation - see
  // `api.session_messages`. It builds this list from the turn stream, the same
  // record the terminal and the phone render, rather than from the model's
  // context file. Filtering again here would be a second opinion about which
  // messages are real, and the two would drift.
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "welcome";
    empty.textContent = "Nothing here yet. Say something below.";
    pane.append(empty);
    return;
  }
  for (const message of messages) {
    pane.append(bubble(message.role, message.content, message));
  }
}

function bubble(role, content, meta) {
  const info = typeof meta === "string" ? { source: meta } : meta || {};
  const block = document.createElement("article");
  block.className = `msg ${role}`;
  if (info.turn_id) block.dataset.turnId = info.turn_id;
  if (info.pending) block.dataset.pending = "true";
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = role === "user" ? "you" : "shamsu";
  block.append(who);

  const text = content || "";
  if (text) {
    const body = document.createElement("div");
    body.className = "body";
    if (role === "assistant") renderMarkdown(body, text);
    else body.textContent = text;
    block.append(body);
  } else if (info.verdict) {
    // A turn that ended without an answer - stopped, failed, cancelled. Shown
    // as its verdict rather than skipped, so a thread never looks like it
    // swallowed your question.
    const body = document.createElement("div");
    body.className = "body quiet";
    body.textContent = info.verdict;
    block.append(body);
  }

  const marks = [];
  // Badged only when the prompt came from somewhere OTHER than this browser.
  // A thread is driven from three places, and the question a web reader
  // actually has is "did I send this, or did it arrive from my phone?" -
  // tagging every one of your own messages "web" would answer a question
  // nobody asked, on every line.
  if (role === "user" && info.source && info.source !== "web") marks.push(info.source);
  if (role === "assistant" && text && info.verdict) marks.push(info.verdict);
  if (marks.length) {
    const tag = document.createElement("span");
    tag.className = "from";
    tag.textContent = marks.join(" / ");
    block.append(tag);
  }
  const steps = Array.isArray(info.steps) ? info.steps : [];
  if (steps.length) block.append(stepList(steps));
  return block;
}

// What the turn actually did, folded away.
//
// Collapsed by default and attached to its own answer, which is the whole
// design: the first attempt at showing activity in this pane put every event
// in the thread as a top-level row, so a finished conversation was followed by
// the entire log again - every read_file, every "context is filling". A turn's
// working belongs INSIDE that turn, and closed until asked for.
function stepList(steps) {
  const box = document.createElement("details");
  box.className = "steps";
  const head = document.createElement("summary");
  const tools = steps.filter((step) => step.kind === "tool");
  const failed = tools.filter((step) => !step.ok).length;
  head.textContent = failed
    ? `${tools.length} tool call${tools.length === 1 ? "" : "s"}, ${failed} failed`
    : `${tools.length} tool call${tools.length === 1 ? "" : "s"}`;
  box.append(head);

  const list = document.createElement("ol");
  list.className = "step-list";
  for (const step of steps) {
    const row = document.createElement("li");
    if (step.kind === "tool") {
      row.className = step.ok ? "step tool" : "step tool failed";
      const name = document.createElement("span");
      name.className = "step-tool";
      name.textContent = step.tool || "tool";
      row.append(name);
      if (step.target) {
        const target = document.createElement("span");
        target.className = "step-target";
        target.textContent = step.target;
        row.append(target);
      }
      if (step.ms) {
        const took = document.createElement("span");
        took.className = "step-ms";
        took.textContent = step.ms >= 1000 ? `${(step.ms / 1000).toFixed(1)}s` : `${step.ms}ms`;
        row.append(took);
      }
      if (!step.ok && step.detail) {
        const why = document.createElement("span");
        why.className = "step-why";
        why.textContent = step.detail;
        row.append(why);
      }
    } else {
      row.className = "step note";
      row.textContent = step.text || "";
    }
    if (step.diff) {
      const diff = document.createElement("pre");
      diff.className = "tool-preview";
      diff.textContent = step.diff;
      row.append(diff);
    }
    list.append(row);
  }
  box.append(list);
  return box;
}

/* --- the live turn ------------------------------------------------------ */

function subscribe() {
  if (state.stream) state.stream.close();
  const base = state.base;
  const epoch = state.selectionEpoch;
  const cursor = Number.isSafeInteger(state.streamCursor) ? `&after=${state.streamCursor}` : "";
  const stream = new EventSource(`${base}/stream?t=${encodeURIComponent(state.token)}${cursor}`);
  state.stream = stream;
  stream.onopen = () => {
    if (epoch === state.selectionEpoch) setConnection("Service connected");
  };
  stream.onmessage = (event) => {
    if (epoch !== state.selectionEpoch || base !== state.base) return;
    try { handleEvent(JSON.parse(event.data)); } catch { /* A malformed event cannot switch sessions. */ }
  };
  stream.onerror = () => {
    if (epoch === state.selectionEpoch) setConnection("Reconnecting updates", true);
  };
}

function handleEvent(event) {
  if (event.kind === "tool.call" || event.kind === "status" || event.kind === "turn.end") el("run-activity").textContent = event.text || "Waiting for the next action";
  if (event.turn_id && event.turn_id !== state.turnId) {
    state.turnId = event.turn_id;
    dropWelcome();
  }
  const wasAtBottom = isAtBottom();

  switch (event.kind) {
    case "turn.start":
      setRunning(true);
      if (event.text) {
        const pending = state.pendingPrompts.find((item) => item.base === state.base && item.text === event.text && !item.echoed);
        const existing = event.turn_id && [...document.querySelectorAll(".msg.user")].some((node) => node.dataset.turnId === event.turn_id);
        if (pending) {
          pending.echoed = true;
          pending.node.classList.remove("pending");
          pending.node.dataset.turnId = event.turn_id || "";
        } else if (!existing) el("conversation").append(bubble("user", event.text, { source: event.source, turn_id: event.turn_id }));
      }
      openTurnCard();
      break;
    case "activity":
      addLine(event.text, "");
      break;
    case "tool.call":
      addLine(event.text, "tool");
      break;
    case "error":
      addLine(event.text, "err");
      break;
    case "tool.result": {
      if (event.data?.diff) {
        const details = document.createElement("details"); details.className = "steps";
        const summary = document.createElement("summary"); summary.textContent = `Review changes to ${event.data.target || event.data.tool}`;
        const preview = document.createElement("pre"); preview.className = "tool-preview"; preview.textContent = event.data.diff;
        details.append(summary, preview); liveTurn().append(details);
      }
      break;
    }
    case "status":
      setFooter(event.text, false, event.data);
      break;
    case "assistant":
      if (event.text) {
        const pending = [...document.querySelectorAll('.msg.assistant[data-pending="true"]')].find((node) => node.dataset.turnId === event.turn_id);
        const answer = bubble("assistant", event.text, { turn_id: event.turn_id });
        if (pending) pending.replaceWith(answer);
        else el("conversation").append(answer);
      }
      break;
    case "turn.end":
      setRunning(false);
      setFooter(event.text, true, event.data);
      refreshQueue();
      state.pendingPrompts = state.pendingPrompts.filter((item) => !item.echoed);
      break;
    default:
      break;
  }

  if (wasAtBottom) {
    scrollToBottom(false);
  } else {
    state.pendingNew += 1;
    updateJump();
  }
}

function dropWelcome() {
  el("conversation").querySelector(".welcome")?.remove();
}

function setRunning(running) {
  state.running = running;
  const pill = el("run-pill");
  pill.hidden = !state.session;
  pill.textContent = running ? "Working" : "Idle";
  pill.classList.toggle("running", running);
}

function openTurnCard() {
  dropWelcome();
  const turn = document.createElement("section");
  turn.className = "turn";
  turn.id = "live-turn";
  const log = document.createElement("div");
  log.className = "log";
  const foot = document.createElement("div");
  foot.className = "foot";
  turn.append(log, foot);
  el("conversation").append(turn);
}

function liveTurn() {
  if (!document.getElementById("live-turn")) openTurnCard();
  return document.getElementById("live-turn");
}

function addLine(value, variant) {
  if (!value) return;
  const line = document.createElement("span");
  line.className = `line ${variant}`.trim();
  line.textContent = value;
  liveTurn().querySelector(".log").append(line);
}

/* Every number here already rode in on the status event and none of them were
   shown: the browser read `text` and dropped `data`, so a turn was a spinner
   and a sentence. A run that is 19 rounds in at 84% of its window with the
   model at 3 tok/s looks exactly like a healthy one until it fails. */
function meterText(data) {
  if (!data) return "";
  const parts = [];
  if (data.round && data.max_rounds) parts.push(`rnd ${data.round}/${data.max_rounds}`);
  if (typeof data.ctx_pct === "number") parts.push(`ctx ${data.ctx_pct}%`);
  if (typeof data.tokens_per_second === "number" && data.tokens_per_second > 0) {
    parts.push(`${Math.round(data.tokens_per_second)} tok/s`);
  }
  return parts.join(" / ");
}

function setFooter(value, done, data) {
  const foot = liveTurn().querySelector(".foot");
  foot.replaceChildren();
  if (!done) {
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    foot.append(spinner);
  }
  foot.append(document.createTextNode(value || ""));

  const meter = meterText(data);
  if (meter) {
    const span = document.createElement("span");
    span.className = "meter";
    span.textContent = ` / ${meter}`;
    foot.append(span);
  }

  foot.classList.toggle("done", Boolean(done));
  // A failed turn must not read as a finished one. `turn.end` carries the
  // verdict; without this a run that died looked identical to one that worked.
  foot.classList.toggle("failed", Boolean(done) && (data || {}).status === "failed");
  if (done) document.getElementById("live-turn")?.removeAttribute("id");
}

/* --- sending ------------------------------------------------------------ */

async function send(event) {
  event.preventDefault();
  if (state.sending) return;
  const box = el("prompt");
  const text = box.value.trim();
  if (!text) return;
  if (text.startsWith("/") && (await runCommand(text))) { box.value = ""; autoGrow(); return; }
  if (!state.workspace) { addWorkspace(""); return; }
  const intendedWorkspace = state.workspace.id;
  const intendedBase = state.base;
  if (state.creatingSession) await state.creatingSession;
  if (state.workspace?.id !== intendedWorkspace || (intendedBase && state.base !== intendedBase)) return;
  if (!state.base) await newThread("");
  if (state.workspace?.id !== intendedWorkspace) return;
  if (!state.base) return;
  const base = state.base;
  const epoch = state.selectionEpoch;
  state.sending = true;
  el("send").disabled = true;
  el("send").setAttribute("aria-busy", "true");
  box.value = "";
  state.drafts.delete(base);
  autoGrow();
  el("palette").hidden = true;
  dropWelcome();
  const node = bubble("user", text);
  node.classList.add("pending");
  el("conversation").append(node);
  const pending = { text, base, node, echoed: false };
  state.pendingPrompts.push(pending);
  scrollToBottom(false);
  try {
    const result = await post(`${base}/prompt`, { text });
    if (epoch === state.selectionEpoch) notice(result.queued ? result.reason || "Task queued" : "Task accepted");
  } catch (error) {
    if (pending.echoed) return;
    node.classList.remove("pending"); node.classList.add("failed");
    const retry = document.createElement("button");
    retry.type = "button"; retry.className = "btn"; retry.textContent = "Retry task";
    retry.addEventListener("click", () => {
      if (state.base !== base) return;
      node.remove(); box.value = text; send({ preventDefault() {} });
    });
    node.append(retry);
    state.pendingPrompts = state.pendingPrompts.filter((item) => item !== pending);
    if (epoch === state.selectionEpoch) { if (!box.value) box.value = text; notice(`Could not send: ${error.message}`, true); }
  } finally {
    state.sending = false;
    el("send").disabled = false;
    el("send").removeAttribute("aria-busy");
    if (base === state.base) refreshQueue();
  }
}

function notice(message, bad) {
  const hint = el("composer-hint");
  hint.textContent = message;
  hint.style.color = bad ? "var(--danger)" : "var(--warn)";
  clearTimeout(notice.timer);
  if (bad) return;
  notice.timer = setTimeout(() => {
    hint.textContent = "";
    hint.style.color = "";
  }, 6000);
}

function autoGrow() {
  const box = el("prompt");
  box.style.height = "auto";
  box.style.height = `${Math.min(box.scrollHeight, window.innerHeight * 0.4)}px`;
}

/* --- the queue ---------------------------------------------------------- */

async function refreshQueue() {
  const base = state.base;
  if (!base) return;
  let payload;
  try { payload = await api(`${base}/queue`); } catch { return; }
  if (base !== state.base) return;
  const strip = el("queue-strip");
  strip.replaceChildren();
  strip.hidden = payload.queued.length === 0;
  for (const item of payload.queued) {
    const chip = document.createElement("span"); chip.className = "chip";
    const label = document.createElement("span"); label.textContent = `Queued: ${item.text.slice(0, 48)}`;
    const drop = document.createElement("button"); drop.className = "x"; drop.type = "button";
    drop.title = "Remove from queue"; drop.textContent = "\u00d7"; drop.setAttribute("aria-label", "Remove from queue");
    drop.addEventListener("click", async () => {
      try { await post(`${base}/cancel`, { queue_id: item.queue_id }); if (base === state.base) refreshQueue(); }
      catch (error) { notice(error.message, true); }
    });
    chip.append(label, drop); strip.append(chip);
  }
  setRunning(Boolean(payload.running_on));
}

/* --- approvals ---------------------------------------------------------- */

async function pollApprovals() {
  let payload;
  try {
    payload = await api("/api/approvals");
  } catch {
    return;
  }
  const live = new Set(payload.approvals.map((item) => item.approval_id));
  // Retract anything answered elsewhere - the phone and the terminal can
  // answer the same question, and a card left on screen would invite a second
  // answer to something already decided.
  for (const id of state.seenApprovals) {
    if (!live.has(id)) {
      document.getElementById(`approval-${id}`)?.remove();
      state.seenApprovals.delete(id);
    }
  }
  el("approval-empty").hidden = payload.approvals.length > 0;
  for (const approval of payload.approvals) {
    if (state.seenApprovals.has(approval.approval_id)) continue;
    state.seenApprovals.add(approval.approval_id);
    el("approval-list").append(approvalCard(approval));
    setInspector(true);
    if (isAtBottom()) scrollToBottom(false);
  }
}

function approvalCard(approval) {
  const card = document.createElement("section");
  card.className = "approval";
  card.id = `approval-${approval.approval_id}`;

  const title = document.createElement("h3");
  title.textContent = "SHAMSU needs approval";
  const what = document.createElement("div");
  what.className = "what";
  what.textContent = approval.description || approval.action_type || "an action";
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = [approval.risk_level && `risk: ${approval.risk_level}`, approval.workspace]
    .filter(Boolean)
    .join(" / ");

  const row = document.createElement("div");
  row.className = "row";
  const allow = document.createElement("button");
  allow.className = "btn primary";
  allow.type = "button";
  allow.textContent = "Allow";
  const deny = document.createElement("button");
  deny.className = "btn ghost";
  deny.type = "button";
  deny.textContent = "Deny";

  for (const [button, decision] of [[allow, "allow"], [deny, "deny"]]) {
    button.addEventListener("click", async () => {
      allow.disabled = true;
      deny.disabled = true;
      try {
        const result = await post(`/api/approvals/${approval.approval_id}`, { decision });
        if (!result.resolved) notice("already answered somewhere else");
      } catch (error) {
        notice(error.message, true);
        allow.disabled = false;
        deny.disabled = false;
        return;
      }
      card.remove();
      state.seenApprovals.delete(approval.approval_id);
    });
  }
  row.append(allow, deny);
  card.append(title, what, meta);
  if (approval.preview) {
    const preview = document.createElement("pre");
    preview.textContent = approval.preview;
    card.append(preview);
  }
  card.append(row);
  return card;
}

function onComposerKey(event) {
  if (event.isComposing || event.keyCode === 229) return;
  const matches = paletteMatches();
  const open = !el("palette").hidden && matches.length > 0;

  if (open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    state.paletteIndex = (state.paletteIndex + step + matches.length) % matches.length;
    refreshPalette();
    return;
  }
  if (open && (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey))) {
    event.preventDefault();
    acceptCommand(matches[state.paletteIndex]);
    return;
  }
  if (event.key === "Escape" && open) {
    event.preventDefault();
    el("palette").hidden = true;
    return;
  }
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    el("composer").requestSubmit();
  }
}

/* --- sticky bottom ------------------------------------------------------ */

function isAtBottom() {
  const scroller = el("scroller");
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= BOTTOM_SLACK_PX;
}

function scrollToBottom(instant) {
  const scroller = el("scroller");
  scroller.scrollTo({ top: scroller.scrollHeight, behavior: instant ? "auto" : "smooth" });
  state.pendingNew = 0;
  updateJump();
}

function updateJump() {
  el("jump").hidden = state.pendingNew === 0;
  el("jump-count").textContent = String(state.pendingNew);
}

/* --- commands ----------------------------------------------------------- *
 *
 * A "/" at the START of an empty-ish composer opens the palette. Deliberately
 * only at the start: a path or a regex inside a prompt contains slashes, and
 * popping a menu over those would make the composer hostile to exactly the
 * text this tool is for.
 */

async function loadCommands() {
  try {
    ({ commands: state.commands } = await api("/api/commands"));
  } catch {
    state.commands = [];
  }
}

function paletteQuery() {
  const value = el("prompt").value;
  if (!value.startsWith("/")) return null;
  if (value.includes("\n")) return null;
  return value;
}

function refreshPalette() {
  const query = paletteQuery();
  const palette = el("palette");
  if (query === null) {
    palette.hidden = true;
    return;
  }
  const word = query.split(/\s+/)[0].toLowerCase();
  const matches = state.commands.filter((command) => command.name.startsWith(word));
  if (!matches.length) {
    palette.hidden = true;
    return;
  }
  state.paletteIndex = Math.min(state.paletteIndex, matches.length - 1);
  palette.replaceChildren();
  matches.forEach((command, index) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "cmd";
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", String(index === state.paletteIndex));

    const name = document.createElement("span");
    name.className = "name";
    name.textContent = command.name;
    const args = document.createElement("span");
    args.className = "args";
    args.textContent = command.args || "";
    const help = document.createElement("span");
    help.className = "help";
    help.textContent = command.help;

    row.append(name, args, help);
    row.addEventListener("click", () => acceptCommand(command));
    palette.append(row);
  });
  palette.hidden = false;
  palette.dataset.count = String(matches.length);
}

function paletteMatches() {
  const query = paletteQuery();
  if (query === null) return [];
  const word = query.split(/\s+/)[0].toLowerCase();
  return state.commands.filter((command) => command.name.startsWith(word));
}

function acceptCommand(command) {
  const box = el("prompt");
  // Commands that take an argument keep the cursor after a space, so you can
  // type the path or the title without retyping the command.
  box.value = command.args ? `${command.name} ` : command.name;
  box.focus();
  if (!command.args) {
    runCommand(box.value.trim());
    box.value = "";
  }
  autoGrow();
  refreshPalette();
}

async function runCommand(line) {
  const [name, ...rest] = line.split(/\s+/);
  const argument = rest.join(" ").trim();
  switch (name) {
    case "/help":
      showHelp();
      return true;
    case "/settings":
      openSettings();
      return true;
    case "/threads":
      el("filter").focus();
      return true;
    case "/new":
      await newThread(argument);
      return true;
    case "/workspace":
      await addWorkspace(argument);
      return true;
    case "/queue":
      await refreshQueue();
      notice(
        el("queue-strip").hidden ? "nothing queued" : "queued prompts shown above",
      );
      return true;
    case "/approvals":
      await pollApprovals();
      notice(
        state.seenApprovals.size
          ? `${state.seenApprovals.size} awaiting approval`
          : "nothing awaiting approval",
      );
      return true;
    default:
      return false;
  }
}

function showHelp() {
  dropWelcome();
  const block = document.createElement("article");
  block.className = "msg assistant";
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = state.commands
    .map((command) => `${command.name} ${command.args}`.trim() + ` - ${command.help}`)
    .join("\n");
  block.append(body);
  el("conversation").append(block);
  scrollToBottom(false);
}

/* --- threads and workspaces --------------------------------------------- */

async function newThread(title) {
  if (state.creatingSession) return state.creatingSession;
  const workspace = state.workspace || state.workspaces[0];
  if (!workspace) { addWorkspace(""); return; }
  const epoch = state.selectionEpoch;
  const task = (async () => {
    try {
      const { session } = await post(`/api/workspaces/${workspace.id}/sessions`, { title });
      await loadWorkspaces();
      if (state.workspace?.id !== workspace.id || epoch !== state.selectionEpoch) return;
      const freshWorkspace = state.workspaces.find((item) => item.id === workspace.id) || workspace;
      await openSession(session, freshWorkspace);
      notice(`New conversation in ${freshWorkspace.name}`);
    } catch (error) { notice(`Could not create conversation: ${error.message}`, true); }
  })();
  state.creatingSession = task;
  try { await task; } finally { state.creatingSession = null; }
}

async function addWorkspace(path) {
  if (!path) {
    el("project-error").textContent = "";
    el("project-create").disabled = !state.runtime?.workspace_root;
    if (state.runtime?.workspace_root && !el("project-path").value) el("project-path").value = state.runtime.workspace_root + "/";
    el("project-dialog").showModal();
    return;
  }
  try {
    const { workspace } = await post("/api/workspaces", { path });
    await loadWorkspaces(); await openWorkspace(workspace); notice(`Added ${workspace.name}`);
  } catch (error) { notice(error.message, true); }
}

/* --- settings ------------------------------------------------------------ *
 *
 * The drawer opens on ONE cheap request. Everything that costs a round trip to
 * Ollama, to Telegram or to the control database loads afterwards and fills in,
 * because a settings page that hangs because a model server is down is a
 * settings page you cannot use to notice the model server is down.
 */

async function openSettings() {
  state.settingsFocus = document.activeElement;
  for (const id of ["appbar", "sidebar", "main", "run-inspector"]) el(id).inert = true;
  el("drawer").hidden = false;
  el("close-settings").focus();
  try {
    renderSettings(await api("/api/settings"));
  } catch (error) {
    notice(error.message, true);
    return;
  }
  loadModels();
  loadTelegram();
  loadLocks();
}

function closeSettings() {
  el("drawer").hidden = true;
  for (const id of ["appbar", "sidebar", "main", "run-inspector"]) el(id).inert = false;
  state.settingsFocus?.focus();
}

function renderSettings(settings) {
  el("set-model").textContent = settings.model || "no model configured";
  renderContext(settings.context);
  renderVerbosity(settings.verbosity);
  el("set-telegram").textContent = settings.telegram.configured
    ? `token configured (${settings.telegram.source})`
    : "no bot token";
  fillList(el("set-tools"), settings.tools, (tool) => tool);
}

function renderContext(context) {
  const buttons = el("ctx-buttons");
  buttons.replaceChildren();
  for (const bucket of context.buckets) {
    buttons.append(
      choice(`${Math.round(bucket / 1024)}k`, bucket === context.max_ctx, context.env_override, () =>
        saveSetting({ chat_max_ctx: bucket }, `context window set to ${bucket}`),
      ),
    );
  }
  buttons.append(
    choice("default", !context.saved, context.env_override, () =>
      saveSetting({ chat_max_ctx: null }, "context window reset"),
    ),
  );
  el("ctx-env").hidden = !context.env_override;
  el("ctx-meter").textContent = context.last_window
    ? `last turn used ${Math.round(context.last_prompt_tokens / 100) / 10}k of ` +
      `${Math.round(context.last_window / 1024)}k (${context.pct}%)`
    : "no turn measured in this process yet";
}

function renderVerbosity(verbosity) {
  const buttons = el("verb-buttons");
  buttons.replaceChildren();
  for (const level of verbosity.levels) {
    buttons.append(
      choice(level, level === verbosity.level, false, () =>
        saveSetting({ verbosity: level }, `verbosity set to ${level}`),
      ),
    );
  }
  el("verb-kinds").textContent = `shows: ${verbosity.body_kinds.join(", ")}`;
}

function choice(label, pressed, disabled, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ctx-btn";
  button.textContent = label;
  button.setAttribute("aria-pressed", String(pressed));
  button.disabled = Boolean(disabled);
  button.addEventListener("click", onClick);
  return button;
}

function fillList(list, items, label) {
  list.replaceChildren();
  for (const item of items) {
    const row = document.createElement("li");
    row.textContent = label(item);
    list.append(row);
  }
}

async function saveSetting(change, message) {
  try {
    renderSettings(await post("/api/settings", change));
    notice(message);
  } catch (error) {
    notice(error.message, true);
  }
}

/* --- models -------------------------------------------------------------- */

async function loadModels() {
  const pick = el("model-pick");
  let models;
  try {
    models = await api("/api/models");
  } catch (error) {
    el("model-source").textContent = error.message;
    return;
  }
  el("set-model").textContent = models.effective || "no model configured";
  el("model-source").textContent = models.server_running
    ? models.source_label
    : `${models.source_label} - Ollama is not reachable at ${models.base_url}`;
  // A workspace pin outranks anything chosen here. Saying so is the difference
  // between a picker that looks broken and one that is merely outranked.
  el("model-shadow").hidden = !models.workspace_pin_shadows;

  pick.replaceChildren();
  pick.append(option("", "use the default for this machine"));
  for (const model of models.models) {
    const marks = [];
    if (!model.installed) marks.push("not pulled");
    if (!model.known) marks.push("untested");
    if (model.loaded) marks.push("in VRAM");
    pick.append(option(model.name, marks.length ? `${model.name} (${marks.join(", ")})` : model.name));
  }
  pick.value = models.source === "install" ? models.effective : "";
  pick.disabled = models.source === "env";
  renderOllama(models);
}

function option(value, label) {
  const item = document.createElement("option");
  item.value = value;
  item.textContent = label;
  return item;
}

async function saveModel() {
  const chosen = el("model-pick").value;
  try {
    renderSettings(await post("/api/settings", { model: chosen || null }));
    await loadModels();
    notice(chosen ? `model set to ${chosen}` : "model reset to the default");
  } catch (error) {
    notice(error.message, true);
  }
}

/* --- ollama -------------------------------------------------------------- */

function renderOllama(models) {
  el("ol-state").textContent = models.server_running
    ? `running at ${models.base_url}`
    : `not reachable at ${models.base_url}`;
  el("ol-loaded").textContent = models.loaded.length
    ? `in VRAM now: ${models.loaded.join(", ")}`
    : "nothing loaded";
  el("ol-unload").disabled = !models.loaded.length;
}

async function unloadModels() {
  try {
    const { unloaded } = await post("/api/ollama/unload", {});
    notice(unloaded.length ? `unloaded ${unloaded.join(", ")}` : "nothing of ours was loaded");
    await loadModels();
  } catch (error) {
    notice(error.message, true);
  }
}

/* --- telegram ------------------------------------------------------------ */

async function loadTelegram() {
  let telegram;
  try {
    ({ telegram } = await api("/api/telegram"));
  } catch (error) {
    el("tg-run").textContent = error.message;
    return;
  }
  state.telegram = telegram;
  renderTelegram(telegram);
}

function renderTelegram(telegram) {
  el("set-telegram").textContent = telegram.configured
    ? `token configured (${telegram.token_source})`
    : "no bot token";

  el("tg-run").textContent = telegram.running
    ? `polling in pid ${telegram.owner_pid}${telegram.is_this_process ? " (this server)" : ""}`
    : "not running";

  const facts = el("tg-facts");
  facts.replaceChildren();
  // Stated, not detected: SHAMSU only ever long-polls, and leaving that
  // ambiguous is how a registered webhook went unnoticed.
  addFact(facts, "Transport", telegram.transport);
  addFact(facts, "Project", telegram.workspace || "not started yet");
  addFact(facts, "Paired", String(telegram.paired_count));
  addFact(facts, "Sent", String(telegram.messages_sent));
  addFact(facts, "Failures", String(telegram.send_failures));
  if (telegram.last_error) addFact(facts, "Last error", telegram.last_error);

  el("tg-start").disabled = !telegram.configured || telegram.running;
  el("tg-stop").disabled = !telegram.running || !telegram.is_this_process;
  el("tg-test").disabled = !telegram.configured;

  fillWorkspacePicker(telegram.workspace);
  renderPairings(telegram.pairings || []);
}

function addFact(list, name, value) {
  const key = document.createElement("dt");
  key.textContent = name;
  const detail = document.createElement("dd");
  detail.textContent = value;
  list.append(key, detail);
}

function fillWorkspacePicker(current) {
  const pick = el("tg-workspace");
  pick.replaceChildren();
  for (const workspace of state.workspaces) {
    pick.append(option(workspace.id, workspace.name));
    if (workspace.path === current) pick.value = workspace.id;
  }
}

function renderPairings(pairings) {
  const list = el("tg-pairings");
  list.replaceChildren();
  if (!pairings.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "nobody paired yet";
    list.append(empty);
    return;
  }
  for (const pairing of pairings) {
    const row = document.createElement("li");
    const who = document.createElement("span");
    who.textContent = `${pairing.display_name || pairing.user_id} (${pairing.permission_level})`;
    const drop = document.createElement("button");
    drop.type = "button";
    drop.className = "link-btn";
    drop.textContent = "Unpair";
    drop.addEventListener("click", () => unpair(pairing.user_id));
    row.append(who, drop);
    list.append(row);
  }
}

async function startBot() {
  try {
    const payload = await post("/api/telegram/start", {});
    renderTelegram(payload.telegram);
    notice(payload.detail || "bot started");
  } catch (error) {
    notice(error.message, true);
    loadTelegram();
  }
}

async function stopBot() {
  try {
    const { telegram } = await post("/api/telegram/stop", {});
    renderTelegram(telegram);
    notice("bot stopped");
  } catch (error) {
    notice(error.message, true);
  }
}

async function bindBot() {
  const workspace_id = el("tg-workspace").value;
  if (!workspace_id) return;
  try {
    const payload = await post("/api/telegram/bind", { workspace_id });
    renderTelegram(payload.telegram);
    notice("bot rebound - pairings kept");
  } catch (error) {
    notice(error.message, true);
  }
}

async function testBot() {
  try {
    const { probe } = await post("/api/telegram/test", {});
    renderProbe(probe);
  } catch (error) {
    notice(error.message, true);
  }
}

function renderProbe(probe) {
  const line = el("tg-webhook");
  if (!probe.ok) {
    line.hidden = false;
    line.textContent = `Telegram refused: ${probe.error}`;
    el("tg-fix").hidden = true;
    return;
  }
  if (probe.webhook_blocks_polling) {
    // The failure this whole panel exists for: getUpdates returns 409 while a
    // webhook stands, the poll loop retries forever, and the bot looks fine.
    line.hidden = false;
    line.textContent =
      `A webhook is registered (${probe.webhook_url}), so long polling cannot ` +
      `receive anything. ${probe.pending_updates} update(s) are waiting.`;
    el("tg-fix").hidden = false;
    return;
  }
  line.hidden = false;
  line.textContent = `@${probe.bot_username} answered, no webhook registered.`;
  el("tg-fix").hidden = true;
}

async function deleteWebhook() {
  try {
    const payload = await post("/api/telegram/webhook/delete", {});
    notice(payload.message);
    if (payload.probe && payload.probe.ok) renderProbe(payload.probe);
    loadTelegram();
  } catch (error) {
    notice(error.message, true);
  }
}

async function pairDevice() {
  try {
    const { pairing } = await post("/api/telegram/pairings", {});
    const slot = el("tg-code");
    slot.hidden = false;
    slot.textContent = pairing.code;
    notice("send this code to the bot within 5 minutes");
  } catch (error) {
    notice(error.message, true);
  }
}

async function unpair(userId) {
  try {
    const { pairings } = await post(`/api/telegram/pairings/${userId}/unpair`, {});
    renderPairings(pairings);
    notice("device unpaired");
  } catch (error) {
    notice(error.message, true);
  }
}

async function saveTelegramToken() {
  const box = el("tg-token");
  const token = box.value.trim();
  if (!token) return;
  try {
    await post("/api/telegram", { token });
    box.value = "";
    notice("bot token saved for this installation");
    loadTelegram();
  } catch (error) {
    notice(error.message, true);
  }
}

/* --- locks --------------------------------------------------------------- */

async function loadLocks() {
  let locks;
  try {
    locks = await api("/api/locks");
  } catch (error) {
    notice(error.message, true);
    return;
  }
  renderLocks(locks);
}

function renderLocks(locks) {
  const list = el("lock-list");
  list.replaceChildren();
  if (!locks.leases.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "nothing running";
    list.append(empty);
    return;
  }
  for (const lease of locks.leases) {
    const row = document.createElement("li");
    row.textContent = lease.is_machine_slot
      ? `${lease.session_id} - ${lease.owner_surface}, pid ${lease.owner_pid}`
      : `${shortPath(lease.workspace)} / ${lease.session_id} - ${lease.owner_surface}, pid ${lease.owner_pid}`;
    list.append(row);
  }
}

function shortPath(path) {
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/") || path;
}

async function clearStaleLocks() {
  try {
    const locks = await post("/api/locks/clear-stale", {});
    renderLocks(locks);
    notice(locks.released ? `released ${locks.released} stale lock(s)` : "no stale locks");
  } catch (error) {
    notice(error.message, true);
  }
}

/* --- responsive rail ----------------------------------------------------- */

function narrowViewport() {
  return window.matchMedia("(max-width: 959px)").matches;
}

function toggleRail() {
  const app = el("app");
  if (narrowViewport()) {
    const isOpen = app.classList.toggle("rail-open");
    app.classList.remove("rail-hidden");
    el("rail-toggle").setAttribute("aria-expanded", String(isOpen));
    return;
  }
  const isHidden = app.classList.toggle("rail-hidden");
  app.classList.remove("rail-open");
  el("rail-toggle").setAttribute("aria-expanded", String(!isHidden));
}

function closeMobileRail() {
  if (!narrowViewport()) return;
  el("app").classList.remove("rail-open");
  el("rail-toggle").setAttribute("aria-expanded", "false");
}

function normalizeRailForViewport() {
  const app = el("app");
  if (narrowViewport()) {
    app.classList.remove("rail-hidden");
    el("rail-toggle").setAttribute("aria-expanded", String(app.classList.contains("rail-open")));
  } else {
    app.classList.remove("rail-open");
    el("rail-toggle").setAttribute("aria-expanded", String(!app.classList.contains("rail-hidden")));
  }
}

/* --- boot --------------------------------------------------------------- */

async function boot() {
  state.token = resolveToken();

  el("filter").addEventListener("input", applyFilter);
  el("jump").addEventListener("click", () => scrollToBottom(false));
  el("composer").addEventListener("submit", send);
  el("rail-toggle").addEventListener("click", toggleRail);
  el("rail-scrim").addEventListener("click", closeMobileRail);
  el("prompt").addEventListener("input", () => {
    autoGrow();
    refreshPalette();
  });
  el("prompt").addEventListener("keydown", onComposerKey);

  el("new-thread").addEventListener("click", () => newThread(""));
  el("add-workspace").addEventListener("click", () => addWorkspace(""));
  el("open-settings").addEventListener("click", openSettings);
  el("close-settings").addEventListener("click", closeSettings);
  el("drawer-scrim").addEventListener("click", closeSettings);
  el("tg-save").addEventListener("click", saveTelegramToken);
  el("model-pick").addEventListener("change", saveModel);
  el("tg-start").addEventListener("click", startBot);
  el("tg-stop").addEventListener("click", stopBot);
  el("tg-bind").addEventListener("click", bindBot);
  el("tg-test").addEventListener("click", testBot);
  el("tg-fix").addEventListener("click", deleteWebhook);
  el("tg-pair").addEventListener("click", pairDevice);
  el("ol-unload").addEventListener("click", unloadModels);
  el("lock-clear").addEventListener("click", clearStaleLocks);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Tab" && !el("drawer").hidden) {
      const fields = [...el("drawer-panel").querySelectorAll('button, input, select, textarea, a[href], [tabindex="0"]')].filter((node) => !node.disabled && node.offsetParent !== null);
      const first = fields[0], last = fields[fields.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    if (event.key !== "Escape") return;
    setInspector(false);
    if (!el("drawer").hidden) closeSettings();
    closeMobileRail();
  });
  window.addEventListener("resize", normalizeRailForViewport);
  el("scroller").addEventListener("scroll", () => {
    if (isAtBottom() && state.pendingNew) {
      state.pendingNew = 0;
      updateJump();
    }
  });
  normalizeRailForViewport();

  wireWorkbench();
  await connectWorkbench();
  setInterval(refreshQueue, QUEUE_POLL_MS);
  setInterval(pollApprovals, APPROVAL_POLL_MS);
  setInterval(refreshRuntime, 10000);
}


function beginSelection() {
  if (state.base) state.drafts.set(state.base, el("prompt").value);
  state.selectionEpoch += 1;
  state.stream?.close(); state.stream = null;
  state.turnId = ""; state.pendingNew = 0;
  el("queue-strip").replaceChildren(); el("queue-strip").hidden = true;
  setRunning(false); updateJump();
  return state.selectionEpoch;
}
function rememberLocation(project, session) {
  const hash = new URLSearchParams({ project, ...(session ? { session } : {}) }).toString();
  if (location.hash !== `#${hash}`) history.pushState({}, "", `#${hash}`);
}
async function restoreLocation() {
  const params = new URLSearchParams(location.hash.slice(1));
  const project = state.workspaces?.find((item) => item.id === params.get("project"));
  if (!project) return;
  const session = params.get("session");
  if (!session) { selectWorkspace(project, false); return; }
  try {
    const payload = await api(`/api/workspaces/${project.id}/sessions`);
    const found = payload.sessions.find((item) => item.session_id === session);
    if (found) await openSession(found, project, false);
    else selectWorkspace(project, false);
  } catch (error) { notice(error.message, true); }
}
function setConnection(message, bad = false) {
  el("service-state").textContent = message;
  el("service-state").dataset.state = bad ? "error" : "ready";
}
function setInspector(open) {
  el("app").classList.toggle("inspector-open", open);
  el("inspector-toggle").setAttribute("aria-expanded", String(open));
}
async function refreshRuntime() {
  try {
    const runtime = await api("/api/runtime"); state.runtime = runtime;
    el("project-create").disabled = !runtime.workspace_root;
    el("worker-state").textContent = runtime.worker === "busy" ? "Working on a task" : "Ready for tasks";
    el("runtime-model").textContent = runtime.model;
    el("runtime-queue").textContent = String(runtime.queued);
    el("rail-model").textContent = runtime.model;
    const downloading = runtime.model_download?.status === "downloading";
    const message = downloading ? "Downloading the configured model" : runtime.ready ? "Ready to work independently" : runtime.model_connected ? "Download a model to start coding" : "Model service unavailable";
    el("runtime-message").textContent = message;
    el("runtime-message").dataset.state = runtime.ready || downloading ? "ready" : "error";
    el("runtime-setup").hidden = runtime.ready;
    el("model-download").disabled = downloading || !runtime.model_connected;
    el("model-download").textContent = downloading ? "Downloading model" : "Download configured model";
    el("model-download-status").textContent = runtime.model_download?.error || (downloading ? "The download continues when you close this page." : runtime.model_installed ? "Configured model is installed." : "Downloads model weights to the service's persistent storage.");
    if (!state.stream || state.stream.readyState === EventSource.OPEN) setConnection("Service connected");
  } catch (error) { setConnection("Service unavailable", true); }
}
async function connectWorkbench() {
  try {
    const health = await api("/api/health");
    el("rail-model").textContent = health.model || "No model configured";
    setConnection("Service connected");
    await loadWorkspaces();
    await loadCommands();
    el("prompt").disabled = false; el("send").disabled = false;
    el("new-thread").hidden = !state.workspaces.length;
    if (location.hash) await restoreLocation();
    if (!state.workspace && state.workspaces.length) selectWorkspace(state.workspaces[0], false);
    await refreshRuntime(); await pollApprovals();
    el("add-workspace").disabled = false;
    if (el("connect-dialog").open) el("connect-dialog").close();
  } catch (error) {
    if (error.status === 401) { setConnection("Access token required", true); if (!el("connect-dialog").open) el("connect-dialog").showModal(); }
    else { setConnection("Service unavailable", true); notice(`Could not connect: ${error.message}. Reload to retry.`, true); }
    throw error;
  }
}
function wireWorkbench() {
  el("inspector-toggle").addEventListener("click", () => setInspector(!el("app").classList.contains("inspector-open")));
  el("close-inspector").addEventListener("click", () => setInspector(false));
  el("runtime-setup").addEventListener("click", openSettings);
  el("project-cancel").addEventListener("click", () => el("project-dialog").close());
  el("project-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const submit = el("project-submit"); submit.disabled = true;
    el("project-error").textContent = "";
    try {
      const { workspace } = await post("/api/workspaces", { path: el("project-path").value, create: el("project-create").checked });
      await loadWorkspaces(); await openWorkspace(workspace); el("project-dialog").close();
    } catch (error) { el("project-error").textContent = error.message; }
    finally { submit.disabled = false; }
  });
  el("connect-form").addEventListener("submit", async (event) => {
    event.preventDefault(); state.token = el("access-token").value.trim(); sessionStorage.setItem("shamsu-token", state.token);
    try { await connectWorkbench(); el("connect-error").textContent = ""; }
    catch (error) { el("connect-error").textContent = "Could not connect. Check the token and try again."; }
  });
  el("model-download").addEventListener("click", async () => {
    el("model-download").disabled = true;
    try { await post("/api/runtime/model"); await refreshRuntime(); }
    catch (error) { el("model-download-status").textContent = error.message; el("model-download").disabled = false; }
  });
  for (const button of document.querySelectorAll("[data-prompt]")) button.addEventListener("click", () => {
    el("prompt").value = button.dataset.prompt;
    if (!state.workspace) addWorkspace(""); else el("prompt").focus();
  });
  window.addEventListener("popstate", restoreLocation);
}
function renderMarkdown(container, text) {
  let code = null, list = null;
  const inline = (target, value) => {
    for (const part of value.split(/(`[^`]+`|\*\*[^*]+\*\*)/g)) {
      if (part.startsWith("`") && part.endsWith("`")) { const node = document.createElement("code"); node.textContent = part.slice(1, -1); target.append(node); }
      else if (part.startsWith("**") && part.endsWith("**")) { const node = document.createElement("strong"); node.textContent = part.slice(2, -2); target.append(node); }
      else target.append(document.createTextNode(part));
    }
  };
  for (const line of text.split("\n")) {
    if (line.trim().startsWith("```")) {
      if (code) code = null;
      else { const pre = document.createElement("pre"); code = document.createElement("code"); pre.append(code); container.append(pre); }
      list = null; continue;
    }
    if (code) { code.append(document.createTextNode(line + "\n")); continue; }
    const bullet = line.match(/^\s*[-*]\s+(.+)/);
    if (bullet) { if (!list) { list = document.createElement("ul"); container.append(list); } const li = document.createElement("li"); inline(li, bullet[1]); list.append(li); continue; }
    list = null;
    if (!line.trim()) continue;
    const heading = line.match(/^#{1,4}\s+(.+)/);
    const node = document.createElement(heading ? "h3" : "p"); inline(node, heading ? heading[1] : line); container.append(node);
  }
}

boot().catch(() => {});
