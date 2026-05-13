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

function appendCell(row, value) {
  const cell = document.createElement("td");
  cell.textContent = value ?? "";
  row.appendChild(cell);
}

function renderAttendanceRows(rows) {
  const body = document.querySelector("[data-attendance-body]");
  if (!body) return;
  body.replaceChildren();
  if (!rows.length) {
    const row = document.createElement("tr");
    row.dataset.emptyRow = "true";
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.textContent = "No attendance captured yet.";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  for (const item of rows) {
    const row = document.createElement("tr");
    appendCell(row, item.device_event_time);
    appendCell(row, item.zone_trusted_time);
    appendCell(row, item.user);
    appendCell(row, item.device_id);
    appendCell(row, item.source_type);
    appendCell(row, item.trust_status);
    appendCell(row, item.fraud_score);
    appendCell(row, item.fraud_reason);
    body.appendChild(row);
  }
}

async function refreshAttendance() {
  const body = document.querySelector("[data-attendance-body]");
  if (!body) return;
  const status = document.querySelector("[data-attendance-refresh]");
  try {
    const res = await fetch("/api/attendance/recent?limit=200");
    if (!res.ok) throw new Error("Attendance refresh failed");
    const data = await res.json();
    renderAttendanceRows(data.rows || []);
    if (status) status.textContent = `Last refreshed ${data.server_time}`;
  } catch {
    if (status) status.textContent = "Realtime refresh unavailable";
  }
}

setInterval(refreshAttendance, 3000);
refreshAttendance();

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
