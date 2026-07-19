const messages = document.querySelector("#messages");
const promptInput = document.querySelector("#prompt");
const composer = document.querySelector("#composer");
const themeToggle = document.querySelector("#themeToggle");
const attachFileButton = document.querySelector("#attachFileButton");
const fileInput = document.querySelector("#fileInput");
const attachmentTray = document.querySelector("#attachmentTray");
const permissionModal = document.querySelector("#permissionModal");
const permissionDetails = document.querySelector("#permissionDetails");
const allowFileAccess = document.querySelector("#allowFileAccess");
const denyFileAccess = document.querySelector("#denyFileAccess");
const attachments = [];
const MAX_ATTACHMENT_BYTES = 200 * 1024;
let pendingPermissionResolve = null;

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
  if (attachFileButton) {
    attachFileButton.disabled = isBusy;
  }
}

function formatBytes(size) {
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function renderAttachments() {
  attachmentTray.innerHTML = "";
  attachments.forEach((attachment, index) => {
    const chip = document.createElement("div");
    chip.className = "attachment-chip";

    const name = document.createElement("span");
    name.textContent = attachment.name;
    name.title = attachment.name;

    const size = document.createElement("small");
    size.textContent = formatBytes(attachment.size);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${attachment.name}`);
    remove.textContent = "x";
    remove.addEventListener("click", () => {
      attachments.splice(index, 1);
      renderAttachments();
    });

    chip.append(name, size, remove);
    attachmentTray.append(chip);
  });
}

function readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")));
    reader.addEventListener("error", () => reject(reader.error || new Error("Could not read file.")));
    reader.readAsText(file);
  });
}

function askFilePermission(file) {
  return new Promise((resolve) => {
    pendingPermissionResolve = resolve;
    permissionDetails.textContent = `${file.name} (${formatBytes(file.size)}) will be read in your browser and sent only to the local SHAMSU backend with your next prompt.`;
    permissionModal.hidden = false;
    allowFileAccess.focus();
  });
}

function closePermissionDialog(allowed) {
  permissionModal.hidden = true;
  if (pendingPermissionResolve) {
    pendingPermissionResolve(allowed);
    pendingPermissionResolve = null;
  }
  attachFileButton.focus();
}

async function addFiles(fileList) {
  const files = Array.from(fileList || []);
  for (const file of files) {
    if (file.size > MAX_ATTACHMENT_BYTES) {
      addMessage("assistant", `${file.name} is too large for web sharing right now. Limit: ${formatBytes(MAX_ATTACHMENT_BYTES)}.`);
      continue;
    }
    const allowed = await askFilePermission(file);
    if (!allowed) {
      addMessage("assistant", `File access denied for ${file.name}.`);
      continue;
    }
    try {
      const content = await readFileAsText(file);
      attachments.push({
        name: file.name,
        type: file.type || "text/plain",
        size: file.size,
        content,
      });
    } catch (error) {
      addMessage("assistant", `Could not read ${file.name}: ${error.message}`);
    }
  }
  renderAttachments();
}

async function sendPrompt(text, sharedFiles) {
  try {
    const response = await fetch("/api/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: text, attachments: sharedFiles }),
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
  if (!text && attachments.length === 0) {
    return;
  }

  const sharedFiles = attachments.splice(0, attachments.length);
  const attachmentSummary = sharedFiles.length
    ? `\n\nAttached: ${sharedFiles.map((file) => file.name).join(", ")}`
    : "";
  addMessage("user", `${text || "Shared file(s) for review."}${attachmentSummary}`);
  if (sharedFiles.length > 0 && text.startsWith("/")) {
    addMessage("assistant", "Attached files are only included with natural prompts. Slash commands run exactly as typed.");
  }
  renderAttachments();
  promptInput.value = "";
  setComposerBusy(true);
  sendPrompt(text, sharedFiles)
    .then((answer) => addMessage("assistant", answer))
    .catch((error) => addMessage("assistant", `Unexpected UI error: ${error.message}`))
    .finally(() => setComposerBusy(false));
});

if (attachFileButton && fileInput) {
  attachFileButton.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    addFiles(fileInput.files).finally(() => {
      fileInput.value = "";
    });
  });
}

if (allowFileAccess && denyFileAccess && permissionModal) {
  allowFileAccess.addEventListener("click", () => closePermissionDialog(true));
  denyFileAccess.addEventListener("click", () => closePermissionDialog(false));
  permissionModal.addEventListener("click", (event) => {
    if (event.target === permissionModal) {
      closePermissionDialog(false);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (!permissionModal.hidden && event.key === "Escape") {
      closePermissionDialog(false);
    }
  });
}

if (themeToggle) {
  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme === "light" ? "light" : "dark";
    setTheme(current === "light" ? "dark" : "light");
  });
}

setTheme(localStorage.getItem("shamsu-theme") || "dark");
loadStatus();
