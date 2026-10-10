"use strict";

const WEEKDAYS = ["日", "月", "火", "水", "木", "金", "土"];
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const state = { tab: "events", status: null, lastRender: "" };

// ---------- utils ----------
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

function parseDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function dateLabel(iso) {
  const d = parseDate(iso);
  return `${d.getMonth() + 1}月${d.getDate()}日(${WEEKDAYS[d.getDay()]})`;
}

function relativeDay(iso, todayIso) {
  const diff = Math.round((parseDate(iso) - parseDate(todayIso)) / 86400000);
  return { 0: "今日", 1: "明日", 2: "明後日" }[diff] || "";
}

function timeAgo(isoDateTime) {
  const sec = (Date.now() - new Date(isoDateTime).getTime()) / 1000;
  if (sec < 60) return "たった今";
  if (sec < 3600) return `${Math.floor(sec / 60)}分前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}時間前`;
  const d = new Date(isoDateTime);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 2600);
}

function sourceMeta(ev) {
  const parts = [];
  if (ev.location) parts.push(esc(ev.location));
  if (ev.source === "discord") {
    const from = [ev.source_channel, ev.source_author].filter(Boolean).map(esc).join(" ・ ");
    parts.push(ev.source_url ? `<a href="${esc(ev.source_url)}" target="_blank" rel="noopener">Discord</a> ${from}` : `Discord ${from}`);
  } else {
    const from = [ev.source_channel, ev.source_author].filter(Boolean).map(esc).join(" ・ ");
    if (ev.source === "winnotify") parts.push(`通知 ${from}`);
    else if (from) parts.push(from);
  }
  return parts.join(" ・ ");
}

function eventRow(ev, { deletable = true } = {}) {
  const time = ev.all_day
    ? "終日"
    : `${esc(ev.start_time)}${ev.end_time ? `<small>${esc(ev.end_time)}</small>` : ""}`;
  const barClass = ev.status === "pending" ? "pending" : ev.all_day ? "allday" : "";
  const meta = sourceMeta(ev);
  return `
    <div class="row event-row">
      <div class="time">${time}</div>
      <div class="bar ${barClass}"></div>
      <div class="body">
        <div class="title">${esc(ev.title)}</div>
        ${meta ? `<div class="meta">${meta}</div>` : ""}
      </div>
      ${deletable ? `<button class="icon-button delete" data-delete="${ev.id}" aria-label="削除" title="削除">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>` : ""}
    </div>`;
}

function empty(glyph, title, text) {
  return `<div class="empty"><span class="glyph">${glyph}</span><h3>${esc(title)}</h3><p>${esc(text)}</p></div>`;
}

// ---------- render ----------
async function renderEvents() {
  const events = await api("/api/events");
  if (!events.length) return empty("○", "予定はありません", "Discord などのメッセージや通知から予定が見つかるとここに表示されます");
  const today = state.status?.today || new Date().toISOString().slice(0, 10);
  const byDate = new Map();
  for (const ev of events) {
    if (!byDate.has(ev.date)) byDate.set(ev.date, []);
    byDate.get(ev.date).push(ev);
  }
  let html = "";
  for (const [date, list] of byDate) {
    const rel = relativeDay(date, today);
    html += `<div class="section-header"><strong class="${rel === "今日" ? "today" : ""}">${dateLabel(date)}</strong><span>${rel}</span></div>`;
    html += `<div class="group">${list.map((ev) => eventRow(ev)).join("")}</div>`;
  }
  return html;
}

function changeCard({ kind, change, event, after, conflicts }) {
  const isCancel = kind === "cancel";
  const rows = (list) => list.map((ev) => eventRow(ev, { deletable: false })).join("");
  const source = change.source_url
    ? ` <a href="${esc(change.source_url)}" target="_blank" rel="noopener">元メッセージ</a>`
    : "";
  return `
    <div class="group pending-card">
      <div class="heading">${isCancel ? "予定の取り消し" : "予定の変更"}</div>
      ${change.reason ? `<div class="reason">${esc(change.reason)}</div>` : ""}
      ${isCancel
        ? `<div class="label-small">${dateLabel(event.date)}</div>${rows([event])}`
        : `<div class="label-small">変更前 ・ ${dateLabel(event.date)}</div>${rows([event])}
           <div class="label-small">変更後 ・ ${dateLabel(after.date)}</div>${rows([after])}`}
      ${conflicts.length ? `<div class="label-small">重なる予定</div>${rows(conflicts)}` : ""}
      ${change.raw_text ? `<div class="quote">${esc(change.raw_text)}${source}</div>` : ""}
      <div class="actions">
        <button class="secondary-button" data-change-reject="${change.id}">${isCancel ? "残す" : "変更しない"}</button>
        <button class="primary-button" data-change-apply="${change.id}">${isCancel ? "取り消す" : "変更する"}</button>
      </div>
    </div>`;
}

async function renderPending() {
  const items = await api("/api/pending");
  if (!items.length) return empty("✓", "確認待ちはありません", "予定が重なったときや、予定の変更・取り消しがあいまいなときに、ここで選べます");
  return items.map((item) => item.kind === "update" || item.kind === "cancel" ? changeCard(item) : addCard(item)).join("");
}

function addCard({ event, conflicts, merge_into: mergeInto, likely_same: likelySame }) {
  const heading = likelySame
    ? `${dateLabel(event.date)}に同じ予定があるかもしれません`
    : `${dateLabel(event.date)}は既に予定があります`;
  return `
    <div class="group pending-card">
      <div class="heading">${heading}</div>
      ${eventRow(event, { deletable: false })}
      <div class="label-small">既存の予定</div>
      ${conflicts.map((c) => eventRow(c, { deletable: false })).join("") || `<div class="row secondary">（既存の予定は削除されました）</div>`}
      <div class="actions">
        <button class="secondary-button" data-reject="${event.id}">追加しない</button>
        ${mergeInto ? `<button class="secondary-button" data-merge="${event.id}" data-merge-into="${mergeInto}"
          title="${conflicts.length > 1 ? "一番上の既存の予定にまとめます" : "既存の予定にまとめます"}">同じ予定（まとめる）</button>` : ""}
        <button class="primary-button" data-confirm="${event.id}">追加する</button>
      </div>
    </div>`;
}

async function renderNotifications() {
  const list = await api("/api/notifications");
  if (!list.length) return empty("◎", "通知はありません", "予定が追加されるとここに通知が届きます");
  const hasUnread = list.some((n) => !n.read);
  return `
    ${hasUnread ? `<div class="toolbar"><button class="text-button" id="mark-read">すべて既読にする</button></div>` : ""}
    <div class="group" style="margin-top:16px">
      ${list.map((n) => `
        <div class="row notif-row">
          <span class="dot ${esc(n.kind)} ${n.read ? "read" : ""}"></span>
          <div class="msg">${esc(n.message)}${n.undoable ? `<button class="text-button undo" data-undo="${n.change_id}">元に戻す</button>` : ""}</div>
          <span class="when">${timeAgo(n.created_at)}</span>
        </div>`).join("")}
    </div>`;
}

async function render(force = false) {
  const renderer = { events: renderEvents, pending: renderPending, notifications: renderNotifications }[state.tab];
  try {
    const html = await renderer();
    if (force || html !== state.lastRender) {
      $("#content").innerHTML = html;
      state.lastRender = html;
    }
  } catch (e) {
    $("#content").innerHTML = empty("!", "読み込みに失敗しました", e.message);
  }
}

function renderStatus() {
  const s = state.status;
  if (!s) return;
  const setBadge = (el, n) => { el.hidden = !n; el.textContent = n; };
  setBadge($("#pending-badge"), s.pending_count);
  setBadge($("#unread-badge"), s.unread_count);

  const llm = s.llm;
  const llmText = llm.available
    ? `ローカルLLM: ${esc(llm.text_model)}${llm.vision_model && llm.vision_model !== llm.text_model ? `（画像: ${esc(llm.vision_model)}）` : llm.vision_model ? "（画像対応）" : "（画像非対応）"}`
    : `ローカルLLM: ${esc(llm.error || "未接続")}`;
  const items = [`<span class="item"><span class="light ${llm.available ? "ok" : "ng"}"></span>${llmText}
      <button class="text-button" id="refresh-llm">再検出</button></span>`];

  const STATE = { connected: "接続中", listening: "監視中", connecting: "接続しています…", starting: "開始しています…", reconnecting: "再接続中…", error: "エラー", stopped: "停止" };
  const lightOf = (state) => (state === "connected" || state === "listening" ? "ok" : state === "error" ? "ng" : "wait");

  const win = s.connectors.find((c) => c.name === "windows_notifications");
  if (win) {
    const backend = { winrt: "通知API", database: "通知データベース" }[win.backend] || "";
    const extra = win.error ? ` — ${esc(win.error)}` : `（${esc(win.apps.join(", "))}${backend ? ` / ${backend}` : ""}）`;
    items.push(`<span class="item"><span class="light ${lightOf(win.state)}"></span>Windows通知: ${STATE[win.state] || esc(win.state)}${extra}</span>`);
  }

  const discord = s.connectors.find((c) => c.name === "discord");
  if (discord) {
    const extra = discord.error ? ` — ${esc(discord.error)}` : !discord.owner_configured ? "（DISCORD_OWNER_ID 未設定のため DM 通知なし）" : "";
    items.push(`<span class="item"><span class="light ${lightOf(discord.state)}"></span>Discord Bot: ${STATE[discord.state] || esc(discord.state)}${extra}</span>`);
  } else if (!win) {
    items.push(`<span class="item"><span class="light"></span>連絡ツール: 未接続（.env を設定してください）</span>`);
  }
  $("#statusbar").innerHTML = items.join("");
}

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
    renderStatus();
  } catch (_) { /* サーバー停止中など */ }
}

// ---------- actions ----------
document.addEventListener("click", async (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  try {
    if (t.dataset.tab) {
      $$(".navbar .segment").forEach((b) => b.classList.toggle("active", b === t));
      state.tab = t.dataset.tab;
      await render(true);
    } else if (t.dataset.delete) {
      if (!confirm("この予定を削除しますか？")) return;
      await api(`/api/events/${t.dataset.delete}`, { method: "DELETE" });
      toast("削除しました");
      await render();
    } else if (t.dataset.confirm) {
      await api(`/api/events/${t.dataset.confirm}/confirm`, { method: "POST" });
      toast("予定を追加しました");
      await Promise.all([render(), refreshStatus()]);
    } else if (t.dataset.reject) {
      await api(`/api/events/${t.dataset.reject}/reject`, { method: "POST" });
      toast("追加しませんでした");
      await Promise.all([render(), refreshStatus()]);
    } else if (t.dataset.merge) {
      await api(`/api/events/${t.dataset.merge}/merge?into=${t.dataset.mergeInto}`, { method: "POST" });
      toast("同じ予定としてまとめました");
      await Promise.all([render(), refreshStatus()]);
    } else if (t.dataset.changeApply) {
      const res = await api(`/api/changes/${t.dataset.changeApply}/apply`, { method: "POST" });
      toast(res.status !== "applied" ? "対象の予定がなくなっていたため反映しませんでした" : res.kind === "cancel" ? "予定を取り消しました" : "予定を変更しました");
      await Promise.all([render(), refreshStatus()]);
    } else if (t.dataset.changeReject) {
      const res = await api(`/api/changes/${t.dataset.changeReject}/reject`, { method: "POST" });
      toast(res.kind === "cancel" ? "予定を残しました" : "変更しませんでした");
      await Promise.all([render(), refreshStatus()]);
    } else if (t.dataset.undo) {
      await api(`/api/changes/${t.dataset.undo}/undo`, { method: "POST" });
      toast("元に戻しました");
      await Promise.all([render(), refreshStatus()]);
    } else if (t.id === "mark-read") {
      await api("/api/notifications/read", { method: "POST" });
      await Promise.all([render(), refreshStatus()]);
    } else if (t.id === "refresh-llm") {
      t.disabled = true;
      await api("/api/llm/refresh", { method: "POST" });
      await refreshStatus();
      toast("ローカルLLMを再検出しました");
    }
  } catch (err) {
    toast(err.message);
  }
});

// ---------- sheet ----------
function openSheet() {
  $("#sheet").hidden = false;
  const dateInput = $("#manual-form [name=date]");
  if (!dateInput.value) dateInput.value = state.status?.today || new Date().toISOString().slice(0, 10);
  $("#analyze-form textarea").focus();
}
function closeSheet() { $("#sheet").hidden = true; }

$("#open-sheet").addEventListener("click", openSheet);
$("#close-sheet").addEventListener("click", closeSheet);
$("#sheet").addEventListener("click", (e) => { if (e.target.id === "sheet") closeSheet(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

$$("#sheet-tabs .segment").forEach((btn) => btn.addEventListener("click", (e) => {
  e.preventDefault();
  $$("#sheet-tabs .segment").forEach((b) => b.classList.toggle("active", b === btn));
  $("#analyze-form").hidden = btn.dataset.mode !== "analyze";
  $("#manual-form").hidden = btn.dataset.mode !== "manual";
}));

$("#analyze-form [type=file]").addEventListener("change", (e) => {
  const names = [...e.target.files].map((f) => f.name);
  $("#file-names").textContent = names.length ? names.join(", ") : "画像なし";
});

const RESULT_TAG = { added: "追加", pending: "確認待ち", duplicate: "登録済み", merged: "まとめた", updated: "変更", cancelled: "取り消し", change_pending: "確認待ち" };

$("#analyze-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const button = $("button[type=submit]", form);
  const out = $("#analyze-result");
  button.disabled = true;
  $(".spinner", button).hidden = false;
  $(".label", button).textContent = "ローカルLLMで精査中…";
  out.innerHTML = "";
  try {
    const res = await api("/api/analyze", { method: "POST", body: new FormData(form) });
    if (!res.results.length) {
      out.innerHTML = `<p class="secondary" style="text-align:center">${esc(res.skipped || "予定は見つかりませんでした")}</p>`;
    } else {
      out.innerHTML = `<div class="group">${res.results.map((r) => `
        <div class="row"><span class="result-tag ${r.status}">${RESULT_TAG[r.status]}</span>
        <span>${esc(r.event.label)}</span></div>`).join("")}</div>`;
      form.reset();
      $("#file-names").textContent = "画像なし";
    }
    await Promise.all([render(), refreshStatus()]);
  } catch (err) {
    out.innerHTML = `<p class="secondary" style="text-align:center;color:var(--red)">${esc(err.message)}</p>`;
  } finally {
    button.disabled = false;
    $(".spinner", button).hidden = true;
    $(".label", button).textContent = "解析する";
  }
});

$("#manual-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  for (const k of ["start_time", "end_time"]) if (!data[k]) delete data[k];
  try {
    const res = await api("/api/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const MESSAGES = { pending: "同日の予定と重なるため「確認待ち」に入りました", duplicate: "既に登録済みです", merged: "登録済みの同じ予定に情報を追加しました" };
    toast(MESSAGES[res.status] || `追加しました: ${res.event.label}`);
    e.target.reset();
    closeSheet();
    await Promise.all([render(), refreshStatus()]);
  } catch (err) {
    toast(err.message);
  }
});

// ---------- init ----------
(function init() {
  const now = new Date();
  $("#today-label").textContent = `${now.getMonth() + 1}月${now.getDate()}日 ${WEEKDAYS[now.getDay()]}曜日`;
  refreshStatus().then(() => render(true));
  setInterval(() => { refreshStatus(); if ($("#sheet").hidden) render(); }, 5000);
})();
