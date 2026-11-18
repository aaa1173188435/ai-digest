"""Generate a single-file labelling page.

No server, no build step, no dependencies: open the HTML file, click, export a
JSON. Labels persist in localStorage so closing the tab does not lose work.

Keyboard: ``1`` relevant, ``2`` irrelevant (applied to the first unlabelled
card), ``u`` undo the last one. 41 items take about three minutes.
"""

from __future__ import annotations

import json

from .dataset import EvalCase

_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>标注 · __CASE_ID_TEXT__</title>
<style>
  :root { --bg:#fbfaf8; --fg:#1c1b19; --muted:#6b6862; --line:#e3e0da;
          --yes:#1f7a45; --no:#a33; --chip:#efece6; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:16px/1.6 "Iowan Old Style", "Songti SC", Georgia, serif; }
  header { position:sticky; top:0; background:var(--bg);
           border-bottom:1px solid var(--line); padding:14px 20px;
           display:flex; gap:16px; align-items:center; flex-wrap:wrap; z-index:5; }
  h1 { font-size:16px; margin:0; font-weight:600; }
  .progress { color:var(--muted); font-size:14px;
              font-family:ui-monospace, Menlo, monospace; }
  .bar { flex:1; min-width:120px; height:6px; background:var(--chip);
         border-radius:3px; overflow:hidden; }
  .bar > i { display:block; height:100%; width:0; background:var(--yes); }
  button { font:inherit; font-size:14px; padding:6px 12px; border-radius:5px;
           border:1px solid var(--line); background:#fff; color:var(--fg);
           cursor:pointer; }
  button:hover { border-color:var(--muted); }
  button.primary { background:var(--fg); color:var(--bg); border-color:var(--fg); }
  main { max-width:760px; margin:0 auto; padding:20px; }
  .hint { color:var(--muted); font-size:13px; margin:0 0 18px; }
  .card { border:1px solid var(--line); border-radius:8px; padding:14px 16px;
          margin-bottom:12px; background:#fff; scroll-margin-top:90px; }
  .card.yes { border-left:4px solid var(--yes); }
  .card.no  { border-left:4px solid var(--no); opacity:.62; }
  .meta { font-size:12px; color:var(--muted);
          font-family:ui-monospace, Menlo, monospace; display:flex; gap:10px; }
  .title { font-size:17px; font-weight:600; margin:6px 0; line-height:1.35; }
  .sum { font-size:14px; color:var(--muted); margin:0 0 10px; }
  .acts { display:flex; gap:8px; }
  .acts button.on-yes { background:var(--yes); color:#fff; border-color:var(--yes); }
  .acts button.on-no  { background:var(--no);  color:#fff; border-color:var(--no); }
  .done { margin-top:60px; padding:20px; text-align:center; color:var(--muted); }
</style>
</head>
<body>
<header>
  <h1>标注 · __CASE_ID_TEXT__</h1>
  <span class="progress" id="progress">0 / 0</span>
  <div class="bar"><i id="bar"></i></div>
  <button id="export" class="primary">导出 labels.json</button>
  <button id="reset">清空</button>
</header>
<main>
  <p class="hint">
    问题只有一个：<b>这条内容，一个做 AI 工程的人今天值得花两分钟读吗？</b><br>
    广告、招聘、纯产品促销、明显跑题（比如前端库发版）→ 不值得。
    键盘 <kbd>1</kbd> = 值得，<kbd>2</kbd> = 不值得，<kbd>u</kbd> = 撤销。
  </p>
  <div id="cards"></div>
  <div class="done" id="done" hidden>全部标完了，点右上角导出。</div>
</main>
<script>
const CASE_ID = __CASE_ID__;
const ITEMS = __ITEMS__;
const KEY = "aidigest-labels-" + CASE_ID;

let labels = {};
try { labels = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { labels = {}; }
let history = [];

const cards = document.getElementById("cards");
for (const item of ITEMS) {
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.id = item.id;
  el.innerHTML = `
    <div class="meta"><span>${escapeHtml(item.source)}</span></div>
    <div class="title">${escapeHtml(item.title)}</div>
    <p class="sum">${escapeHtml(item.summary || "（无摘要）")}</p>
    <div class="acts">
      <button data-v="1">值得读</button>
      <button data-v="0">不值得</button>
    </div>`;
  el.querySelector('[data-v="1"]').addEventListener("click", () => set(item.id, 1));
  el.querySelector('[data-v="0"]').addEventListener("click", () => set(item.id, 0));
  cards.appendChild(el);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function set(id, value) {
  labels[id] = value;
  history.push(id);
  localStorage.setItem(KEY, JSON.stringify(labels));
  paint();
}

function paint() {
  const total = ITEMS.length;
  let done = 0;
  for (const el of cards.children) {
    const id = el.dataset.id;
    el.classList.remove("yes", "no");
    el.querySelectorAll(".acts button").forEach(b => b.classList.remove("on-yes", "on-no"));
    if (labels[id] === 1) {
      el.classList.add("yes");
      el.querySelector('[data-v="1"]').classList.add("on-yes");
      done++;
    } else if (labels[id] === 0) {
      el.classList.add("no");
      el.querySelector('[data-v="0"]').classList.add("on-no");
      done++;
    }
  }
  document.getElementById("progress").textContent = done + " / " + total;
  document.getElementById("bar").style.width = (total ? 100 * done / total : 0) + "%";
  document.getElementById("done").hidden = done !== total || total === 0;
}

function nextUnlabelled() {
  for (const el of cards.children) {
    if (!(el.dataset.id in labels)) return el;
  }
  return null;
}

document.addEventListener("keydown", e => {
  if (e.key === "1" || e.key === "2") {
    const el = nextUnlabelled();
    if (el) { set(el.dataset.id, e.key === "1" ? 1 : 0); el.scrollIntoView({block:"center"}); }
    e.preventDefault();
  } else if (e.key === "u") {
    const id = history.pop();
    if (id) { delete labels[id]; localStorage.setItem(KEY, JSON.stringify(labels)); paint(); }
    e.preventDefault();
  }
});

document.getElementById("export").addEventListener("click", () => {
  const payload = {
    case_id: CASE_ID,
    labels: labels,
    labeled_at: new Date().toISOString(),
    labeler: "manual",
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type:"application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "labels-" + CASE_ID + ".json";
  a.click();
  URL.revokeObjectURL(a.href);
});

document.getElementById("reset").addEventListener("click", () => {
  if (!confirm("清空所有标注？")) return;
  labels = {}; history = [];
  localStorage.removeItem(KEY);
  paint();
});

paint();
</script>
</body>
</html>
"""


def build_labeler_html(case: EvalCase) -> str:
    items = [
        {"id": item.id, "title": item.title, "source": item.source, "summary": item.summary}
        for item in case.items
    ]
    # Two distinct placeholders: the HTML one must stay plain text, the script
    # one must stay a valid JS string literal.
    html = _TEMPLATE.replace("__CASE_ID_TEXT__", case.case_id)
    return html.replace("__CASE_ID__", json.dumps(case.case_id)).replace(
        "__ITEMS__", json.dumps(items, ensure_ascii=False)
    )
