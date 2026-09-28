/* jobscout dashboard interactions — built on vendored Bootstrap 5.3.
   Progressive enhancement: every page works without this file. */

(function () {
  "use strict";

  var body = document.body;

  /* ── theme (Bootstrap's data-bs-theme) ──────────────────────────────── */

  var THEME_KEY = "jobscout-theme";

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-bs-theme", theme);
  }

  applyTheme(localStorage.getItem(THEME_KEY) || "light");

  document.addEventListener("click", function (e) {
    var toggle = e.target.closest("[data-theme-toggle]");
    if (!toggle) return;
    var next = document.documentElement.getAttribute("data-bs-theme") === "dark"
      ? "light" : "dark";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
    toast("Theme: " + next, "primary");
  });

  /* ── toasts (bootstrap.Toast) ───────────────────────────────────────── */

  var toastContainer = null;

  function toast(message, tone) {
    if (!window.bootstrap || !bootstrap.Toast) return;
    if (!toastContainer) {
      toastContainer = document.createElement("div");
      toastContainer.className =
        "toast-container position-fixed bottom-0 end-0 p-3";
      body.appendChild(toastContainer);
    }
    var el = document.createElement("div");
    el.className = "toast align-items-center border-0 text-bg-" +
      (tone === "danger" ? "danger" : tone === "success" ? "success" : "primary");
    el.setAttribute("role", "status");
    el.innerHTML =
      '<div class="d-flex"><div class="toast-body"></div>' +
      '<button type="button" class="btn-close btn-close-white me-2 m-auto" ' +
      'data-bs-dismiss="toast" aria-label="Close"></button></div>';
    el.querySelector(".toast-body").textContent = message;
    toastContainer.appendChild(el);
    new bootstrap.Toast(el, { delay: 4000 }).show();
    el.addEventListener("hidden.bs.toast", function () { el.remove(); });
  }

  window.jobscoutToast = toast;

  /* ── htmx wiring ────────────────────────────────────────────────────── */

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
      var tone = xhr.getResponseHeader("X-Toast-Tone") || "success";
      try { msg = decodeURIComponent(msg); } catch (err) { /* keep raw */ }
      toast(msg, tone);
    }
  });

  // skeleton placeholders while results load (Bootstrap .placeholder)
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var target = e.detail.elt;
    if (!target || !target.closest) return;
    var results = target.closest("[data-skeleton-rows]");
    if (results && e.detail.xhr) {
      var n = parseInt(results.getAttribute("data-skeleton-rows") || "8", 10);
      var html = '<div class="placeholder-glow p-3">';
      for (var i = 0; i < n; i++) {
        html += '<div class="placeholder col-12 py-3 mb-1 rounded"></div>';
      }
      html += "</div>";
      results.innerHTML = html;
    }
  });

  // flash rows swapped in by a POST (status change)
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.requestConfig && e.detail.requestConfig.verb !== "get") {
      var target = e.detail.target;
      var rows = [];
      if (target && target.tagName === "TR") rows.push(target);
      if (target && target.querySelectorAll) {
        rows = rows.concat([].slice.call(target.querySelectorAll("tr")));
      }
      rows.forEach(function (r) { r.classList.add("tr--flash"); });
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

  // confirm-before-send on destructive controls
  document.body.addEventListener("htmx:confirm", function (e) {
    var el = e.detail.elt;
    if (!el || !el.hasAttribute || !el.hasAttribute("data-confirm")) return;
    e.preventDefault();
    if (window.confirm(el.getAttribute("data-confirm"))) {
      e.detail.issueRequest(true);
    }
  });

  // plain forms: disable submit while pending
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches("form[data-busy-submit]")) return;
    var btn = form.querySelector('button[type="submit"]');
    if (btn) {
      btn.setAttribute("aria-busy", "true");
      setTimeout(function () { btn.removeAttribute("aria-busy"); }, 20000);
    }
  });

  /* ── dropdown-selects (Bootstrap Dropdown + hidden native select) ───── */

  // Selecting an option syncs the hidden <select>, updates the toggle label,
  // and fires a real change event so htmx hx-trigger="change" picks it up.
  document.addEventListener("click", function (e) {
    var item = e.target.closest(".js-select .dropdown-item");
    if (!item) return;
    var wrap = item.closest(".js-select");
    var select = wrap.querySelector("select");
    var toggle = wrap.querySelector(".dropdown-toggle");
    if (!select || !toggle) return;

    select.value = item.getAttribute("data-value") || "";
    [].forEach.call(select.options, function (o) {
      o.selected = o.value === select.value;
    });
    toggle.querySelector(".js-select-label").textContent =
      item.textContent.trim() || "—";
    toggle.classList.toggle("is-empty", !select.value);

    [].forEach.call(wrap.querySelectorAll(".dropdown-item"), function (i) {
      i.classList.toggle("active", i === item);
    });
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });

  /* ── table sorting: a property of every .table component ────────────── */

  function ensureSortKeys(table) {
    var head = table.tHead;
    if (!head || !head.rows.length) return;
    [].forEach.call(head.rows[0].cells, function (th, i) {
      if (th.hasAttribute("data-sort-key")) return;
      if (th.querySelector(".th-sort")) return;      // server-side column
      if (!th.textContent.trim()) return;             // empty actions column
      th.setAttribute("data-sort-key", "col-" + i);
    });
  }

  function autoEnableSorting(root) {
    [].forEach.call((root || document).querySelectorAll("table.table"),
      function (t) {
        if (t.hasAttribute("data-nosort")) return;
        if (t.querySelector(".th-sort")) return;      // server-side table
        t.setAttribute("data-sortable", "");
        ensureSortKeys(t);
      });
  }

  autoEnableSorting(document);
  document.body.addEventListener("htmx:afterSwap", function (e) {
    autoEnableSorting(e.detail.target);
  });

  document.addEventListener("click", function (e) {
    var th = e.target.closest("table[data-sortable] th[data-sort-key]");
    if (!th) return;
    var table = th.closest("table");
    var tbody = table.tBodies[0];
    var idx = [].indexOf.call(th.parentNode.children, th);
    // first click sorts ascending; clicking the active column toggles
    var dir = (th.getAttribute("data-sort-active") === "1" &&
               th.getAttribute("data-sort-dir") === "asc") ? -1 : 1;

    [].slice.call(tbody.rows).sort(function (a, b) {
      var ca = a.cells[idx] ? a.cells[idx].textContent.trim() : "";
      var cb = b.cells[idx] ? b.cells[idx].textContent.trim() : "";
      var na = parseFloat(ca.replace(/[^-\d.]/g, ""));
      var nb = parseFloat(cb.replace(/[^-\d.]/g, ""));
      var cmp;
      if (!isNaN(na) && !isNaN(nb) && ca && cb) cmp = na - nb;
      else cmp = ca.localeCompare(cb, undefined,
                                  { numeric: true, sensitivity: "base" });
      return cmp * dir;
    }).forEach(function (row) { tbody.appendChild(row); });

    [].forEach.call(table.querySelectorAll("th[data-sort-key]"), function (h) {
      h.removeAttribute("data-sort-active");
      h.removeAttribute("data-sort-dir");
    });
    th.setAttribute("data-sort-active", "1");
    th.setAttribute("data-sort-dir", dir === 1 ? "asc" : "desc");
  });

  /* ── keyboard: "/" focuses the search box ───────────────────────────── */

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
