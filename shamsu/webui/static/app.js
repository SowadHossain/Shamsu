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
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
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

async function loadWorkspaces() {
  const { workspaces } = await api("/api/workspaces");
  state.workspaces = workspaces;
  const tree = el("tree");
  tree.replaceChildren();

  if (!workspaces.length) {
    const empty = document.createElement("p");
    empty.className = "ws-empty";
    empty.textContent = "No workspaces yet. Open SHAMSU in a project, or start it with --scan.";
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

  const dot = document.createElement("span");
  dot.className = "dot idle";
  const label = document.createElement("span");
  label.className = "label";
  label.textContent = session.title || session.session_id;
  const when = document.createElement("span");
  when.className = "when";
  when.textContent = relativeTime(session.updated_at);

  button.append(dot, label, when);
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

/* --- opening a thread --------------------------------------------------- */

async function openSession(session, workspace) {
  state.session = session;
  state.workspace = workspace;
  state.base = `/api/workspaces/${workspace.id}/sessions/${session.session_id}`;
  state.turnId = "";
  state.pendingNew = 0;
  updateJump();

  for (const button of document.querySelectorAll(".thread")) {
    button.setAttribute(
      "aria-current",
      String(button.dataset.sessionId === session.session_id),
    );
  }
  el("thread-title").textContent = session.title || session.session_id;
  el("thread-sub").textContent = workspace.path;
  el("prompt").disabled = false;
  el("send").disabled = false;
  el("prompt").focus();

  const { messages } = await api(`${state.base}/messages`);
  renderMessages(messages);
  scrollToBottom(true);
  subscribe();
  refreshQueue();
}

function renderMessages(messages) {
  const pane = el("conversation");
  pane.replaceChildren();
  const visible = messages.filter(
    (message) => message.role === "user" || message.role === "assistant",
  );
  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "welcome";
    empty.textContent = "Nothing here yet. Say something below.";
    pane.append(empty);
    return;
  }
  for (const message of visible) {
    pane.append(bubble(message.role, message.content));
  }
}

function bubble(role, content) {
  const block = document.createElement("article");
  block.className = `msg ${role}`;
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = role === "user" ? "you" : "shamsu";
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = content;
  block.append(who, body);
  return block;
}

/* --- the live turn ------------------------------------------------------ */

function subscribe() {
  if (state.stream) state.stream.close();
  // EventSource cannot set headers, so the token rides as a query parameter on
  // this one request. It never enters the address bar or the history.
  const url = `${state.base}/stream?t=${encodeURIComponent(state.token)}`;
  const stream = new EventSource(url);
  state.stream = stream;
  stream.onmessage = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    handleEvent(payload);
  };
  // The browser reconnects on its own and replays Last-Event-ID.
  stream.onerror = () => {};
}

function handleEvent(event) {
  if (event.turn_id && event.turn_id !== state.turnId) {
    state.turnId = event.turn_id;
    dropWelcome();
  }
  const wasAtBottom = isAtBottom();

  switch (event.kind) {
    case "turn.start":
      setRunning(true);
      if (event.text) el("conversation").append(bubble("user", event.text));
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
    case "status":
      setFooter(event.text, false);
      break;
    case "assistant":
      if (event.text) el("conversation").append(bubble("assistant", event.text));
      break;
    case "turn.end":
      setRunning(false);
      setFooter(event.text, true);
      refreshQueue();
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
  pill.hidden = false;
  pill.textContent = running ? "running" : "idle";
  pill.classList.toggle("running", running);
  const dot = document.querySelector('.thread[aria-current="true"] .dot');
  if (dot) dot.classList.toggle("idle", !running);
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

function setFooter(value, done) {
  const foot = liveTurn().querySelector(".foot");
  foot.replaceChildren();
  if (!done) {
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    foot.append(spinner);
  }
  foot.append(document.createTextNode(value || ""));
  foot.classList.toggle("done", Boolean(done));
  if (done) document.getElementById("live-turn")?.removeAttribute("id");
}

/* --- sending ------------------------------------------------------------ */

async function send(event) {
  event.preventDefault();
  const box = el("prompt");
  const text = box.value.trim();
  if (!text) return;

  box.value = "";
  autoGrow();
  el("palette").hidden = true;

  // A command is handled here, not sent to the model - the same split the
  // REPL makes. An unknown slash word falls through as an ordinary prompt,
  // because "/usr/bin/python is missing" is a sentence, not a typo.
  if (text.startsWith("/") && (await runCommand(text))) return;

  if (!state.base) {
    notice("pick a thread first, or /new", true);
    return;
  }
  dropWelcome();
  el("conversation").append(bubble("user", text));
  scrollToBottom(false);

  try {
    const result = await post(`${state.base}/prompt`, { text });
    if (result.queued) notice(result.reason || "queued");
  } catch (error) {
    notice(`could not send: ${error.message}`, true);
  }
  refreshQueue();
}

function notice(message, bad) {
  const hint = el("composer-hint");
  hint.textContent = message;
  hint.style.color = bad ? "var(--danger)" : "var(--warn)";
  clearTimeout(notice.timer);
  notice.timer = setTimeout(() => {
    hint.textContent = "Enter to send · Shift+Enter for a new line";
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
  if (!state.base) return;
  let payload;
  try {
    payload = await api(`${state.base}/queue`);
  } catch {
    return;
  }
  const strip = el("queue-strip");
  strip.replaceChildren();
  strip.hidden = payload.queued.length === 0;
  for (const item of payload.queued) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const label = document.createElement("span");
    label.textContent = `${item.source}: ${item.text.slice(0, 48)}`;
    const drop = document.createElement("button");
    drop.className = "x";
    drop.type = "button";
    drop.title = "Remove from queue";
    drop.textContent = "×";
    drop.addEventListener("click", async () => {
      await post(`${state.base}/cancel`, { queue_id: item.queue_id });
      refreshQueue();
    });
    chip.append(label, drop);
    strip.append(chip);
  }
  if (payload.running_on) setRunning(true);
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
  for (const approval of payload.approvals) {
    if (state.seenApprovals.has(approval.approval_id)) continue;
    state.seenApprovals.add(approval.approval_id);
    el("conversation").append(approvalCard(approval));
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
    .join(" · ");

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
  card.append(title, what, meta, row);
  return card;
}

function onComposerKey(event) {
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

function closeSettings() {
  el("drawer").hidden = true;
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
  const workspace = state.workspace || state.workspaces[0];
  if (!workspace) {
    notice("add a workspace first: /workspace <path>", true);
    return;
  }
  try {
    const { session } = await post(`/api/workspaces/${workspace.id}/sessions`, {
      title,
    });
    await loadWorkspaces();
    await openSession(session, workspace);
    notice(`new thread in ${workspace.name}`);
  } catch (error) {
    notice(`could not create: ${error.message}`, true);
  }
}

async function addWorkspace(path) {
  const chosen = path || window.prompt("Folder path for the workspace");
  if (!chosen) return;
  try {
    const { workspace } = await post("/api/workspaces", { path: chosen });
    await loadWorkspaces();
    notice(`added ${workspace.name}`);
  } catch (error) {
    notice(error.message, true);
  }
}

/* --- settings ------------------------------------------------------------ */

async function openSettings() {
  el("drawer").hidden = false;
  let settings;
  try {
    settings = await api("/api/settings");
  } catch (error) {
    notice(error.message, true);
    return;
  }
  renderSettings(settings);
}

function renderSettings(settings) {
  el("set-model").textContent = settings.model || "no model configured";

  const context = settings.context;
  const buttons = el("ctx-buttons");
  buttons.replaceChildren();
  for (const bucket of context.buckets) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ctx-btn";
    button.textContent = `${Math.round(bucket / 1024)}k`;
    button.setAttribute("aria-pressed", String(bucket === context.max_ctx));
    button.disabled = context.env_override;
    button.addEventListener("click", () => saveContext(bucket));
    buttons.append(button);
  }
  const auto = document.createElement("button");
  auto.type = "button";
  auto.className = "ctx-btn";
  auto.textContent = "default";
  auto.setAttribute("aria-pressed", String(!context.saved));
  auto.disabled = context.env_override;
  auto.addEventListener("click", () => saveContext(null));
  buttons.append(auto);

  el("ctx-env").hidden = !context.env_override;
  el("ctx-meter").textContent = context.last_window
    ? `last turn used ${Math.round(context.last_prompt_tokens / 100) / 10}k of ` +
      `${Math.round(context.last_window / 1024)}k (${context.pct}%)`
    : "no turn measured in this process yet";

  el("set-telegram").textContent = settings.telegram.configured
    ? `configured (${settings.telegram.source})`
    : "not configured";

  const tools = el("set-tools");
  tools.replaceChildren();
  for (const tool of settings.tools) {
    const item = document.createElement("li");
    item.textContent = tool;
    tools.append(item);
  }
}

async function saveContext(window_) {
  try {
    const settings = await post("/api/settings", { chat_max_ctx: window_ });
    renderSettings(settings);
    notice(window_ ? `context window set to ${window_}` : "context window reset");
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
    openSettings();
  } catch (error) {
    notice(error.message, true);
  }
}

/* --- boot --------------------------------------------------------------- */

async function boot() {
  state.token = resolveToken();

  el("filter").addEventListener("input", applyFilter);
  el("jump").addEventListener("click", () => scrollToBottom(false));
  el("composer").addEventListener("submit", send);
  el("rail-toggle").addEventListener("click", () => {
    document.getElementById("app").classList.toggle("rail-hidden");
  });
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
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el("drawer").hidden) closeSettings();
  });
  el("scroller").addEventListener("scroll", () => {
    if (isAtBottom() && state.pendingNew) {
      state.pendingNew = 0;
      updateJump();
    }
  });

  if (!state.token) {
    el("thread-title").textContent = "Open the link SHAMSU printed";
    return;
  }
  try {
    const health = await api("/api/health");
    el("rail-model").textContent = health.model || "no model";
    await loadCommands();
    await loadWorkspaces();
    // The composer works before a thread is picked, because /new and
    // /workspace are how you get one.
    el("prompt").disabled = false;
    el("send").disabled = false;
    el("new-thread").hidden = false;
  } catch (error) {
    el("thread-title").textContent = `Could not load: ${error.message}`;
    return;
  }
  setInterval(refreshQueue, QUEUE_POLL_MS);
  setInterval(pollApprovals, APPROVAL_POLL_MS);
  pollApprovals();
}

boot();
