const messages = document.querySelector("#messages");
const promptInput = document.querySelector("#prompt");
const composer = document.querySelector("#composer");

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
  addMessage(
    "assistant",
    "This preview UI has captured your request. Backend command execution will be wired to the SHAMSU CLI in the next feature slice.",
  );
  promptInput.value = "";
});
