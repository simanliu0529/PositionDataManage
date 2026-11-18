const form = document.querySelector("#query");
const status = document.querySelector("#status");
const previous = document.querySelector("#previous");
const next = document.querySelector("#next");
const rows = document.querySelector("#rows");
const markers = document.querySelector("#markers");
const pageSize = 50;
let offset = 0;
let filters = new URLSearchParams();
let active;

async function getJSON(url, signal) {
  const response = await fetch(url, { signal });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error);
  return data;
}

function render(items) {
  rows.replaceChildren();
  markers.replaceChildren();
  for (const item of items) {
    const row = rows.insertRow();
    for (const key of ["user_id", "checkin_time", "latitude", "longitude", "location_id", "node"]) {
      row.insertCell().textContent = item[key];
    }
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", (item.longitude + 180) * 2);
    dot.setAttribute("cy", (90 - item.latitude) * 2);
    dot.setAttribute("r", 4);
    dot.setAttribute("fill", item.node === 0 ? "#00609b" : "#9a451a");
    markers.append(dot);
  }
}

async function search(targetOffset) {
  active?.abort();
  active = new AbortController();
  const signal = active.signal;
  previous.disabled = next.disabled = true;
  status.textContent = "查询中…";
  render([]);
  const params = new URLSearchParams(filters);
  params.set("limit", pageSize);
  params.set("offset", targetOffset);
  try {
    const data = await getJSON("/api/checkins?" + params, signal);
    offset = targetOffset;
    render(data.items);
    status.textContent = `第 ${offset / pageSize + 1} 页 · ${data.items.length} 条 · 查询节点 ${data.queried_nodes.join("、")}`;
    previous.disabled = offset === 0;
    next.disabled = !data.has_more;
  } catch (error) {
    if (!signal.aborted) status.textContent = error.message;
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  filters = new URLSearchParams();
  for (const [key, value] of new FormData(form)) {
    if (value) filters.set(key, ["start", "end"].includes(key) ? value + "T00:00:00Z" : value);
  }
  search(0);
});
previous.addEventListener("click", () => search(offset - pageSize));
next.addEventListener("click", () => search(offset + pageSize));
const nodes = document.querySelector("#nodes");
getJSON("/api/nodes").then(data => {
  nodes.textContent = data.nodes.map(({node, count}) => `节点 ${node} · ${count.toLocaleString()} 条`).join("　|　");
}).catch(error => { nodes.textContent = error.message; });
search(0);
