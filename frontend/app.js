(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const WEEK = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const STATUS = { signed: "已签到", pending: "待签", failed: "失败", unknown: "未知" };
  let timer = null;
  let editing = null;     // 正在编辑的账号 id（null 表示添加）
  let sched = { id: null, day: null, label: "", busy: false, mode: "day" };
  let accountsCache = [];

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const show = (id, on) => { $(id).hidden = !on; };

  function toast(msg, bad) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "show" + (bad ? " bad" : "");
    clearTimeout(toast.h);
    toast.h = setTimeout(() => { t.className = ""; }, bad ? 7000 : 3000);
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
    if (init.method !== "GET") init.headers["X-Requested-With"] = "portal";
    if (opts.body) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    let r;
    try { r = await fetch(path.replace(/^\//, ""), init); }
    catch { throw { status: 0, kind: "network", message: "无法连接服务器" }; }
    let data = {};
    try { data = await r.json(); } catch { /* 非 JSON */ }
    if (!r.ok) {
      const e = data.error || {};
      throw { status: r.status, kind: e.kind || "http", message: e.message || "请求失败（" + r.status + "）", trace: e.trace };
    }
    return data;
  }

  // ---------- 时间（北京时间） ----------
  const fmt = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour12: false,
    hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const fmtHM = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour12: false,
    hour: "2-digit", minute: "2-digit" });
  const hms = (sec) => fmt.format(new Date(sec * 1000));
  const hm = (sec) => fmtHM.format(new Date(sec * 1000));
  const chinaToday = () => new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai" }).format(new Date());
  function shift(d, n) {
    const t = new Date(d + "T00:00:00Z");
    t.setUTCDate(t.getUTCDate() + n);
    return t.toISOString().slice(0, 10);
  }
  function weekStart(d) {   // 该日所在周的周一
    const t = new Date(d + "T00:00:00Z");
    return shift(d, -((t.getUTCDay() + 6) % 7));
  }
  function weekLabel(d) {
    const a = weekStart(d), b = shift(a, 6);
    const md = (x) => `${+x.split("-")[1]} 月 ${+x.split("-")[2]} 日`;
    return `${md(a)} – ${md(b)}` + (weekStart(chinaToday()) === a ? "（本周）" : "");
  }
  function dayLabel(d) {
    const t = new Date(d + "T00:00:00Z");
    const [, m, dd] = d.split("-");
    return `${+m} 月 ${+dd} 日 ${WEEK[t.getUTCDay()]}` + (d === chinaToday() ? "（今天）" : "");
  }
  const isoHM = (iso) => iso.slice(11, 16);

  // ---------- 视图 ----------
  // 登录由平台入口负责：会话失效时回到入口登录，登录后自动回到本应用。
  function toPortalLogin() {
    clearInterval(timer); timer = null;
    location.href = "/?next=" + encodeURIComponent(location.pathname);
  }

  async function viewMain() {
    await refresh();
    clearInterval(timer);
    timer = setInterval(() => { if (!document.hidden) refresh(); }, 10000);
  }

  async function refresh() {
    try {
      const [a, ev] = await Promise.all([api("/api/accounts"), api("/api/events?limit=60")]);
      accountsCache = a.accounts;
      renderAccounts(a.accounts);
      renderEvents(ev.events);
    } catch (e) {
      if (e.status === 401) toPortalLogin();
      else toast(e.message, true);
    }
  }

  function planRow(p) {
    const li = el("li");
    const left = el("span", "pn", p.name);
    const st = el("span", "pill-s " + p.status, STATUS[p.status] || p.status);
    left.append(st);
    let info = `${hm(p.start)}–${hm(p.end)}`;
    if (p.status === "pending") info += ` · 计划 ${hms(p.planned_at)}`;
    li.append(left, el("span", "pm", info));
    if (p.message && p.status !== "pending") li.title = p.message;
    return li;
  }

  function accountCard(a) {
    const li = el("li", "acc" + (a.enabled ? "" : " off"));
    const head = el("div", "acc-head");
    const who = el("div");
    who.append(el("div", "acc-name", a.label), el("div", "acc-num", a.number));
    const sw = el("button", "switch");
    sw.type = "button"; sw.setAttribute("role", "switch");
    sw.setAttribute("aria-checked", String(a.enabled));
    sw.setAttribute("aria-label", `${a.label} 自动签到开关`);
    sw.addEventListener("click", () => toggle(a, sw));
    head.append(who, sw);
    li.append(head);

    const bad = a.state === "auth_failed" || a.state === "captcha" || a.state === "error";
    let stateText = a.enabled ? (a.state_msg || "等待首次检查…") : "自动签到已关闭";
    if (a.state === "auth_failed") stateText = "已暂停：" + a.state_msg + "（修改密码后重新开启）";
    if (a.state === "captcha") stateText = "已暂停：" + a.state_msg;
    li.append(el("p", "acc-state" + (a.enabled && bad ? " bad" : ""), stateText));

    if (a.enabled && a.plans.length) {
      const ul = el("ul", "plans");
      a.plans.forEach((p) => ul.append(planRow(p)));
      li.append(ul);
    }

    const act = el("div", "acc-actions");
    const mk = (text, fn, cls) => {
      const b = el("button", "btn ghost" + (cls ? " " + cls : ""), text);
      b.type = "button"; b.addEventListener("click", fn); return b;
    };
    act.append(mk("课表 / 手动签到", () => openSchedule(a)));
    if (a.enabled) act.append(mk("立即检查", () => checkNow(a)));
    act.append(mk("编辑", () => openEdit(a)), mk("删除", () => removeAccount(a), "danger"));
    li.append(act);
    return li;
  }

  function renderAccounts(list) {
    $("accounts").replaceChildren(...list.map(accountCard));
    show("empty", list.length === 0);
  }

  function renderEvents(list) {
    $("events").replaceChildren(...list.map((e) => {
      const li = el("li", e.kind);
      li.append(el("span", "t", hms(e.ts)));
      const m = el("span", "m", (e.account ? `[${e.account}] ` : "") + e.message);
      li.append(m);
      return li;
    }));
  }

  // ---------- 账号操作 ----------
  async function toggle(a, sw) {
    sw.disabled = true;
    try {
      await api("/api/accounts/" + a.id, { method: "PATCH", body: { enabled: !a.enabled } });
      toast(a.enabled ? "已关闭自动签到" : "已开启，正在检查可签课程…");
    } catch (e) { toast(e.message, true); }
    await refresh();
  }

  async function checkNow(a) {
    try { await api(`/api/accounts/${a.id}/check`, { method: "POST" }); toast("已触发检查"); }
    catch (e) { toast(e.message, true); }
    setTimeout(refresh, 2500);
  }

  function confirmBox(title, text, okText) {
    return new Promise((resolve) => {
      $("dlg-title").textContent = title;
      $("dlg-text").textContent = text;
      $("dlg-ok").textContent = okText;
      const d = $("dlg");
      d.returnValue = "";
      d.onclose = () => resolve(d.returnValue === "ok");
      d.showModal();
    });
  }

  async function removeAccount(a) {
    if (!await confirmBox("删除账号", `将删除 ${a.label}（${a.number}）及其保存的密码，无法恢复。`, "删除")) return;
    try { await api("/api/accounts/" + a.id, { method: "DELETE" }); toast("已删除"); }
    catch (e) { toast(e.message, true); }
    await refresh();
  }

  function openEdit(a) {
    editing = a ? a.id : null;
    $("acc-title").textContent = a ? "编辑账号" : "添加账号";
    $("acc-label").value = a ? (a.label === a.number ? "" : a.label) : "";
    $("acc-number").value = a ? a.number : "";
    $("acc-pw").value = "";
    $("acc-pw").required = !a;
    $("acc-pw-hint").textContent = a ? "（留空则不修改）" : "";
    $("acc-enabled").checked = false;
    show("acc-enable-row", !a);
    $("dlg-acc").showModal();
    $(a ? "acc-label" : "acc-number").focus();
  }
  $("b-add").addEventListener("click", () => openEdit(null));
  $("acc-cancel").addEventListener("click", () => $("dlg-acc").close());
  $("f-acc").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("acc-save"); btn.disabled = true;
    try {
      const body = { label: $("acc-label").value, number: $("acc-number").value };
      if ($("acc-pw").value) body.password = $("acc-pw").value;
      if (editing == null) {
        body.enabled = $("acc-enabled").checked;
        await api("/api/accounts", { method: "POST", body });
      } else {
        await api("/api/accounts/" + editing, { method: "PATCH", body });
      }
      $("dlg-acc").close();
      toast("已保存");
      await refresh();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
  });

  // ---------- 课表 / 手动签到 ----------
  function openSchedule(a) {
    sched = { id: a.id, day: chinaToday(), label: a.label, busy: false, mode: sched.mode };
    setModeUi(sched.mode);
    $("sched-title").textContent = `${a.label} · 课表`;
    $("dlg-sched").showModal();
    loadSchedule();
  }

  function lessonItem(l) {
    const li = el("li", "lesson");
    const r1 = el("div", "row1");
    r1.append(el("span", "name", l.name), el("span", "time", `${isoHM(l.start)}–${isoHM(l.end)}`));
    const r3 = el("div", "row3");
    r3.append(el("span", "badge " + l.state, { signed: "已签到", unsigned: "未签到", unknown: "状态未知" }[l.state]));
    if (l.state === "unsigned") {
      if (l.can_sign) {
        const b = el("button", "btn primary", "签到");
        b.type = "button"; b.addEventListener("click", () => manualSign(l));
        r3.append(b);
      } else r3.append(el("span", "hint", "课前 10 分钟至下课可签"));
    }
    li.append(r1);
    const meta = [l.teacher, l.room].filter(Boolean).join(" · ");
    if (meta) li.append(el("div", "meta", meta));
    li.append(r3);
    return li;
  }

  // 周视图：一周内每天的课程、签到状态与计划签到时刻
  function weekLesson(l, day) {
    const li = el("li", "wl");
    const r1 = el("div", "row1");
    r1.append(el("span", "time", `${isoHM(l.start)}–${isoHM(l.end)}`), el("span", "name", l.name),
      el("span", "badge " + l.state, { signed: "已签到", unsigned: "未签到", unknown: "状态未知" }[l.state]));
    li.append(r1);
    const meta = [l.teacher, l.room].filter(Boolean).join(" · ");
    if (meta) li.append(el("div", "meta", meta));
    const notes = [];
    if (l.planned_at) notes.push(`计划 ${hms(l.planned_at)} 签到`);
    if (l.same_slot > 1) notes.push(`同名同时段共 ${l.same_slot} 个班，共用同一签到时刻`);
    if (notes.length) li.append(el("div", "hint", notes.join(" · ")));
    if (l.can_sign) {
      const b = el("button", "btn primary", "签到");
      b.type = "button"; b.addEventListener("click", () => manualSign(l, day));
      li.append(b);
    }
    return li;
  }

  function renderWeek(res) {
    const s = res.summary;
    $("week-sum").textContent = `本周共 ${s.total} 节 · 已签 ${s.signed} · 未签 ${s.unsigned}` +
      (s.unknown ? ` · 状态未知 ${s.unknown}` : "");
    show("week-sum", true);
    $("week").replaceChildren(...res.days.map((d) => {
      const sec = el("section", "wday" + (d.date === chinaToday() ? " today" : ""));
      const h = el("h4", null, dayLabel(d.date));
      h.append(el("span", "cnt", d.lessons.length ? `${d.lessons.length} 节` : "无课"));
      sec.append(h);
      if (d.lessons.length) {
        const ul = el("ul", "wlist");
        d.lessons.forEach((l) => ul.append(weekLesson(l, d.date)));
        sec.append(ul);
      }
      return sec;
    }));
  }

  function setModeUi(mode) {
    $("m-day").setAttribute("aria-pressed", String(mode === "day"));
    $("m-week").setAttribute("aria-pressed", String(mode === "week"));
    $("d-prev").setAttribute("aria-label", mode === "week" ? "上一周" : "前一天");
    $("d-next").setAttribute("aria-label", mode === "week" ? "下一周" : "后一天");
  }
  function setMode(mode) {
    sched.mode = mode;
    setModeUi(mode);
    loadSchedule();
  }

  async function loadSchedule() {
    if (sched.busy) return;
    sched.busy = true;
    const week = sched.mode === "week";
    $("d-today").textContent = week ? weekLabel(sched.day) : dayLabel(sched.day);
    show("sched-err", false);
    show("lessons", !week); show("week", week);
    if (!week) show("week-sum", false);
    try {
      if (week) {
        show("sched-empty", false);
        renderWeek(await api(`/api/accounts/${sched.id}/week?date=${sched.day}`));
        return;
      }
      const res = await api(`/api/accounts/${sched.id}/schedule?date=${sched.day}`);
      $("lessons").replaceChildren(...res.lessons.map(lessonItem));
      show("sched-empty", res.lessons.length === 0);
    } catch (e) {
      $("lessons").replaceChildren(); $("week").replaceChildren(); show("week-sum", false); show("sched-empty", false);
      $("sched-err").textContent = e.message + (e.trace ? "\n" + e.trace.join("\n") : "");
      show("sched-err", true);
    } finally { sched.busy = false; }
  }

  async function manualSign(l, day) {
    day = day || sched.day;
    if (!await confirmBox("确认签到", `${sched.label}：${l.name}（${isoHM(l.start)}–${isoHM(l.end)}）将提交一次签到。`, "签到")) return;
    try {
      const r = await api(`/api/accounts/${sched.id}/sign`, { method: "POST", body: { date: day, id: l.id } });
      toast(r.message, r.status !== "success");
    } catch (e) { toast(e.message, true); }
    await loadSchedule();
    refresh();
  }

  $("sched-close").addEventListener("click", () => $("dlg-sched").close());
  const step = () => (sched.mode === "week" ? 7 : 1);
  $("d-prev").addEventListener("click", () => { sched.day = shift(sched.day, -step()); loadSchedule(); });
  $("d-next").addEventListener("click", () => { sched.day = shift(sched.day, step()); loadSchedule(); });
  $("m-day").addEventListener("click", () => { if (sched.mode !== "day") setMode("day"); });
  $("m-week").addEventListener("click", () => { if (sched.mode !== "week") setMode("week"); });
  $("d-today").addEventListener("click", () => { sched.day = chinaToday(); loadSchedule(); });
  $("d-refresh").addEventListener("click", loadSchedule);

  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

  // ---------- 启动 ----------
  viewMain();
})();
