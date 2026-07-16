const messages = document.querySelector("#messages");
const promptInput = document.querySelector("#prompt");
const composer = document.querySelector("#composer");
const themeToggle = document.querySelector("#themeToggle");

function addMessage(role, text) {
  const item = document.createElement("div");
  item.className = `message ${role}`;

  const label = document.createElement("span");
  label.textContent = role === "user" ? "You" : "Shamsu";

  const body = document.createElement("p");
  body.textContent = text;

  item.append(label, body);
  messages.append(item);
  item.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function setTheme(theme) {
  const normalized = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = normalized;
  localStorage.setItem("shamsu-theme", normalized);
  if (themeToggle) {
    const isLight = normalized === "light";
    themeToggle.textContent = isLight ? "Dark" : "Light";
    themeToggle.setAttribute("aria-pressed", String(isLight));
  }
}

function setComposerBusy(isBusy) {
  const submit = composer.querySelector('button[type="submit"]');
  submit.disabled = isBusy;
  submit.textContent = isBusy ? "Sending..." : "Send";
  promptInput.disabled = isBusy;
}

async function sendPrompt(text) {
  try {
    const response = await fetch("/api/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: text }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      return payload.answer || payload.error || "The backend could not complete that request.";
    }
    return payload.answer;
  } catch (error) {
    await loadStatus();
    return [
      "The browser could not reach the SHAMSU backend for that prompt.",
      "Refresh the page and try again. If you just restarted the server, wait a few seconds first.",
      "For long runtime commands like /models pull, use the terminal until web progress/approval controls are added.",
    ].join("\n");
  }
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status");
    const payload = await response.json();
    if (!payload.ok) {
      return;
    }
    const runtimeValue = document.querySelector('[data-status="runtime"]');
    const modelValue = document.querySelector('[data-status="model"]');
    const workspaceValue = document.querySelector('[data-status="workspace"]');
    if (runtimeValue) {
      runtimeValue.textContent = payload.runtime?.ready ? "Ready" : "Needs attention";
    }
    if (modelValue && payload.model) {
      modelValue.textContent = payload.model;
    }
    if (workspaceValue && payload.workspace) {
      workspaceValue.textContent = payload.workspace;
    }
  } catch {
    // The page can still render as a static shell if the API is unavailable.
  }
}

document.querySelectorAll("[data-command]").forEach((button) => {
  button.addEventListener("click", () => {
    const command = button.dataset.command || "";
    promptInput.value = command;
    promptInput.focus();
  });
});

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
  });
});

document.querySelectorAll(".thread").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".thread").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
  });
});

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = promptInput.value.trim();
  if (!text) {
    return;
  }

  addMessage("user", text);
  promptInput.value = "";
  setComposerBusy(true);
  sendPrompt(text)
    .then((answer) => addMessage("assistant", answer))
    .catch((error) => addMessage("assistant", `Unexpected UI error: ${error.message}`))
    .finally(() => setComposerBusy(false));
});

if (themeToggle) {
  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme === "light" ? "light" : "dark";
    setTheme(current === "light" ? "dark" : "light");
  });
}

setTheme(localStorage.getItem("shamsu-theme") || "dark");
loadStatus();
