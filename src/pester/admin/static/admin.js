// Small progressive enhancements for the admin UI. Everything works without them except the messenger's
// reply and command helpers.

function localizeTimes(root) {
  for (const el of root.querySelectorAll("time[datetime]")) {
    const d = new Date(el.getAttribute("datetime"));
    if (isNaN(d)) continue;
    el.title = el.getAttribute("datetime");
    el.textContent = d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }
}

function scrollChat() {
  const log = document.getElementById("chat-log");
  if (log) log.scrollTop = log.scrollHeight;
}

function replyTo(id) {
  document.getElementById("reply-to").value = id;
  const banner = document.getElementById("replying");
  banner.hidden = false;
  const bubble = document.querySelector(`.bubble[data-id="${id}"] .prewrap`);
  banner.querySelector("span").textContent = bubble ? `“${bubble.textContent.slice(0, 60)}”` : "#" + id;
  document.querySelector(".composer input[name=text]").focus();
}

function clearReply() {
  const input = document.getElementById("reply-to");
  if (input) input.value = "";
  const banner = document.getElementById("replying");
  if (banner) banner.hidden = true;
}

document.addEventListener("DOMContentLoaded", () => {
  localizeTimes(document);
  scrollChat();
  document.addEventListener("click", (event) => {
    const command = event.target.closest("[data-command]");
    if (!command) return;
    const input = document.querySelector(".composer input[name=text]");
    input.value = command.dataset.command;
    input.focus();
  });
  document.body.addEventListener("htmx:afterSwap", (event) => {
    localizeTimes(event.detail.target.parentElement || document);
    if (event.detail.target.id === "chat-log" || event.detail.elt.id === "chat-log") scrollChat();
  });
});
