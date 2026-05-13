async function refreshStatus() {
  const badge = document.querySelector("[data-status-json]");
  if (!badge) return;
  try {
    const res = await fetch("/api/status");
    badge.textContent = JSON.stringify(await res.json(), null, 2);
  } catch {
    badge.textContent = "Status unavailable";
  }
}

setInterval(refreshStatus, 5000);

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-api-post]");
  if (!button) return;
  event.preventDefault();
  button.disabled = true;
  try {
    const res = await fetch(button.dataset.apiPost, { method: "POST" });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      alert(body.detail || "Action failed");
    }
    window.location.reload();
  } finally {
    button.disabled = false;
  }
});
