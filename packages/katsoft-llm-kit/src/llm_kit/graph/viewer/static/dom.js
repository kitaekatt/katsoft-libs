// DOM helpers. Every string from a record goes through createTextNode or
// setAttribute; this page never builds markup from text.

const SVG_NS = "http://www.w3.org/2000/svg";

function build(node, props, children) {
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.setAttribute("class", value);
    else if (key === "text") node.textContent = String(value);
    else if (key === "on") for (const [name, fn] of Object.entries(value)) node.addEventListener(name, fn);
    else if (key === "dataset") for (const [name, v] of Object.entries(value)) node.dataset[name] = String(v);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function el(tag, props, ...children) {
  return build(document.createElement(tag), props, children);
}

export function svg(tag, props, ...children) {
  return build(document.createElementNS(SVG_NS, tag), props, children);
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function formatSeconds(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 90) return `${seconds.toFixed(1)} s`;
  if (seconds < 5400) return `${(seconds / 60).toFixed(1)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function formatCost(usd) {
  if (usd === null || usd === undefined) return "";
  if (usd === 0) return "$0";
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

export function formatTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString();
}

export function jsonText(value) {
  return JSON.stringify(value, null, 2) ?? "null";
}

export function shorten(text, limit) {
  const s = String(text);
  return s.length <= limit ? s : `${s.slice(0, Math.max(0, limit - 3))}...`;
}

export async function getJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  let body = null;
  try {
    body = await response.json();
  } catch (error) {
    throw new Error(`${url}: HTTP ${response.status}, the answer is not JSON`);
  }
  if (!response.ok) throw new Error(`${url}: ${body && body.error ? body.error : `HTTP ${response.status}`}`);
  return body;
}
