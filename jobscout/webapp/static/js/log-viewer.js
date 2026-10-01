/* log-viewer.js — SSE client for the real-time run log viewer.
   Connects to /discovery/run-logs, appends entries to the scrollable
   display, and updates the stats overlay (agent calls + spend) in real time. */

(function () {
  "use strict";

  var logViewer = null;
  var logEntries = null;
  var logBody = null;
  var logToggle = null;
  var logStatusText = null;
  var logCountBadge = null;
  var logAgentCalls = null;
  var logAgentSpend = null;
  var eventSource = null;
  var entryCount = 0;
  var isExpanded = false;
  var userScrolledUp = false;

  function init() {
    logViewer = document.getElementById("log-viewer");
    if (!logViewer) return;

    logEntries = document.getElementById("log-entries");
    logBody = document.getElementById("log-viewer-body");
    logToggle = document.getElementById("log-viewer-toggle");
    logStatusText = document.getElementById("log-status-text");
    logCountBadge = document.getElementById("log-count-badge");
    logAgentCalls = document.getElementById("log-agent-calls");
    logAgentSpend = document.getElementById("log-agent-spend");

    if (logEntries) {
      logEntries.addEventListener("scroll", function () {
        var atBottom = logEntries.scrollHeight - logEntries.scrollTop - logEntries.clientHeight < 40;
        userScrolledUp = !atBottom;
      });
    }

    // Auto-expand if a run is active
    if (logViewer.getAttribute("data-run-active") === "true") {
      expandLogViewer();
    }
  }

  function toggleLogViewer() {
    if (isExpanded) {
      collapseLogViewer();
    } else {
      expandLogViewer();
    }
  }

  function expandLogViewer() {
    if (!logBody) return;
    logBody.classList.remove("d-none");
    if (logToggle) logToggle.setAttribute("aria-expanded", "true");
    var chevron = logToggle ? logToggle.querySelector(".log-chevron") : null;
    if (chevron) chevron.style.transform = "rotate(90deg)";
    isExpanded = true;
    connectSSE();
  }

  function collapseLogViewer() {
    if (!logBody) return;
    logBody.classList.add("d-none");
    if (logToggle) logToggle.setAttribute("aria-expanded", "false");
    var chevron = logToggle ? logToggle.querySelector(".log-chevron") : null;
    if (chevron) chevron.style.transform = "";
    isExpanded = false;
    disconnectSSE();
  }

  function connectSSE() {
    // If an existing connection is still open, close it and reconnect.
    // HTMX swaps the entire #run-status div every 3s, so the old DOM
    // elements the previous connection's closures reference are gone.
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    if (!logStatusText) return;

    logStatusText.textContent = "connecting…";
    eventSource = new EventSource("/discovery/run-logs");

    eventSource.onmessage = function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }

      if (data.type === "snapshot") {
        // Clear and load snapshot
        if (logEntries) logEntries.innerHTML = "";
        entryCount = 0;
        if (data.entries) {
          for (var i = 0; i < data.entries.length; i++) {
            appendEntry(data.entries[i]);
          }
        }
        updateStats(data.stats);
        if (logStatusText) logStatusText.textContent = "live";
      } else if (data.type === "entry") {
        appendEntry(data.entry);
        updateStats(data.stats);
        if (logStatusText) logStatusText.textContent = "live";
      } else if (data.type === "run_complete") {
        if (logStatusText) logStatusText.textContent = "complete";
        disconnectSSE();
      }
    };

    eventSource.onerror = function () {
      if (logStatusText) logStatusText.textContent = "reconnecting…";
      // EventSource auto-reconnects; nothing to do
    };
  }

  function disconnectSSE() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  function appendEntry(entry) {
    if (!logEntries) return;

    var row = document.createElement("div");
    row.className = "log-entry log-entry--" + (entry.level || "info");

    var time = document.createElement("span");
    time.className = "log-entry__time";
    time.textContent = formatTime(entry.timestamp);

    var level = document.createElement("span");
    level.className = "log-entry__level";
    level.textContent = (entry.level || "info").toUpperCase();

    var source = document.createElement("span");
    source.className = "log-entry__source";
    source.textContent = entry.source || "";

    var msg = document.createElement("span");
    msg.className = "log-entry__message";
    msg.textContent = entry.message || "";

    row.appendChild(time);
    row.appendChild(level);
    row.appendChild(source);
    row.appendChild(msg);

    logEntries.appendChild(row);
    entryCount++;

    // Keep only last 500 entries in DOM
    while (logEntries.children.length > 500) {
      logEntries.removeChild(logEntries.firstChild);
    }

    // Auto-scroll to bottom unless user has scrolled up
    if (!userScrolledUp) {
      logEntries.scrollTop = logEntries.scrollHeight;
    }

    // Update badge
    if (logCountBadge) {
      logCountBadge.textContent = entryCount;
    }
  }

  function updateStats(stats) {
    if (!stats) return;
    if (logAgentCalls) {
      logAgentCalls.textContent = stats.agent_calls || 0;
    }
    if (logAgentSpend) {
      logAgentSpend.textContent = (stats.total_spend || 0).toFixed(2);
    }
  }

  function formatTime(iso) {
    if (!iso) return "";
    try {
      var d = new Date(iso);
      return d.toLocaleTimeString("en-US", { hour12: false });
    } catch (err) {
      return iso;
    }
  }

  // Expose toggle globally for the button onclick
  window.toggleLogViewer = toggleLogViewer;

  // Initialize on DOM ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();