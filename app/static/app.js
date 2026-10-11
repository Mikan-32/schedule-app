"use strict";

const WEEKDAYS = ["日", "月", "火", "水", "木", "金", "土"];
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const state = { tab: "events", status: null, lastRender: "", renderSeq: 0, cal: { month: null, selected: null } };

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

const pad2 = (n) => String(n).padStart(2, "0");
function toIso(d) {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function todayIso() {
  return state.status?.today || toIso(new Date());
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
// ---------- calendar ----------
const CHIP_MAX = 3; // 1 日のマスに並べる予定の数（残りは「他N件」）

function monthOf(iso) {
  const d = parseDate(iso);
  return toIso(new Date(d.getFullYear(), d.getMonth(), 1));
}

// 月の表示範囲（日曜始まり。必要な週だけ）
function calendarRange(monthIso) {
  const first = parseDate(monthIso);
  const lastDay = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  const weeks = Math.ceil((first.getDay() + lastDay) / 7);
  const start = new Date(first.getFullYear(), first.getMonth(), 1 - first.getDay());
  const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + weeks * 7 - 1);
  return { first, start, end, weeks };
}

function selectDay(iso) {
  state.cal.selected = iso;
  state.cal.month = monthOf(iso);
}

function moveMonth(dir) {
  const today = todayIso();
  if (dir === "today") return selectDay(today);
  const cur = parseDate(state.cal.month || monthOf(today));
  const next = toIso(new Date(cur.getFullYear(), cur.getMonth() + (dir === "prev" ? -1 : 1), 1));
  // 今月に戻ったら今日、それ以外は 1 日を選ぶ
  selectDay(monthOf(today) === next ? today : next);
}

function calendarChip(ev) {
  const time = ev.all_day ? "" : `<span class="chip-time">${esc(ev.start_time)}</span>`;
  return `<span class="chip ${ev.all_day ? "allday" : ""}">${time}<span class="chip-title">${esc(ev.title)}</span></span>`;
}

async function renderEvents() {
  const today = todayIso();
  const cal = state.cal;
  if (!cal.selected) selectDay(today);
  const { first, start, end, weeks } = calendarRange(cal.month);
  const selected = cal.selected;
  const events = await api(`/api/events?date_from=${toIso(start)}&date_to=${toIso(end)}`);
  const byDate = new Map();
  for (const ev of events) {
    if (!byDate.has(ev.date)) byDate.set(ev.date, []);
    byDate.get(ev.date).push(ev);
  }

  let cells = "";
  for (let i = 0; i < weeks * 7; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    const iso = toIso(d);
    const list = byDate.get(iso) || [];
    const cls = ["day"];
    if (d.getMonth() !== first.getMonth()) cls.push("outside");
    if (d.getDay() === 0 || d.getDay() === 6) cls.push("weekend");
    if (iso === today) cls.push("today");
    if (iso === selected) cls.push("selected");
    const more = list.length - CHIP_MAX;
    cells += `
      <button class="${cls.join(" ")}" data-day="${iso}" aria-pressed="${iso === selected}"
        aria-label="${dateLabel(iso)}${iso === today ? " 今日" : ""}${list.length ? ` 予定${list.length}件` : ""}">
        <span class="num">${d.getDate()}</span>
        <span class="chips">${list.slice(0, CHIP_MAX).map(calendarChip).join("")}${more > 0 ? `<span class="more">他${more}件</span>` : ""}</span>
        <span class="dots">${list.slice(0, 3).map((ev) => `<i class="${ev.all_day ? "allday" : ""}"></i>`).join("")}</span>
      </button>`;
  }

  const dayEvents = byDate.get(selected) || [];
  const rel = relativeDay(selected, today);
  return `
    <div class="cal-header">
      <h2 class="cal-title">${first.getFullYear()}年${first.getMonth() + 1}月</h2>
      <div class="cal-nav">
        <button class="icon-button" data-cal-nav="prev" aria-label="前の月" title="前の月"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>
        <button class="text-button" data-cal-nav="today">今日</button>
        <button class="icon-button" data-cal-nav="next" aria-label="次の月" title="次の月"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg></button>
      </div>
    </div>
    <div class="group calendar">
      <div class="weekdays" aria-hidden="true">${WEEKDAYS.map((w) => `<span>${w}</span>`).join("")}</div>
      <div class="days">${cells}</div>
    </div>
    <div class="section-header"><strong class="${rel === "今日" ? "today" : ""}">${dateLabel(selected)}</strong><span>${rel}</span></div>
    <div class="group">${dayEvents.length ? dayEvents.map((ev) => eventRow(ev)).join("") : `<div class="row secondary">予定はありません</div>`}</div>`;
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
          <div class="msg">${esc(n.message)}${n.undoable ? `<button class="text-button undo" data-undo="${n.change_id}">元に戻す</button>` : ""}${n.adoptable ? `<button class="text-button undo" data-adopt="${n.event_id}">自分の予定として追加する</button>` : ""}</div>
          <span class="when">${timeAgo(n.created_at)}</span>
        </div>`).join("")}
    </div>`;
}

async function render(force = false) {
  const renderer = { events: renderEvents, pending: renderPending, notifications: renderNotifications }[state.tab];
  const seq = ++state.renderSeq;
  try {
    const html = await renderer();
    if (seq !== state.renderSeq) return; // 後から始まった描画があれば、そちらに任せる
    if (force || html !== state.lastRender) {
      $("#content").innerHTML = html;
      state.lastRender = html;
    }
  } catch (e) {
    if (seq !== state.renderSeq) return;
    $("#content").innerHTML = empty("!", "読み込みに失敗しました", e.message);
    state.lastRender = "";
  }
}

function renderStatus() {
  const s = state.status;
  if (!s) return;
  const setBadge = (el, n) => { el.hidden = !n; el.textContent = n; };
  setBadge($("#pending-badge"), s.pending_count);
  setBadge($("#unread-badge"), s.unread_count);

  const llm = s.llm;
  const rt = llm.runtime || {};
  const llmLabel = rt.mode === "embedded" ? "ローカルLLM（アプリ内）" : "ローカルLLM";
  let llmLight = llm.available ? "ok" : "ng";
  let llmText = llm.available
    ? `${llmLabel}: ${esc(llm.text_model)}${llm.vision_model && llm.vision_model !== llm.text_model ? `（画像: ${esc(llm.vision_model)}）` : llm.vision_model ? "（画像対応）" : "（画像非対応）"}`
    : `${llmLabel}: ${esc(llm.error || "未接続")}`;
  let llmButton = "再検出";
  if (rt.mode === "embedded" && rt.state !== "running") {
    // 同梱の Ollama の準備中（初回はダウンロード）・エラー
    const p = rt.progress;
    const pct = p && p.total ? ` ${fmtBytes(p.done)} / ${fmtBytes(p.total)}（${Math.floor((p.done * 100) / p.total)}%）` : "";
    llmText = `${llmLabel}: ${esc(rt.message || rt.state)}${pct}`;
    llmLight = rt.state === "error" || rt.state === "stopped" ? "ng" : "wait";
    llmButton = rt.state === "error" || rt.state === "stopped" ? "再試行" : "";
  }
  const items = [`<span class="item"><span class="light ${llmLight}"></span>${llmText}
      ${llmButton ? `<button class="text-button" id="refresh-llm">${llmButton}</button>` : ""}</span>`];

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

  const q = s.queue || {};
  if (q.waiting) items.push(`<span class="item"><span class="light wait"></span>精査待ち ${q.waiting}件（ローカルLLMにつながったら自動で精査）</span>`);
  if (q.failed) items.push(`<span class="item"><span class="light ng"></span>精査できなかったメッセージ ${q.failed}件
      <button class="text-button" id="retry-queue">再試行</button></span>`);

  const g = s.google || {};
  const login = (label) => `<a href="/api/google/login" target="_blank" rel="noopener">${label}</a>`;
  const GOOGLE = {
    no_credentials: ["", "未設定（README の手順で credentials.json を置いてください）"],
    bad_credentials: ["ng", "credentials.json を読み込めません"],
    not_connected: ["wait", `未連携 ${login("連携する")}`],
    connected: ["ok", `同期中（${g.synced ?? 0}件） <button class="text-button" id="google-disconnect">解除</button>`],
    error: ["ng", `${esc(g.error)} ${login("再連携")}`],
  };
  if (g.state && g.state !== "disabled") {
    const [light, text] = GOOGLE[g.state] || ["", esc(g.state)];
    items.push(`<span class="item"><span class="light ${light}"></span>Googleカレンダー: ${text}</span>`);
  }

  const p = s.power || {};
  if (p.supported && p.mode && p.mode !== "off") {
    const text = p.active ? "有効" : p.mode === "ac" ? "待機中（バッテリー駆動）" : "無効";
    items.push(`<span class="item"><span class="light ${p.active ? "ok" : ""}"></span>スリープ防止: ${text}</span>`);
  }
  $("#statusbar").innerHTML = items.join("");
}

function fmtBytes(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}GB`;
  return `${(n / 1e6).toFixed(n >= 1e8 ? 0 : 1)}MB`;
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
    } else if (t.dataset.day) {
      selectDay(t.dataset.day);
      await render(true);
      $(`[data-day="${t.dataset.day}"]`)?.focus();
    } else if (t.dataset.calNav) {
      moveMonth(t.dataset.calNav);
      await render(true);
      $(`[data-cal-nav="${t.dataset.calNav}"]`)?.focus();
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
    } else if (t.dataset.adopt) {
      const res = await api(`/api/events/${t.dataset.adopt}/adopt`, { method: "POST" });
      const ADOPTED = { added: "予定に追加しました", pending: "同日の予定と重なるため「確認待ち」に入りました", merged: "登録済みの同じ予定にまとめました", duplicate: "既に登録済みです" };
      toast(ADOPTED[res.status] || "追加しました");
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
    } else if (t.id === "retry-queue") {
      const res = await api("/api/queue/retry", { method: "POST" });
      toast(`${res.retried}件をもう一度精査します`);
      await refreshStatus();
    } else if (t.id === "google-disconnect") {
      if (!confirm("Google カレンダーとの連携を解除しますか？（登録済みの予定は Google カレンダーに残ります）")) return;
      await api("/api/google/disconnect", { method: "POST" });
      toast("連携を解除しました");
      await refreshStatus();
    } else if (t.id === "refresh-llm") {
      t.disabled = true;
      const res = await api("/api/llm/refresh", { method: "POST" });
      await refreshStatus();
      toast(res.runtime?.mode === "embedded" && res.runtime.state !== "running" ? "ローカルLLMの準備をやり直します" : "ローカルLLMを再検出しました");
    }
  } catch (err) {
    toast(err.message);
  }
});

// ---------- sheet ----------
function openSheet() {
  $("#sheet").hidden = false;
  const dateInput = $("#manual-form [name=date]");
  if (!dateInput.value) dateInput.value = (state.tab === "events" && state.cal.selected) || todayIso();
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

const RESULT_TAG = { added: "追加", pending: "確認待ち", duplicate: "登録済み", merged: "まとめた", updated: "変更", cancelled: "取り消し", change_pending: "確認待ち", skipped: "見送り" };

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
