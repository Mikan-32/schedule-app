"use strict";

const WEEKDAYS = ["日", "月", "火", "水", "木", "金", "土"];
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const state = { tab: "events", status: null, lastRender: "", renderSeq: 0, cal: { month: null, selected: null }, holidays: new Map() };

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
    <div class="row event-row app-${appOf(ev)}">
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
const HOUR_PX = 40; // 時間軸の 1 時間の高さ
// 連絡元（色は style.css の .app-*）
const APPS = { discord: "Discord", slack: "Slack", teams: "Teams", zoom: "Zoom", line: "LINE", manual: "手動", other: "その他" };

function appOf(ev) {
  if (ev.source === "manual") return "manual";
  if (ev.source !== "other" && APPS[ev.source]) return ev.source; // Discord Bot など
  // Windows 通知は source_channel が「Discord サーバー #ch」「Slack: タイトル」の形
  const head = String(ev.source_channel || "").split(":")[0].toLowerCase();
  for (const key of ["discord", "slack", "teams", "zoom"]) if (head.includes(key)) return key;
  return /\bline\b/.test(head) ? "line" : "other";
}

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

async function holidaysFor(from, to) {
  const key = `${from}/${to}`;
  if (!state.holidays.has(key)) {
    try {
      const list = await api(`/api/holidays?date_from=${from}&date_to=${to}`);
      state.holidays.set(key, new Map(list.map((h) => [h.date, h.name])));
    } catch (_) {
      return new Map(); // 祝日が取れなくてもカレンダーは出す（アプリの再起動前など）
    }
  }
  return state.holidays.get(key);
}

function groupByDate(events) {
  const byDate = new Map();
  for (const ev of events) {
    if (!byDate.has(ev.date)) byDate.set(ev.date, []);
    byDate.get(ev.date).push(ev);
  }
  return byDate;
}

const toMin = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };
const nowMinutes = (now) => now.getHours() * 60 + now.getMinutes();

// 時刻のある予定の [開始, 終了]（分）。終了がなければ 1 時間とみなす
function span(ev) {
  const s = toMin(ev.start_time);
  let e = ev.end_time ? toMin(ev.end_time) : s + 60;
  if (e <= s) e = s + 30;
  return [s, Math.min(e, 24 * 60)];
}

function timeText(ev) {
  return ev.all_day ? "終日" : esc(ev.time_range || ev.start_time);
}

function tooltip(ev) {
  return [ev.title, ev.all_day ? "終日" : ev.time_range || ev.start_time, ev.location, APPS[appOf(ev)]].filter(Boolean).join("\n");
}

function deleteButton(ev) {
  return `<button class="icon-button delete" data-delete="${ev.id}" aria-label="「${esc(ev.title)}」を削除" title="削除">
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>`;
}

function calendarChip(ev) {
  const time = ev.all_day ? "" : `<span class="chip-time">${esc(ev.start_time)}</span>`;
  return `<span class="chip app-${appOf(ev)}${ev.all_day ? " allday" : ""}">${time}<span class="chip-title">${esc(ev.title)}</span></span>`;
}

// 次の予定（今日のまだ終わっていない時刻のある予定 → 明日以降の予定。今日の終日の予定は下の「今日」に出る）
function nextEvent(upcoming, today, now) {
  const nowMin = nowMinutes(now);
  return upcoming.find((ev) => ev.date > today || (ev.date === today && !ev.all_day && span(ev)[1] > nowMin));
}

function untilLabel(ev, today, now) {
  const days = Math.round((parseDate(ev.date) - parseDate(today)) / 86400000);
  if (days > 0) return { 1: "明日", 2: "明後日" }[days] || `${days}日後`;
  if (ev.all_day) return "今日";
  const diff = span(ev)[0] - nowMinutes(now);
  if (diff <= 0) return "開催中";
  if (diff < 60) return `あと${diff}分`;
  return `あと${Math.floor(diff / 60)}時間${diff % 60 ? `${diff % 60}分` : ""}`;
}

function nextUpCard(ev, today, now) {
  if (!ev) return `<div class="group next-up app-other"><span class="next-label">次の予定</span><span class="secondary">今後の予定はありません</span></div>`;
  const app = appOf(ev);
  const meta = [ev.location && esc(ev.location), APPS[app]].filter(Boolean).join(" ・ ");
  return `
    <button class="group next-up app-${app}" data-day="${ev.date}" title="この日を表示">
      <span class="next-label">次の予定<span class="until">${untilLabel(ev, today, now)}</span></span>
      <span class="next-title">${esc(ev.title)}</span>
      <span class="next-when">${dateLabel(ev.date)} ${timeText(ev)}</span>
      ${meta ? `<span class="next-meta">${meta}</span>` : ""}
    </button>`;
}

// 重なる予定を横に並べる（重なりのかたまりごとに列を割り当てる）
function layoutColumns(items) {
  const sorted = [...items].sort((a, b) => a.s - b.s || b.e - a.e);
  const out = [];
  let group = [];
  let groupEnd = -1;
  const flush = () => {
    const colEnds = [];
    for (const it of group) {
      let c = colEnds.findIndex((end) => end <= it.s);
      if (c < 0) c = colEnds.push(0) - 1;
      colEnds[c] = it.e;
      it.col = c;
    }
    for (const it of group) it.cols = colEnds.length;
    out.push(...group);
    group = [];
  };
  for (const it of sorted) {
    if (group.length && it.s >= groupEnd) flush();
    groupEnd = group.length ? Math.max(groupEnd, it.e) : it.e;
    group.push(it);
  }
  if (group.length) flush();
  return out;
}

function timeline(list, now) {
  const items = list.map((ev) => { const [s, e] = span(ev); return { ev, s, e }; });
  const startH = Math.min(8, Math.floor(Math.min(...items.map((i) => i.s)) / 60));
  const endH = Math.max(20, Math.ceil(Math.max(...items.map((i) => i.e)) / 60));
  const y = (min) => ((min - startH * 60) * HOUR_PX) / 60;
  let hours = "";
  for (let h = startH; h <= endH; h++) hours += `<div class="hour" style="top:${(h - startH) * HOUR_PX}px"><span>${h}:00</span></div>`;
  const blocks = layoutColumns(items).map(({ ev, s, e, col, cols }) => {
    const short = e - s < 45; // 1 行（開始時刻＋タイトル）
    const meta = e - s >= 90 && cols === 1 ? sourceMeta(ev) : ""; // 場所・連絡元は入りきるときだけ
    return `
      <div class="tl-event app-${appOf(ev)}${short ? " short" : ""}" title="${esc(tooltip(ev))}"
        style="top:${y(s) + 1}px;height:${Math.max(y(e) - y(s) - 2, 18)}px;left:calc(${(col * 100) / cols}% + 1px);width:calc(${100 / cols}% - 2px)">
        <span class="tl-time">${short ? esc(ev.start_time) : timeText(ev)}</span>
        <span class="tl-title">${esc(ev.title)}</span>
        ${meta ? `<span class="tl-meta">${meta}</span>` : ""}
        ${deleteButton(ev)}
      </div>`;
  }).join("");
  const nowMin = now ? nowMinutes(now) : -1;
  const nowLine = nowMin >= startH * 60 && nowMin <= endH * 60 ? `<div class="now-line" style="top:${y(nowMin)}px"></div>` : "";
  return `<div class="timeline"><div class="tl-grid" style="height:${(endH - startH) * HOUR_PX}px">${hours}<div class="tl-events">${blocks}</div>${nowLine}</div></div>`;
}

function dayHeading(iso, today, holiday) {
  const rel = relativeDay(iso, today);
  return `<strong class="${rel === "今日" ? "today" : ""}">${dateLabel(iso)}</strong>
    <span class="sub">${rel ? `<span>${rel}</span>` : ""}${holiday ? `<span class="holiday-name">${esc(holiday)}</span>` : ""}</span>`;
}

function dayPanel(iso, list, today, holiday, now) {
  const allDay = list.filter((ev) => ev.all_day);
  const timed = list.filter((ev) => !ev.all_day);
  let body = "";
  if (allDay.length) {
    body += `<div class="allday-row"><span class="allday-label">終日</span><div class="allday-list">${allDay.map((ev) => `
      <div class="allday-pill app-${appOf(ev)}" title="${esc(tooltip(ev))}"><span class="pill-title">${esc(ev.title)}</span>${deleteButton(ev)}</div>`).join("")}
    </div></div>`;
  }
  if (timed.length) body += timeline(timed, iso === today ? now : null);
  if (!list.length) body += `<div class="row secondary">予定はありません</div>`;
  return `<section class="group day-panel"><div class="day-panel-header">${dayHeading(iso, today, holiday)}</div>${body}</section>`;
}

async function renderEvents() {
  const today = todayIso();
  const now = new Date();
  const cal = state.cal;
  if (!cal.selected) selectDay(today);
  const { first, start, end, weeks } = calendarRange(cal.month);
  const selected = cal.selected;
  const from = toIso(start);
  const to = toIso(end);
  const [events, holidays, upcoming] = await Promise.all([
    api(`/api/events?date_from=${from}&date_to=${to}`),
    holidaysFor(from, to),
    api("/api/events"),
  ]);
  const byDate = groupByDate(events);

  let cells = "";
  for (let i = 0; i < weeks * 7; i++) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    const iso = toIso(d);
    const list = byDate.get(iso) || [];
    const holiday = holidays.get(iso);
    const cls = ["day"];
    if (d.getMonth() !== first.getMonth()) cls.push("outside");
    if (d.getDay() === 0) cls.push("sun");
    if (d.getDay() === 6) cls.push("sat");
    if (holiday) cls.push("holiday");
    if (iso === today) cls.push("today");
    if (iso === selected) cls.push("selected");
    const more = list.length - CHIP_MAX;
    const label = [dateLabel(iso), holiday, iso === today && "今日", list.length && `予定${list.length}件`].filter(Boolean).join(" ");
    cells += `
      <button class="${cls.join(" ")}" data-day="${iso}" aria-pressed="${iso === selected}" aria-label="${esc(label)}">
        <span class="num">${d.getDate()}</span>
        <span class="chips">${holiday ? `<span class="holiday-label">${esc(holiday)}</span>` : ""}${list.slice(0, CHIP_MAX).map(calendarChip).join("")}${more > 0 ? `<span class="more">他${more}件</span>` : ""}</span>
        <span class="dots">${list.slice(0, 3).map((ev) => `<i class="app-${appOf(ev)}"></i>`).join("")}</span>
      </button>`;
  }

  const apps = new Set(events.map(appOf));
  const legend = apps.size
    ? `<div class="legend">${Object.keys(APPS).filter((a) => apps.has(a)).map((a) => `<span class="app-${a}"><i></i>${APPS[a]}</span>`).join("")}</div>`
    : "";
  const dayEvents = byDate.get(selected) || [];
  const holiday = holidays.get(selected);

  return `
    <div class="cal-layout">
      <section class="cal-main">
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
        ${legend}
        <div class="day-list">
          <div class="section-header">${dayHeading(selected, today, holiday)}</div>
          <div class="group">${dayEvents.length ? dayEvents.map((ev) => eventRow(ev)).join("") : `<div class="row secondary">予定はありません</div>`}</div>
        </div>
      </section>
      <aside class="cal-side">
        ${nextUpCard(nextEvent(upcoming, today, now), today, now)}
        ${dayPanel(selected, dayEvents, today, holiday, now)}
      </aside>
    </div>`;
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
  if (q.waiting) {
    const how = llm.available ? "順に精査しています" : "ローカルLLMにつながったら自動で精査";
    items.push(`<span class="item"><span class="light wait"></span>精査待ち ${q.waiting}件（${how}）</span>`);
  }
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
  updateAnalyzeNote();
  if (paste.jobs.some((job) => job.state === "waiting")) renderJobs(); // 「準備ができたら」の表示を最新に
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
      document.body.dataset.tab = state.tab;
      await render(true);
    } else if (t.dataset.day) {
      selectDay(t.dataset.day);
      await render(true);
      $(`.calendar [data-day="${t.dataset.day}"]`)?.focus();
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
    } else if (t.dataset.removeImage !== undefined) {
      removeImage(Number(t.dataset.removeImage));
    } else if (t.dataset.dismissJob) {
      const job = paste.jobs.find((j) => j.id === t.dataset.dismissJob);
      if (job) releaseJob(job);
      paste.jobs = paste.jobs.filter((j) => j !== job);
      renderJobs();
    } else if (t.id === "mark-read") {
      await api("/api/notifications/read", { method: "POST" });
      await Promise.all([render(), refreshStatus()]);
    } else if (t.id === "retry-queue") {
      const res = await api("/api/queue/retry", { method: "POST" });
      toast(`${res.retried}件をもう一度精査します`);
      paste.jobs.forEach((job) => { if (job.state === "failed") job.state = "waiting"; });
      renderJobs();
      pollJobs();
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
function setSheetMode(mode) {
  $$("#sheet-tabs .segment").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("#analyze-form").hidden = mode !== "analyze";
  $("#manual-form").hidden = mode !== "manual";
}

function openSheet(mode) {
  if (mode) setSheetMode(mode);
  const wasHidden = $("#sheet").hidden;
  $("#sheet").hidden = false;
  const dateInput = $("#manual-form [name=date]");
  if (!dateInput.value) dateInput.value = (state.tab === "events" && state.cal.selected) || todayIso();
  if (wasHidden && !$("#analyze-form").hidden) $("#analyze-form textarea").focus();
}
function closeSheet() {
  $("#sheet").hidden = true;
  // 終わった解析の結果は、閉じたら片付ける（精査中のものは残して、次に開いたときにも表示する）
  paste.jobs = paste.jobs.filter((job) => isActiveJob(job) || (releaseJob(job), false));
  renderJobs();
}

$("#open-sheet").addEventListener("click", () => openSheet());
$("#close-sheet").addEventListener("click", closeSheet);
$("#sheet").addEventListener("click", (e) => { if (e.target.id === "sheet") closeSheet(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#sheet").hidden) closeSheet(); });

$$("#sheet-tabs .segment").forEach((btn) => btn.addEventListener("click", (e) => {
  e.preventDefault();
  setSheetMode(btn.dataset.mode);
}));

const RESULT_TAG = { added: "追加", pending: "確認待ち", duplicate: "登録済み", merged: "まとめた", updated: "変更", cancelled: "取り消し", change_pending: "確認待ち", skipped: "見送り" };

// ---------- 貼り付け（文章・画像）→ 裏で解析 ----------
// 画像は CPU だけの PC だと数分かかるので、送ったらすぐ次を貼れるようにし、結果はあとから表示する
const MAX_IMAGES = 4;
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const PASTE_KEY = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘V" : "Ctrl+V";
const paste = { images: [], jobs: [], timer: null }; // images: [{ file, url }] / jobs: 送った解析（新しい順）

const isActiveJob = (job) => job.state === "waiting" || job.state === "running";
function releaseJob(job) { (job.urls || []).forEach((u) => URL.revokeObjectURL(u)); }

function addImages(files) {
  let added = 0;
  for (const file of [...files].filter((f) => f.type.startsWith("image/"))) {
    if (paste.images.length >= MAX_IMAGES) { toast(`画像は${MAX_IMAGES}枚までです`); break; }
    if (file.size > MAX_IMAGE_BYTES) { toast(`画像が大きすぎます（20MBまで）: ${file.name}`); continue; }
    paste.images.push({ file, url: URL.createObjectURL(file) });
    added++;
  }
  renderThumbs();
  return added;
}

function removeImage(index) {
  const [img] = paste.images.splice(index, 1);
  if (img) URL.revokeObjectURL(img.url);
  renderThumbs();
}

function renderThumbs() {
  const box = $("#thumbs");
  box.hidden = !paste.images.length;
  box.innerHTML = paste.images.map((img, i) => `
    <div class="thumb"><img src="${img.url}" alt="${esc(img.file.name || "貼り付けた画像")}">
      <button type="button" class="thumb-remove" data-remove-image="${i}" aria-label="この画像を外す" title="外す">×</button></div>`).join("");
  $("#file-names").textContent = paste.images.length
    ? `画像 ${paste.images.length}枚（あと${MAX_IMAGES - paste.images.length}枚）`
    : `${PASTE_KEY}・ドラッグでも追加できます`;
  updateAnalyzeNote();
}

function updateAnalyzeNote() {
  const llm = state.status?.llm;
  const noVision = paste.images.length > 0 && llm?.available && !llm.vision_model;
  const note = $("#analyze-note");
  note.classList.toggle("warn", !!noVision);
  note.textContent = noVision
    ? "今のローカルLLMは画像を読めないため、画像は精査されません（画像対応のモデルを入れてください）"
    : "ローカルLLMで精査し、予定が見つかれば登録します。画像は数分かかることがあります（閉じても続きます）。";
}

function elapsedLabel(since) {
  const sec = Math.max(0, Math.floor((Date.now() - since) / 1000));
  return `${Math.floor(sec / 60)}:${pad2(sec % 60)}`;
}

function jobStatus(job) {
  const spin = `<span class="spinner gray"></span>`;
  switch (job.state) {
    case "running":
      return `<div class="line">${spin}ローカルLLMで精査中… ${elapsedLabel(job.runningSince || Date.now())}</div>`;
    case "waiting":
      if (state.status && !state.status.llm.available) return `<div class="line">${spin}ローカルLLMの準備ができたら精査します</div>`;
      return `<div class="line">${spin}${job.ahead ? `順番待ち（先に${job.ahead}件）` : "まもなく精査します"}</div>`;
    case "done": {
      const rs = job.outcome.results;
      if (!rs.length) return `<div class="line">${esc(job.outcome.skipped || "予定は見つかりませんでした")}</div>`;
      return rs.map((r) => `<div class="job-result"><span class="result-tag ${r.status}">${RESULT_TAG[r.status]}</span><span>${esc(r.event.label)}</span></div>`).join("");
    }
    case "failed":
      return `<div class="line error">精査できませんでした${job.error ? `（${esc(job.error)}）` : ""}</div>
        <div class="line"><button type="button" class="text-button" id="retry-queue">もう一度精査する</button></div>`;
    default:
      return `<div class="line">結果は「通知」タブで確認できます</div>`;
  }
}

function renderJobs() {
  const box = $("#analyze-jobs");
  if (!paste.jobs.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="group">${paste.jobs.map((job) => {
    const n = job.urls?.length || 0;
    const title = job.text ? esc(job.text.split("\n")[0].slice(0, 60)) : n ? `画像${n > 1 ? ` ${n}枚` : ""}` : "貼り付けたメッセージ";
    return `<div class="row job-row">
      ${n ? `<div class="job-thumbs">${job.urls.map((u) => `<img src="${u}" alt="">`).join("")}</div>` : ""}
      <div class="job-body"><div class="job-title">${title}</div><div class="job-status">${jobStatus(job)}</div></div>
      ${isActiveJob(job) ? "" : `<button type="button" class="text-button job-dismiss" data-dismiss-job="${esc(job.id)}" aria-label="結果を閉じる" title="閉じる">×</button>`}
    </div>`;
  }).join("")}</div>`;
}

function jobFinished(job) {
  if (job.state === "done") {
    const rs = job.outcome.results;
    toast(rs.length ? `${RESULT_TAG[rs[0].status]}: ${rs[0].event.label}${rs.length > 1 ? `（ほか${rs.length - 1}件）` : ""}` : "貼り付けた内容から予定は見つかりませんでした");
  } else if (job.state === "failed") {
    toast("貼り付けた内容を精査できませんでした");
  }
  render();
  refreshStatus();
}

async function pollJobs() {
  clearTimeout(paste.timer);
  const active = paste.jobs.filter(isActiveJob);
  if (!active.length) return;
  await Promise.all(active.map(async (job) => {
    try {
      const next = await api(`/api/analyze/jobs/${encodeURIComponent(job.id)}`);
      if (next.state === "running" && job.state !== "running") job.runningSince = Date.now();
      Object.assign(job, next);
      if (!isActiveJob(job)) jobFinished(job);
    } catch (_) { /* サーバー停止中など。次の確認でやり直す */ }
  }));
  renderJobs();
  paste.timer = setTimeout(pollJobs, 3000);
}

$("#image-input").addEventListener("change", (e) => {
  addImages(e.target.files);
  e.target.value = "";
});

$("#analyze-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const textarea = $("textarea", form);
  if (!textarea.value.trim() && !paste.images.length) {
    toast("メッセージを入力するか、画像を貼り付けてください");
    return;
  }
  const body = new FormData();
  body.append("text", textarea.value);
  paste.images.forEach((img) => body.append("images", img.file, img.file.name || "image.png"));
  const button = $("button[type=submit]", form);
  button.disabled = true;
  try {
    const job = await api("/api/analyze/jobs", { method: "POST", body });
    // 画像の URL は結果の表示に使うので、ここでは解放しない（結果を閉じたときに解放する）
    paste.jobs.unshift({ ...job, urls: paste.images.map((img) => img.url), text: textarea.value.trim() });
    paste.images = [];
    form.reset();
    renderThumbs();
    renderJobs();
    refreshStatus();
    pollJobs();
  } catch (err) {
    toast(err.message);
  } finally {
    button.disabled = false;
  }
});

// どの画面でも Ctrl+V で画像を貼り付けたら、解析シートを開いて追加する
const isEditable = (el) => !!el?.closest?.("input, textarea, select, [contenteditable]");
document.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (files.length) {
    e.preventDefault();
    openSheet("analyze");
    const n = addImages(files);
    if (n) toast(n > 1 ? `画像を${n}枚追加しました` : "画像を追加しました");
    return;
  }
  // シートを閉じているときに文章を貼り付けたら、解析シートを開いて本文に入れる
  if ($("#sheet").hidden && !isEditable(e.target)) {
    const text = e.clipboardData?.getData("text/plain") || "";
    if (!text.trim()) return;
    e.preventDefault();
    openSheet("analyze");
    const textarea = $("#analyze-form textarea");
    textarea.value = textarea.value ? `${textarea.value}\n${text}` : text;
  }
});

// 画像ファイルを画面にドラッグ＆ドロップしても追加できる
let dragDepth = 0;
const draggingFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragenter", (e) => {
  if (!draggingFiles(e)) return;
  e.preventDefault();
  dragDepth++;
  $("#drop-overlay").hidden = false;
});
document.addEventListener("dragover", (e) => {
  if (!draggingFiles(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
});
document.addEventListener("dragleave", (e) => {
  if (!draggingFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $("#drop-overlay").hidden = true;
});
document.addEventListener("drop", (e) => {
  if (!draggingFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  $("#drop-overlay").hidden = true;
  const images = [...e.dataTransfer.files].filter((f) => f.type.startsWith("image/"));
  if (!images.length) { toast("画像ファイルをドロップしてください"); return; }
  openSheet("analyze");
  addImages(images);
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
  document.body.dataset.tab = state.tab;
  $("#today-label").textContent = `${now.getMonth() + 1}月${now.getDate()}日 ${WEEKDAYS[now.getDay()]}曜日`;
  refreshStatus().then(() => render(true));
  renderThumbs();
  // 画面を開き直したときも、まだ終わっていない解析を表示する
  api("/api/analyze/jobs").then((jobs) => {
    paste.jobs = jobs.map((job) => ({ ...job, urls: [], text: "" }));
    renderJobs();
    pollJobs();
  }).catch(() => {});
  setInterval(() => { refreshStatus(); if ($("#sheet").hidden) render(); }, 5000);
})();
