const TIME_FORMAT_KEY = "zk-time-format";

function selectedTimeFormat() {
  const saved = window.localStorage.getItem(TIME_FORMAT_KEY);
  return saved === "12" ? "12" : "24";
}

function datePartsFor(value, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(value);
  const lookup = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${lookup.year}-${lookup.month}-${lookup.day}`;
}

function timeFor(value, timeZone, format) {
  const options = {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: format === "12",
  };
  return new Intl.DateTimeFormat(format === "12" ? "en-US" : "en-GB", options).format(value);
}

function renderTimestampElement(element, format = selectedTimeFormat()) {
  const value = element.dataset.timestamp;
  if (!value) return;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return;
  const timeZone = element.dataset.timezone || document.body.dataset.displayTimezone || "UTC";
  const datePart = element.querySelector("[data-date-part]");
  const timePart = element.querySelector("[data-time-part]");
  if (datePart) datePart.textContent = datePartsFor(date, timeZone);
  if (timePart) timePart.textContent = timeFor(date, timeZone, format);
}

function renderTimestampElements(root = document) {
  const format = selectedTimeFormat();
  root.querySelectorAll("[data-timestamp]").forEach((element) => renderTimestampElement(element, format));
  document.querySelectorAll("[data-time-format-option]").forEach((button) => {
    const active = button.dataset.timeFormatOption === format;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function initTimeFormatToggle() {
  document.querySelectorAll("[data-time-format-option]").forEach((button) => {
    button.addEventListener("click", () => {
      window.localStorage.setItem(TIME_FORMAT_KEY, button.dataset.timeFormatOption || "24");
      renderTimestampElements();
    });
  });
  renderTimestampElements();
}

document.addEventListener("DOMContentLoaded", initTimeFormatToggle);
