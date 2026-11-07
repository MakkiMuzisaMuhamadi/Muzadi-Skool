/* Offline Marks app - screens and actions.
 * Storage and sending live in offline-marks-store.js (window.MuzadiMarks).
 */
(function () {
  "use strict";
  const MM = window.MuzadiMarks;
  const body = document.body;
  const URLS = {
    me: body.dataset.urlMe, lists: body.dataset.urlLists, pkg: body.dataset.urlPackage,
    sync: body.dataset.urlSync, dashboard: body.dataset.urlDashboard, entry: body.dataset.urlEntry,
    login: body.dataset.urlLogin, sw: body.dataset.urlSw, app: location.pathname,
  };
  const main = document.getElementById("omMain");
  const netEl = document.getElementById("omNet");
  const backBtn = document.getElementById("omBack");
  const titleEl = document.getElementById("omTitle");
  const subEl = document.getElementById("omSub");
  const toastEl = document.getElementById("omToast");

  const state = { me: null, user: null, signedOut: false, filter: "all", busy: false };
  const STATUS = {
    clean: ["Downloaded", "clean"], draft: ["Draft on phone", "draft"], ready: ["Waiting to send", "ready"],
    sent: ["Sent", "sent"], attention: ["Needs attention", "attention"],
  };

  // ------------------------------------------------------------ small helpers
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const online = () => navigator.onLine;
  let toastTimer = null;
  function toast(msg, ms) {
    toastEl.textContent = msg; toastEl.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => toastEl.classList.remove("show"), ms || 3200);
  }
  function ago(iso) {
    if (!iso) return "never";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " day(s) ago";
  }
  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }) + " " +
           d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  function setNet() {
    netEl.textContent = online() ? "Online" : "Offline";
    netEl.className = "om-net " + (online() ? "on" : "off");
  }
  function loginUrl() { return URLS.login + "?next=" + encodeURIComponent(URLS.app); }
  async function getJSON(url) {
    const res = await fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json", "X-Requested-With": "XMLHttpRequest" } });
    const type = res.headers.get("content-type") || "";
    if (res.redirected || !type.includes("application/json")) { const e = new Error("signed_out"); e.auth = true; throw e; }
    const data = await res.json();
    if (res.status === 401) { const e = new Error("signed_out"); e.auth = true; throw e; }
    if (!res.ok || data.ok === false) { const e = new Error(data.error || ("Error " + res.status)); e.status = res.status; throw e; }
    return data;
  }
  function askBackgroundSync() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.ready.then((reg) => { if (reg.sync) reg.sync.register("muzadi-marks-sync").catch(() => {}); }).catch(() => {});
  }

  // ------------------------------------------------------------ who is signed in
  async function refreshMe() {
    if (!online()) return;
    try {
      const data = await getJSON(URLS.me);
      state.me = data; state.signedOut = false;
      state.user = { id: data.user.id, name: data.user.name, is_teacher: data.user.is_teacher, school: data.school.name, term: data.term };
      await MM.setMeta("lastUser", state.user);
      await MM.setMeta("csrf", data.csrf_token);
    } catch (e) {
      if (e.auth) state.signedOut = true;
    }
  }

  // ------------------------------------------------------------ routing
  function route() {
    const m = location.hash.match(/^#list=(.+)$/);
    if (m) renderList(decodeURIComponent(m[1])); else renderHome();
  }
  function openList(id) { location.hash = "list=" + encodeURIComponent(id); }
  backBtn.addEventListener("click", () => { location.hash = ""; });
  document.getElementById("omHome").addEventListener("click", (e) => { e.preventDefault(); location.hash = ""; });
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------ HOME
  async function renderHome() {
    backBtn.hidden = true;
    titleEl.textContent = "Offline Marks";
    subEl.textContent = state.user ? state.user.school || "Muzadi School System" : "Muzadi School System";
    const uid = state.user && state.user.id;
    const lists = uid ? await MM.listsForUser(uid) : [];
    const others = (await MM.allLists()).filter((l) => l.userId !== uid && MM.pendingCount(l));
    lists.sort((a, b) => (a.title + a.exam).localeCompare(b.title + b.exam));

    const count = (st) => lists.filter((l) => l.status === st).length;
    const ready = count("ready");
    let html = "";

    html += `<div class="om-hello"><div><h1>${state.user ? "Hello, " + esc(state.user.name) : "Offline Marks"}</h1>
      <p>${state.user && state.user.term ? esc(state.user.term.name) + " · " : ""}Enter marks without internet. They are kept on this phone until you send them.</p></div></div>`;

    if (!online()) html += `<p class="om-note warn">You are offline. You can enter marks; they are saved on this phone and sent when you are back online.</p>`;
    if (state.signedOut) html += `<p class="om-note bad">You are signed out. <a href="${esc(loginUrl())}">Sign in</a> to download lists or send marks. Marks on this phone are safe.</p>`;
    if (!uid && !state.signedOut && !online()) html += `<p class="om-note info">Open this page once while online and signed in, then download your lists.</p>`;
    if (others.length) html += `<p class="om-note warn">${others.length} list(s) on this phone belong to another account and have unsent marks. That person should sign in on this phone to send them.</p>`;

    html += `<div class="om-actions">`;
    if (uid && online() && !state.signedOut && state.user.is_teacher) html += `<button class="om-btn primary" id="omDownload">Download my lists</button>`;
    if (ready && online()) html += `<button class="om-btn light" id="omSendAll">Send waiting lists (${ready})</button>`;
    if (online()) html += `<a class="om-btn ghost" href="${esc(URLS.entry)}">Enter Marks (online)</a>`;
    html += `</div>`;

    if (!lists.length) {
      html += `<div class="om-card om-empty"><img src="${esc(document.querySelector('.om-brand img').src)}" alt="">
        <h2>No mark lists on this phone yet</h2>
        <ol class="om-steps">
          <li>While you have internet, tap <b>Download my lists</b> (or on <b>Enter Marks</b>, load a list and tap <b>Save for offline</b>).</li>
          <li>Enter marks anytime, even with no data or Wi-Fi. Each mark is saved on the phone as a draft.</li>
          <li>Tap <b>Send to school</b>. If you are offline, it sends by itself when the connection returns.</li>
        </ol></div>`;
    } else {
      const tabs = [["all", "All", lists.length], ["draft", "Drafts", count("draft")], ["ready", "Waiting", ready],
                    ["attention", "Problems", count("attention")], ["sent", "Sent", count("sent")], ["clean", "Downloaded", count("clean")]];
      html += `<div class="om-tabs" role="tablist">` + tabs.filter((t) => t[0] === "all" || t[2]).map((t) =>
        `<button class="om-tab ${state.filter === t[0] ? "active" : ""}" data-filter="${t[0]}" role="tab" aria-selected="${state.filter === t[0]}">${t[1]}<b>${t[2]}</b></button>`).join("") + `</div>`;
      const shown = lists.filter((l) => state.filter === "all" || l.status === state.filter);
      if (!shown.length) html += `<p class="om-note info">No lists here.</p>`;
      shown.forEach((l) => {
        const p = MM.progress(l);
        const pct = p.total ? Math.round(100 * p.entered / p.total) : 0;
        const [label, cls] = STATUS[l.status] || STATUS.clean;
        const pend = MM.pendingCount(l);
        const lockedAll = l.students.length && l.students.every((s) => s.locked);
        html += `<button class="om-list" data-open="${esc(l.id)}">
          <div class="om-list-top"><div><h3>${esc(l.title)}</h3><div class="om-meta">${esc(l.exam)} · ${esc(l.term)} · max ${esc(l.max_mark)}</div></div>
          <span class="om-badge ${cls}">${label}</span></div>
          <div class="om-bar"><i style="width:${pct}%"></i></div>
          <div class="om-list-foot"><span>${p.entered}/${p.total} entered${p.absent ? " · " + p.absent + " absent" : ""}${pend ? " · <b>" + pend + " on phone</b>" : ""}</span>
          <span>${lockedAll ? "Locked · " : ""}Downloaded ${ago(l.downloadedAt)}</span></div></button>`;
      });
    }
    main.innerHTML = html;

    main.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => openList(b.dataset.open)));
    main.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => { state.filter = b.dataset.filter; renderHome(); }));
    const dl = document.getElementById("omDownload"); if (dl) dl.addEventListener("click", downloadMyLists);
    const sa = document.getElementById("omSendAll"); if (sa) sa.addEventListener("click", sendAllReady);
  }

  async function downloadMyLists() {
    if (state.busy) return;
    state.busy = true;
    const btn = document.getElementById("omDownload");
    if (btn) { btn.disabled = true; btn.textContent = "Getting your lists…"; }
    let ok = 0, failed = 0, total = 0;
    try {
      const data = await getJSON(URLS.lists);
      if (!data.lists.length) { toast(data.message || "No mark lists found for this term."); return; }
      total = data.lists.length;
      for (const item of data.lists) {
        if (btn) btn.textContent = `Downloading ${ok + failed + 1} of ${total}…`;
        try {
          const q = new URLSearchParams(item.params).toString();
          const pkg = await getJSON(URLS.pkg + "?" + q);
          await MM.saveDownloaded(state.user.id, pkg.list);
          ok++;
        } catch (e) {
          if (e.auth) throw e;
          failed++;
        }
      }
      toast(`${ok} list(s) ready on this phone` + (failed ? `, ${failed} could not be downloaded` : "") + ".", 4500);
    } catch (e) {
      if (e.auth) { state.signedOut = true; toast("Please sign in again."); }
      else toast(online() ? "Could not download: " + e.message : "You are offline.");
    } finally {
      state.busy = false;
      renderHome();
    }
  }

  async function sendAllReady() {
    if (!state.user) return;
    toast("Sending…");
    const results = await MM.syncReady(state.user.id);
    report(results);
    renderHome();
  }

  function report(results) {
    if (!results.length) return;
    if (results.some((r) => r.auth)) { state.signedOut = true; toast("Sign in again to send. Your marks are safe on this phone.", 5000); return; }
    if (results.some((r) => r.offline)) { toast("No connection. Will send automatically when you are back online.", 4500); return; }
    const bad = results.filter((r) => !r.ok).length;
    toast(bad ? `${results.length - bad} list(s) sent, ${bad} need attention.` : `${results.length} list(s) sent to the school.`, 4500);
  }

  // ------------------------------------------------------------ LIST
  async function renderList(id) {
    const list = await MM.getList(id);
    if (!list || (state.user && list.userId !== state.user.id)) { location.hash = ""; return; }
    backBtn.hidden = false;
    titleEl.textContent = list.title;
    subEl.textContent = list.exam + " · " + list.term;

    const p = MM.progress(list);
    const pend = MM.pendingCount(list);
    const [label, cls] = STATUS[list.status] || STATUS.clean;
    const lockedAll = list.students.length && list.students.every((s) => s.locked);

    let html = `<div class="om-card om-head"><div class="om-list-top"><h2>${esc(list.title)}</h2><span class="om-badge ${cls}" id="omStatus">${label}</span></div>
      <div class="om-chips"><span class="om-chip">${esc(list.exam_name || list.exam)}</span><span class="om-chip">${esc(list.term)}</span>
      <span class="om-chip">Max mark ${esc(list.max_mark)}</span><span class="om-chip">Downloaded ${ago(list.downloadedAt)}</span></div>`;
    (list.windows || []).forEach((w) => {
      const bits = [];
      if (w.stream && list.windows.length > 1) bits.push(esc(w.stream) + ":");
      bits.push({ DRAFT: "Draft", SUBMITTED: "Submitted for approval", APPROVED: "Approved & locked", RETURNED: "Returned for correction" }[w.status] || w.status);
      if (w.deadline) bits.push((w.past_deadline ? "deadline passed " : "deadline ") + esc(fmtDate(w.deadline)));
      html += `<div class="om-meta" style="margin-top:6px">${bits.join(" · ")}${w.review_note ? ` — <i>${esc(w.review_note)}</i>` : ""}</div>`;
    });
    html += `<div class="om-actions" style="margin:10px 0 0">`;
    if (online() && !state.signedOut) html += `<button class="om-btn small light" id="omRefresh">Refresh from school</button>`;
    html += `<button class="om-btn small danger" id="omRemove">Remove from phone</button></div></div>`;

    if (list.lastMessage) html += `<p class="om-note ${list.status === "attention" ? "bad" : list.status === "sent" ? "ok" : "info"}">${esc(list.lastMessage)}</p>`;
    if (state.signedOut) html += `<p class="om-note bad">You are signed out. <a href="${esc(loginUrl())}">Sign in</a> to send. Marks on this phone are safe.</p>`;
    if (lockedAll) html += `<p class="om-note warn">This list is locked: ${esc(list.students[0].lock_reason || "editing is closed")}. You can only view it.</p>`;
    else if (!list.term_open) html += `<p class="om-note warn">Marks entry for this term is finalized.</p>`;

    html += `<div class="om-counts"><div><b id="cEntered">${p.entered}</b><span>Entered</span></div><div><b id="cAbsent">${p.absent}</b><span>Absent</span></div>
      <div><b id="cMissing">${p.missing}</b><span>Not entered</span></div><div><b id="cPending">${pend}</b><span>On phone</span></div></div>
      <input type="search" class="om-search" id="omSearch" placeholder="Search name, candidate ID or stream" aria-label="Search students">
      <div id="omRows">`;

    list.students.forEach((s) => {
      const value = MM.valueFor(list, s);
      const edited = !!list.edits[s.id];
      const conflict = (list.conflicts || {})[s.id];
      const err = (list.errors || {})[s.id];
      const rowCls = s.locked ? "locked" : conflict || err ? "problem" : edited ? "edited" : "";
      html += `<div class="om-row ${rowCls}" data-sid="${s.id}" data-search="${esc((s.name + " " + s.candidate_id + " " + s.stream).toLowerCase())}">
        <div><div class="om-name">${esc(s.name)}</div><div class="om-sub">${esc(s.candidate_id)}${s.stream ? " · " + esc(s.stream) : ""}</div></div>
        <div class="om-mark"><input class="om-input" type="text" inputmode="decimal" enterkeyhint="next" autocomplete="off" spellcheck="false"
          value="${esc(value)}" data-sid="${s.id}" aria-label="Mark for ${esc(s.name)}" ${s.locked ? "disabled" : ""}>
          ${s.locked ? "" : `<button type="button" class="om-abs" data-abs="${s.id}" tabindex="-1" aria-label="Absent">A</button>`}</div>
        <div class="om-slot">${rowMessage(s, edited, conflict, err)}</div></div>`;
    });
    html += `</div>`;
    main.innerHTML = html;
    renderDock(list);
    wireList(list.id, list.max_mark);
  }

  function rowMessage(s, edited, conflict, err) {
    if (s.locked) return `<div class="om-rowmsg lock">${esc(s.lock_reason || "Locked")}</div>`;
    if (conflict) return `<div class="om-conflict">School has <b>${esc(conflict.server || "blank")}</b>, you entered <b>${esc(conflict.mine || "blank")}</b>.
      <button class="om-btn small light" data-keep="${s.id}">Keep mine</button><button class="om-btn small ghost" data-useserver="${s.id}">Use ${esc(conflict.server || "blank")}</button></div>`;
    if (err) return `<div class="om-rowmsg bad">Not saved: ${esc(err)}</div>`;
    if (edited) return `<div class="om-rowmsg edited">Saved on phone · not sent yet</div>`;
    return "";
  }

  function renderDock(list) {
    let dock = document.getElementById("omDock");
    if (!dock) { dock = document.createElement("div"); dock.id = "omDock"; dock.className = "om-dock"; document.body.appendChild(dock); }
    const pend = MM.pendingCount(list);
    const problems = MM.problemCount(list);
    let text;
    if (list.status === "ready") text = online() ? "Sending…" : "Waiting for internet — will send automatically.";
    else if (pend) text = `${pend} mark(s) saved on this phone, not sent yet.`;
    else if (list.status === "sent") text = "All marks are on the school system.";
    else text = "Marks you type are saved on this phone straight away.";
    if (problems) text = `${problems} mark(s) need your attention.`;
    dock.innerHTML = `<div class="om-dock-in"><small id="omDockText">${esc(text)}</small>
      ${list.can_submit ? `<label class="om-check"><input type="checkbox" id="omSubmit" ${list.submitAfterSend ? "checked" : ""}> Submit for approval after sending</label>` : ""}
      <div class="om-dock-row"><button class="om-btn ghost" id="omDone">Save draft</button>
      <button class="om-btn primary" id="omSend">${online() ? "Send to school" : "Send when online"}</button></div></div>`;
    document.getElementById("omDone").addEventListener("click", () => { toast("Draft saved on this phone."); location.hash = ""; });
    document.getElementById("omSend").addEventListener("click", () => sendList(list.id));
  }
  function removeDock() { const d = document.getElementById("omDock"); if (d) d.remove(); }

  async function refreshCounts(id) {
    const list = await MM.getList(id);
    const p = MM.progress(list);
    const set = (i, v) => { const el = document.getElementById(i); if (el) el.textContent = v; };
    set("cEntered", p.entered); set("cAbsent", p.absent); set("cMissing", p.missing); set("cPending", MM.pendingCount(list));
    const [label, cls] = STATUS[list.status] || STATUS.clean;
    const st = document.getElementById("omStatus"); if (st) { st.textContent = label; st.className = "om-badge " + cls; }
    renderDock(list);
    return list;
  }

  function wireList(id, maxMark) {
    const rows = document.getElementById("omRows");
    const inputs = Array.from(rows.querySelectorAll(".om-input:not([disabled])"));

    async function commit(input) {
      const res = MM.check(input.value, maxMark);
      const row = input.closest(".om-row");
      const slot = row.querySelector(".om-slot");
      if (!res.ok) {
        input.classList.add("bad");
        slot.innerHTML = `<div class="om-rowmsg bad">${esc(res.message)} — not saved</div>`;
        return;
      }
      input.classList.remove("bad");
      if (res.value === "ABS") input.value = "ABS";
      try {
        const list = await MM.setEdit(id, input.dataset.sid, res.value);
        const s = list.students.find((x) => String(x.id) === input.dataset.sid);
        const edited = !!list.edits[s.id];
        row.className = "om-row " + (edited ? "edited" : "");
        slot.innerHTML = rowMessage(s, edited, null, null);
        refreshCounts(id);
      } catch (e) {
        toast("Could not save on this phone: " + e.message, 5000);
      }
    }
    function move(input, dir) {
      const visible = inputs.filter((i) => i.closest(".om-row").style.display !== "none");
      const next = visible[visible.indexOf(input) + dir];
      if (next) { next.focus(); next.select(); } else input.blur();
    }
    inputs.forEach((input) => {
      input.addEventListener("focus", () => input.select());
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === "ArrowDown") { e.preventDefault(); clearTimeout(input._t); commit(input); move(input, 1); }
        else if (e.key === "ArrowUp") { e.preventDefault(); clearTimeout(input._t); commit(input); move(input, -1); }
      });
      input.addEventListener("input", () => {
        clearTimeout(input._t);
        input._t = setTimeout(() => { if (MM.check(input.value, maxMark).ok) commit(input); }, 700);
      });
      input.addEventListener("blur", () => { clearTimeout(input._t); commit(input); });
    });
    rows.addEventListener("click", async (e) => {
      const abs = e.target.closest("[data-abs]");
      if (abs) { const input = rows.querySelector(`.om-input[data-sid="${abs.dataset.abs}"]`); input.value = "ABS"; commit(input); move(input, 1); return; }
      const keep = e.target.closest("[data-keep]");
      const use = e.target.closest("[data-useserver]");
      if (keep || use) {
        await MM.resolveConflict(id, Number((keep || use).dataset.keep || (keep || use).dataset.useserver), !!keep);
        toast(keep ? "Your mark will be sent again." : "Using the school's mark.");
        renderList(id);
      }
    });
    document.getElementById("omSearch").addEventListener("input", (e) => {
      const q = e.target.value.trim().toLowerCase();
      rows.querySelectorAll(".om-row").forEach((r) => { r.style.display = !q || r.dataset.search.includes(q) ? "" : "none"; });
    });
    const rf = document.getElementById("omRefresh");
    if (rf) rf.addEventListener("click", () => refreshList(id));
    document.getElementById("omRemove").addEventListener("click", () => removeList(id));
  }

  async function sendList(id) {
    const focused = document.activeElement;
    if (focused && focused.classList && focused.classList.contains("om-input")) focused.blur();
    await new Promise((r) => setTimeout(r, 50));
    let list = await MM.getList(id);
    const submitBox = document.getElementById("omSubmit");
    const submit = !!(submitBox && submitBox.checked);
    if (document.querySelector(".om-input.bad")) { toast("Fix the marks shown in red first."); return; }
    if (submit) {
      const p = MM.progress(list);
      if (p.missing && !confirm(`${p.missing} student(s) have no mark. Submit for approval anyway? (Use A for absent.)`)) return;
    }
    if (!MM.pendingCount(list) && !submit) { toast(list.status === "sent" ? "Everything is already sent." : "No new marks to send."); return; }
    list = await MM.markReady(id, submit);
    if (!online()) {
      askBackgroundSync();
      toast("Saved. It will send by itself when you are back online.", 4500);
      renderList(id); return;
    }
    const btn = document.getElementById("omSend"); if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
    if (!state.me) await refreshMe();
    const r = await MM.syncList(id);
    if (r.auth) state.signedOut = true;
    if (r.offline) askBackgroundSync();
    toast(r.message, 5000);
    renderList(id);
  }

  async function refreshList(id) {
    const list = await MM.getList(id);
    try {
      const data = await getJSON(URLS.pkg + "?" + new URLSearchParams(list.params).toString());
      await MM.saveDownloaded(list.userId, data.list);
      toast("List updated from the school.");
    } catch (e) {
      if (e.auth) { state.signedOut = true; toast("Please sign in again."); }
      else toast("Could not refresh: " + e.message, 4500);
    }
    renderList(id);
  }

  async function removeList(id) {
    const list = await MM.getList(id);
    const pend = MM.pendingCount(list);
    const msg = pend ? `${pend} mark(s) on this list were NOT sent and will be lost. Remove anyway?` : "Remove this list from the phone? (Marks already sent stay on the school system.)";
    if (!confirm(msg)) return;
    await MM.deleteList(id);
    toast("List removed from this phone.");
    location.hash = "";
  }

  // ------------------------------------------------------------ start
  window.addEventListener("hashchange", () => { if (!location.hash.startsWith("#list=")) removeDock(); });
  window.addEventListener("online", async () => {
    setNet(); await refreshMe(); route();
    if (state.user && !state.signedOut) {
      const results = await MM.syncReady(state.user.id);
      if (results.length) { report(results); route(); }
    }
  });
  window.addEventListener("offline", () => { setNet(); route(); });
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register(URLS.sw, { scope: "/" }).catch(() => {});
    navigator.serviceWorker.addEventListener("message", (e) => {
      if (e.data && e.data.type === "marks-synced") { route(); toast("Waiting marks were sent to the school."); }
    });
  }

  (async function start() {
    setNet();
    try { await MM.open(); } catch (e) {
      main.innerHTML = `<p class="om-note bad">This browser cannot store marks offline (private mode or storage blocked). Use Chrome normally, not in incognito.</p>`;
      return;
    }
    await MM.setMeta("urls", { sync: URLS.sync, pkg: URLS.pkg, me: URLS.me });
    state.user = await MM.getMeta("lastUser");
    route();                               // show what is on the phone straight away
    await refreshMe();
    if (state.user && !state.signedOut && online()) {
      const results = await MM.syncReady(state.user.id);
      if (results.length) report(results);
    }
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
    route();
  })();
})();
