"use strict";

import {
    parsePositiveInt,
    postAction,
    registerHandlers,
    showStatus,
    startControlPagePoll,
} from "./core.js";

function hideRequestBanner() {
    const banner = document.querySelector(".request-banner");
    if (banner) {
        banner.remove();
    }
}

function grantExtension(button) {
    const minutes = parsePositiveInt(
        document.getElementById("extension-minutes").value
    );
    if (minutes === null) {
        showStatus("Enter a positive number of minutes", false);
        return;
    }
    postAction(
        { action: "extend_time", minutes: minutes },
        { button: button, refreshStats: true, onSuccess: hideRequestBanner }
    );
}

registerHandlers({
    lock: function (el) {
        postAction({ action: "lock" }, { button: el, refreshStats: true });
    },
    shutdown: function (el) {
        if (!confirm("Are you sure you want to shutdown this computer?")) {
            return;
        }
        postAction({ action: "shutdown" }, { button: el, refreshStats: true });
    },
    "send-message": function (el) {
        const input = document.getElementById("message-text");
        const message = input.value;
        if (!message) {
            showStatus("Please enter a message", false);
            return;
        }
        postAction(
            { action: "message", message: message },
            {
                button: el,
                onSuccess: function () {
                    input.value = "";
                },
            }
        );
    },
    "quick-extend": function (el) {
        const minutes = el.getAttribute("data-minutes");
        document.getElementById("extension-minutes").value = minutes;
        grantExtension(el);
    },
    "grant-extension": function (el) {
        grantExtension(el);
    },
    "clear-manual-lock": function (el) {
        if (!confirm("Clear the manual lock?")) {
            return;
        }
        postAction(
            { action: "clear_manual_lock" },
            { button: el, refreshStats: true }
        );
    },
    "dismiss-request": function (el) {
        postAction(
            { action: "dismiss_time_request" },
            { button: el, onSuccess: hideRequestBanner }
        );
    },
    "reset-today": function (el) {
        if (!confirm("Reset today's used time to zero? (Carry-over is kept.)")) {
            return;
        }
        postAction(
            { action: "reset_today" },
            { button: el, refreshStats: true }
        );
    },
    "save-breaks": function (el) {
        const interval = parseInt(
            document.getElementById("break-interval").value,
            10
        );
        const duration = parseInt(
            document.getElementById("break-duration").value,
            10
        );
        if (isNaN(interval) || interval < 0) {
            showStatus("Enter a break interval of 0 or more minutes", false);
            return;
        }
        if (isNaN(duration) || duration < 1) {
            showStatus("Enter a break length of at least 1 minute", false);
            return;
        }
        postAction(
            { action: "set_break_duration", minutes: duration },
            {
                button: el,
                onSuccess: function () {
                    postAction(
                        { action: "set_break_interval", minutes: interval },
                        { button: el, refreshStats: true }
                    );
                },
            }
        );
    },
    "toggle-timer": function (el) {
        const enabled = el.getAttribute("data-enabled") === "false";
        postAction(
            { action: "set_show_timer", enabled: enabled },
            {
                button: el,
                onSuccess: function () {
                    el.setAttribute("data-enabled", enabled ? "true" : "false");
                    el.textContent = enabled
                        ? "Hide on-screen timer"
                        : "Show on-screen timer";
                },
            }
        );
    },
});

if (document.body.classList.contains("page-control")) {
    startControlPagePoll();
}
