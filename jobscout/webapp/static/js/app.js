/* jobscout dashboard interactions — vanilla JS, no build step.
   Progressive enhancement only: every page works without this file. */

(function () {
  "use strict";

  var body = document.body;

  /* ── theme toggle ─────────────────────────────────────────────────────── */

  var THEME_KEY = "jobscout-theme";

  function applyTheme(theme) {
    if (theme === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  applyTheme(localStorage.getItem(THEME_KEY) || "light");

  document.addEventListener("click", function (e) {
    var toggle = e.target.closest("[data-theme-toggle]");
    if (!toggle) return;
    var next = document.documentElement.getAttribute("data-theme") === "dark"
      ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
    toast("Theme: " + next, "info");
  });

  /* ── toasts ───────────────────────────────────────────────────────────── */

  var toastsEl = null;

  function toast(message, tone) {
    if (!toastsEl) {
      toastsEl = document.createElement("div");
      toastsEl.className = "toasts";
      toastsEl.setAttribute("aria-live", "polite");
      body.appendChild(toastsEl);
    }
    var t = document.createElement("div");
    t.className = "toast toast--" + (tone || "info");
    t.textContent = message;
    toastsEl.appendChild(t);
    var ttl = message.length > 90 ? 7000 : 4000;
    setTimeout(function () {
      t.classList.add("toast--leaving");
      setTimeout(function () { t.remove(); }, 300);
    }, ttl);
  }

  window.jobscoutToast = toast;

  /* ── htmx wiring ──────────────────────────────────────────────────────── */

  // global busy indicator (topbar) while any htmx request flies
  document.body.addEventListener("htmx:beforeRequest", function () {
    body.classList.add("htmx-busy");
  });
  document.body.addEventListener("htmx:afterRequest", function () {
    body.classList.remove("htmx-busy");
  });

  // server-driven toasts: any response carrying X-Toast shows one
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var xhr = e.detail.xhr;
    if (!xhr || !xhr.getResponseHeader) return;
    var msg = xhr.getResponseHeader("X-Toast");
    if (msg) {
      var tone = xhr.getResponseHeader("X-Toast-Tone") || "info";
      try { msg = decodeURIComponent(msg); } catch (err) { /* keep raw */ }
      toast(msg, tone);
    }
  });

  // swap skeletons into a results container before its content arrives
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var target = e.detail.elt;
    if (!target || !target.matches) return;
    var results = target.closest("[data-skeleton-rows]");
    if (results && e.detail.xhr) {
      var n = parseInt(results.getAttribute("data-skeleton-rows") || "10", 10);
      var frag = document.createDocumentFragment();
      for (var i = 0; i < n; i++) {
        var row = document.createElement("div");
        row.className = "skeleton skeleton--row";
        frag.appendChild(row);
      }
      var wrap = document.createElement("div");
      wrap.className = "stack--sm";
      wrap.style.display = "flex";
      wrap.style.flexDirection = "column";
      wrap.style.gap = "4px";
      wrap.appendChild(frag);
      results.innerHTML = "";
      results.appendChild(wrap);
    }
  });

  // flash rows that were swapped in by a POST (status change)
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.requestConfig && e.detail.requestConfig.verb !== "get") {
      var target = e.detail.target;
      var rows = target && target.matches ? [target] : [];
      if (target && target.querySelectorAll) {
        rows = rows.concat(Array.prototype.slice.call(
          target.querySelectorAll("tr")));
      }
      rows.forEach(function (r) {
        if (r.tagName === "TR") r.classList.add("tr--flash");
      });
    }
  });

  // buttons enter busy state while their own request is in flight
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var el = e.detail.elt;
    if (el && el.matches && el.matches("button")) el.setAttribute("aria-busy", "true");
  });
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var el = e.detail.elt;
    if (el && el.matches && el.matches("button")) el.removeAttribute("aria-busy");
  });

  /* ── confirm-before-send on destructive controls ──────────────────────── */

  document.body.addEventListener("htmx:confirm", function (e) {
    var el = e.detail.elt;
    if (!el || !el.hasAttribute || !el.hasAttribute("data-confirm")) return;
    e.preventDefault();
    if (window.confirm(el.getAttribute("data-confirm"))) {
      e.detail.issueRequest(true);
    }
  });

  /* ── plain (non-htmx) form feedback: disable submit while pending ─────── */

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches("form[data-busy-submit]")) return;
    var btn = form.querySelector('button[type="submit"]');
    if (btn) {
      btn.setAttribute("aria-busy", "true");
      window.setTimeout(function () { btn.removeAttribute("aria-busy"); }, 20000);
    }
  });

  /* ── keyboard: "/" focuses the search box ─────────────────────────────── */

  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.target.closest("input, textarea, select")) return;
    var search = document.querySelector('input[name="q"]');
    if (search) {
      e.preventDefault();
      search.focus();
      search.select && search.select();
    }
  });
})();
