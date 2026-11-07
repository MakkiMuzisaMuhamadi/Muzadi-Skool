/* Muzadi offline marks - storage on the phone (IndexedDB) and sending to the server.
 *
 * Used by the Offline Marks page AND by the service worker (background sending),
 * so it must not touch the page (no document/window).
 *
 * A "list" (one class/stream + subject/paper + exam type) is kept like this:
 *   { id, userId, key, params, title, exam, term, max_mark, students:[{id, value(=server value
 *     when downloaded), locked, ...}], edits:{studentId:{value, at, force}},
 *     conflicts:{studentId:{server, mine}}, errors:{studentId: message},
 *     status: "clean" | "draft" | "ready" | "sent" | "attention",
 *     submitAfterSend, downloadedAt, lastSentAt, lastMessage }
 *
 * Edits stay on the phone until the server has accepted them.
 */
(function (root) {
  "use strict";
  const DB_NAME = "muzadi-offline-marks";
  const DB_VERSION = 1;
  const ABSENT = ["ABS", "AB", "ABSENT", "A"];
  let dbPromise = null;

  function req(r) {
    return new Promise((resolve, reject) => { r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); });
  }
  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const r = indexedDB.open(DB_NAME, DB_VERSION);
      r.onupgradeneeded = () => {
        const db = r.result;
        if (!db.objectStoreNames.contains("lists")) {
          const s = db.createObjectStore("lists", { keyPath: "id" });
          s.createIndex("byUser", "userId", { unique: false });
        }
        if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "key" });
      };
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => { dbPromise = null; reject(r.error); };
    });
    return dbPromise;
  }
  async function tx(store, mode, fn) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const t = db.transaction(store, mode);
      let out;
      Promise.resolve(fn(t.objectStore(store))).then((v) => { out = v; }, reject);
      t.oncomplete = () => resolve(out);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error || new Error("Storage error"));
    });
  }

  // ------------------------------------------------------------ values
  function norm(v) {
    if (v === null || v === undefined) return "";
    const t = String(v).trim();
    if (t === "" || t.toLowerCase() === "null") return "";
    if (ABSENT.includes(t.toUpperCase())) return "ABS";
    if (/^\d+(\.\d+)?$/.test(t)) return (Math.round(parseFloat(t) * 10) / 10).toFixed(1);
    return t.toUpperCase();
  }
  /** Same rules as the server (marks_service.parse_mark_input). */
  function check(raw, maxMark) {
    const v = String(raw == null ? "" : raw).trim();
    if (v === "") return { ok: true, value: "" };
    if (ABSENT.includes(v.toUpperCase())) return { ok: true, value: "ABS" };
    if (!/^\d+(\.\d)?$/.test(v)) return { ok: false, message: "Type a number (max one decimal) or ABS" };
    const n = parseFloat(v);
    if (maxMark != null && n > maxMark) return { ok: false, message: "Above the maximum of " + maxMark };
    return { ok: true, value: v };
  }

  // ------------------------------------------------------------ meta
  const getMeta = (key) => tx("meta", "readonly", (s) => req(s.get(key))).then((r) => (r ? r.value : null));
  const setMeta = (key, value) => tx("meta", "readwrite", (s) => req(s.put({ key, value })));

  // ------------------------------------------------------------ lists
  const listId = (userId, key) => userId + "|" + key;
  const getList = (id) => tx("lists", "readonly", (s) => req(s.get(id)));
  const putList = (list) => tx("lists", "readwrite", (s) => req(s.put(list)));
  const deleteList = (id) => tx("lists", "readwrite", (s) => req(s.delete(id)));
  const allLists = () => tx("lists", "readonly", (s) => req(s.getAll()));
  const listsForUser = (userId) => tx("lists", "readonly", (s) => req(s.index("byUser").getAll(userId)));

  /**
   * Read-change-write of one list inside ONE storage transaction, so two quick
   * changes (two students typed fast, or the page and the background sender)
   * can never overwrite each other. `change(list)` must be synchronous;
   * it may return a new object (or null to delete).
   */
  function update(id, change) {
    return tx("lists", "readwrite", (s) => new Promise((resolve, reject) => {
      const g = s.get(id);
      g.onerror = () => reject(g.error);
      g.onsuccess = () => {
        let list;
        try { list = change(g.result); } catch (e) { reject(e); return; }
        if (list === undefined) list = g.result;
        if (list === null) { s.delete(id); resolve(null); return; }
        if (!list) { resolve(list); return; }
        const p = s.put(list);
        p.onsuccess = () => resolve(list);
        p.onerror = () => reject(p.error);
      };
    }));
  }

  function pendingCount(list) { return Object.keys(list.edits || {}).length; }
  function problemCount(list) {
    return Object.keys(list.conflicts || {}).length + Object.keys(list.errors || {}).length;
  }
  function recomputeStatus(list) {
    const pending = pendingCount(list);
    if (problemCount(list)) list.status = "attention";
    else if (!pending) list.status = list.lastSentAt ? "sent" : "clean";
    else if (list.status !== "ready") list.status = "draft";
    return list;
  }
  /** Current value shown on the phone for one student. */
  function valueFor(list, student) {
    const e = (list.edits || {})[student.id];
    return e ? e.value : (student.value || "");
  }
  function progress(list) {
    let entered = 0, absent = 0, total = 0;
    (list.students || []).forEach((s) => {
      total++;
      const v = norm(valueFor(list, s));
      if (v) { entered++; if (v === "ABS") absent++; }
    });
    return { entered, absent, total, missing: total - entered };
  }

  /** Save a list downloaded from the server; keeps any unsent edits. */
  function saveDownloaded(userId, pkg) {
    const id = listId(userId, pkg.key);
    return update(id, (old) => {
      const list = Object.assign({}, pkg, {
        id, userId,
        downloadedAt: pkg.downloaded_at || new Date().toISOString(),
        edits: old ? old.edits || {} : {},
        conflicts: old ? old.conflicts || {} : {},
        errors: old ? old.errors || {} : {},
        status: old ? old.status : "clean",
        submitAfterSend: old ? !!old.submitAfterSend : false,
        lastSentAt: old ? old.lastSentAt || null : null,
        lastMessage: old ? old.lastMessage || "" : "",
      });
      if (old) {
        // a student with an unsent edit keeps the value they started from,
        // so the server can still spot a change made by someone else
        const oldBase = {};
        (old.students || []).forEach((s) => { oldBase[s.id] = s.value; });
        list.students = pkg.students.map((s) => (list.edits[s.id] && s.id in oldBase)
          ? Object.assign({}, s, { value: oldBase[s.id] }) : s);
      }
      return recomputeStatus(list);
    });
  }

  function setEdit(id, studentId, value) {
    return update(id, (list) => {
      if (!list) throw new Error("List not found on this phone");
      const student = list.students.find((s) => String(s.id) === String(studentId));
      if (!student) throw new Error("Student not in list");
      list.edits = list.edits || {};
      delete (list.errors || {})[student.id];
      delete (list.conflicts || {})[student.id];
      const had = list.edits[student.id];
      if (norm(value) === norm(student.value)) delete list.edits[student.id];
      else if (!had || norm(had.value) !== norm(value)) {
        list.edits[student.id] = { value: value, at: new Date().toISOString(), force: had ? !!had.force : false };
      } else { return recomputeStatus(list); }     // same value typed again: nothing changes
      if (list.status === "ready" || list.status === "sent" || list.status === "clean") list.status = "draft";
      return recomputeStatus(list);
    });
  }

  function resolveConflict(id, studentId, keepMine) {
    return update(id, (list) => {
      const c = (list.conflicts || {})[studentId];
      if (!c) return list;
      const student = list.students.find((s) => String(s.id) === String(studentId));
      if (keepMine) {
        list.edits[studentId] = { value: c.mine, at: new Date().toISOString(), force: true };
      } else {
        delete list.edits[studentId];
        if (student) student.value = c.server;
      }
      delete list.conflicts[studentId];
      if (pendingCount(list)) list.status = "draft";
      return recomputeStatus(list);
    });
  }

  function markReady(id, submitAfterSend) {
    return update(id, (list) => {
      list.submitAfterSend = !!submitAfterSend;
      if (pendingCount(list) || list.submitAfterSend) list.status = "ready";
      return list;
    });
  }

  // ------------------------------------------------------------ sending
  /**
   * Sends one list. Returns {ok, auth, offline, message, list}.
   *   auth: true    -> signed out / security token old: sign in again, nothing lost
   *   offline: true -> no connection, try later
   */
  const sending = new Set();     // a list is never sent twice at the same time
  async function syncList(id, opts) {
    if (sending.has(id)) return { ok: false, busy: true, message: "Already sending…" };
    sending.add(id);
    try { return await syncListNow(id, opts); } finally { sending.delete(id); }
  }
  async function syncListNow(id, opts) {
    opts = opts || {};
    const urls = (await getMeta("urls")) || {};
    const csrf = opts.csrf || (await getMeta("csrf"));
    let list = await getList(id);
    if (!list) return { ok: false, message: "List not found" };
    const marks = Object.keys(list.edits || {}).map((sid) => {
      const s = list.students.find((x) => String(x.id) === String(sid)) || {};
      const e = list.edits[sid];
      return { student_id: Number(sid), marksheet_id: s.marksheet_id, value: e.value, base: s.value || "", force: !!e.force };
    });
    const submit = !!list.submitAfterSend && !!list.can_submit;
    if (!marks.length && !submit) {
      list = await update(id, (l) => recomputeStatus(l));
      return { ok: true, message: "Nothing to send", list };
    }
    if (!urls.sync) return { ok: false, message: "Open the app once while online." };

    let res, data = null;
    try {
      res = await fetch(urls.sync, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json", "Accept": "application/json",
          "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf || "",
        },
        body: JSON.stringify({ key: list.key, params: list.params, marks: marks, submit: submit }),
      });
    } catch (e) {
      return { ok: false, offline: true, message: "No connection. Your marks are safe on this phone." };
    }
    const type = res.headers.get("content-type") || "";
    if (type.includes("application/json")) { try { data = await res.json(); } catch (e) { data = null; } }

    if (!data || res.redirected || res.status === 401 || (res.status === 403 && !data)) {
      const msg = "Sign in again to send. Your marks are safe on this phone.";
      await update(id, (l) => { l.lastMessage = msg; return l; });
      return { ok: false, auth: true, message: msg };
    }
    if (!data.ok) {
      const why = data.error === "subscription_expired_read_only"
        ? "The school's subscription has expired: marks cannot be saved right now."
        : data.error === "password_change_required" ? "Open the system and choose a new password first."
        : (data.error || ("Server error " + res.status));
      if (res.status >= 500) return { ok: false, offline: true, message: why };
      list = await update(id, (l) => { l.lastMessage = why; l.status = "attention"; return l; });
      return { ok: false, message: why, list };
    }

    // apply results (inside one transaction: the teacher may be typing meanwhile)
    const c = data.counts || {};
    const sentValue = {};
    marks.forEach((m) => { sentValue[m.student_id] = m.value; });
    list = await update(id, (list) => {
      list.conflicts = list.conflicts || {}; list.errors = list.errors || {};
      (data.results || []).forEach((r) => {
        const sid = r.student_id;
        const student = list.students.find((x) => x.id === sid);
        const edit = list.edits[sid];
        const unchangedSince = edit && norm(edit.value) === norm(sentValue[sid]);
        if (r.status === "saved" || r.status === "removed" || r.status === "unchanged") {
          if (student) student.value = r.server_value || "";
          if (unchangedSince) delete list.edits[sid];
          delete list.errors[sid]; delete list.conflicts[sid];
        } else if (r.status === "conflict") {
          list.conflicts[sid] = { server: r.server_value || "", mine: sentValue[sid] };
        } else {
          list.errors[sid] = r.message || "Not saved";
        }
      });
      const savedN = (c.saved || 0) + (c.removed || 0) + (c.unchanged || 0);
      list.lastSentAt = new Date().toISOString();
      list.lastMessage = savedN + " mark(s) saved on the server"
        + (c.conflict ? ", " + c.conflict + " changed by someone else" : "")
        + (c.error ? ", " + c.error + " refused" : "")
        + (data.submit_message ? ". " + data.submit_message : ".");
      if (data.submitted) { list.submitAfterSend = false; list.can_submit = false; }
      if (list.status === "ready" && (c.conflict || c.error)) list.status = "attention";
      if (list.status === "ready" && !pendingCount(list)) list.status = "sent";
      return recomputeStatus(list);
    });
    return { ok: !(c.conflict || c.error), message: list.lastMessage, list, counts: c, submitted: data.submitted };
  }

  /** Send every list marked "ready" for this user (used on reconnect and by the service worker). */
  async function syncReady(userId) {
    const lists = (await listsForUser(userId)).filter((l) => l.status === "ready");
    const out = [];
    for (const l of lists) {
      const r = await syncList(l.id);
      if (r.busy) continue;
      out.push(r);
      if (r.auth || r.offline) break;
    }
    return out;
  }

  root.MuzadiMarks = {
    open, norm, check, getMeta, setMeta, listId, getList, putList, update, deleteList, allLists, listsForUser,
    pendingCount, problemCount, recomputeStatus, valueFor, progress, saveDownloaded, setEdit,
    resolveConflict, markReady, syncList, syncReady,
  };
})(typeof self !== "undefined" ? self : this);
