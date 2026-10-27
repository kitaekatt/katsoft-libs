// The hover popover shared by every trigger on the page.
//
// Hover or keyboard focus on a trigger opens it. A click pins it. Esc or its
// x button closes it and returns focus to the trigger. The mouse leaving the
// trigger and the popover closes it unless it is pinned. It has a maximum
// size, stays inside the window and scrolls. A tap on a phone pins it.
//
// attachPopover(trigger, build, { id, keyActivate })
//   build()       returns a DOM node (or string) shown in the popover
//   id            optional; a pinned popover follows the trigger with the same
//                 id when the page re-renders
//   keyActivate   true for a trigger that is not a native button or link
//                 (Enter or Space pins it)

import { el } from "./dom.js";

const OPEN_DELAY = 60;
const CLOSE_DELAY = 180;
const MARGIN = 8;

let node = null;
let body = null;
let current = null; // { trigger, build, id, pinned }
let openTimer = null;
let closeTimer = null;

function ensure() {
  if (node) return;
  const close = el("button", { class: "pop-x", type: "button", "aria-label": "Close popover", text: "x" });
  close.addEventListener("click", () => closePopover(true));
  body = el("div", { class: "pop-body" });
  node = el("div", { id: "popover-live", class: "pop", role: "dialog", "aria-label": "Details", tabindex: "-1", hidden: true }, close, body);
  node.addEventListener("mouseenter", () => clearTimeout(closeTimer));
  node.addEventListener("mouseleave", () => scheduleClose());
  node.addEventListener("focusout", (event) => {
    if (current && !current.pinned && !node.contains(event.relatedTarget) && event.relatedTarget !== current.trigger) {
      closePopover(false);
    }
  });
  document.body.append(node);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && current) {
      event.preventDefault();
      closePopover(true);
    }
  });
  window.addEventListener("resize", position);
  window.addEventListener("scroll", position, true);
}

function fill() {
  const content = current.build();
  body.replaceChildren(content instanceof Node ? content : document.createTextNode(String(content)));
}

function position() {
  if (!node || !current || node.hidden) return;
  const rect = current.trigger.getBoundingClientRect();
  node.style.maxWidth = `${Math.min(560, window.innerWidth - 2 * MARGIN)}px`;
  node.style.maxHeight = `${Math.max(120, Math.min(window.innerHeight * 0.6, window.innerHeight - 2 * MARGIN))}px`;
  const width = node.offsetWidth;
  const height = node.offsetHeight;
  const left = Math.min(Math.max(rect.left, MARGIN), Math.max(MARGIN, window.innerWidth - width - MARGIN));
  let top = rect.bottom + MARGIN;
  if (top + height > window.innerHeight - MARGIN && rect.top - MARGIN - height >= MARGIN) {
    top = rect.top - MARGIN - height;
  }
  top = Math.min(Math.max(top, MARGIN), Math.max(MARGIN, window.innerHeight - height - MARGIN));
  node.style.left = `${left}px`;
  node.style.top = `${top}px`;
}

function show(entry, pinned) {
  ensure();
  if (current && current.trigger !== entry.trigger) current.trigger.setAttribute("aria-expanded", "false");
  current = { ...entry, pinned };
  fill();
  node.hidden = false;
  node.classList.toggle("pinned", pinned);
  entry.trigger.setAttribute("aria-expanded", "true");
  position();
}

function scheduleClose() {
  clearTimeout(closeTimer);
  closeTimer = setTimeout(() => {
    if (current && !current.pinned) closePopover(false);
  }, CLOSE_DELAY);
}

/** The element the open popover belongs to, or null. */
export function popoverTrigger() {
  return current ? current.trigger : null;
}

export function closePopover(returnFocus) {
  clearTimeout(openTimer);
  clearTimeout(closeTimer);
  if (!current) return;
  const { trigger } = current;
  current = null;
  node.hidden = true;
  trigger.setAttribute("aria-expanded", "false");
  if (returnFocus && trigger.isConnected) trigger.focus();
}

export function attachPopover(trigger, build, options = {}) {
  ensure();
  const id = options.id || null;
  const entry = { trigger, build, id };
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-expanded", "false");
  trigger.setAttribute("aria-controls", "popover-live");

  if (id && current && current.id === id) {
    // The page re-rendered while this popover was open: follow the new trigger.
    current.trigger = trigger;
    current.build = build;
    trigger.setAttribute("aria-expanded", "true");
    fill();
    position();
  }

  const open = () => {
    clearTimeout(closeTimer);
    if (current && current.pinned && current.trigger !== trigger) return; // a pinned popover stays
    clearTimeout(openTimer);
    openTimer = setTimeout(() => show(entry, current ? current.pinned && current.trigger === trigger : false), OPEN_DELAY);
  };
  const pin = () => {
    clearTimeout(openTimer);
    show(entry, true);
  };

  trigger.addEventListener("mouseenter", open);
  trigger.addEventListener("focus", open);
  trigger.addEventListener("mouseleave", () => {
    clearTimeout(openTimer);
    scheduleClose();
  });
  trigger.addEventListener("blur", (event) => {
    clearTimeout(openTimer);
    if (current && !current.pinned && current.trigger === trigger && !(node && node.contains(event.relatedTarget))) {
      scheduleClose();
    }
  });
  trigger.addEventListener("click", pin);
  if (options.keyActivate) {
    trigger.addEventListener("keydown", (event) => {
      if (event.target === trigger && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        pin();
      }
    });
  }
  return { open: pin, close: () => closePopover(false) };
}
