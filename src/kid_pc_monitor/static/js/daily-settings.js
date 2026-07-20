"use strict";

import { parsePositiveInt, postAction, registerHandlers, showStatus } from "./core.js";

registerHandlers({
    "save-daily-limit": function (el) {
        const minutes = parsePositiveInt(
            document.getElementById("daily-limit-minutes").value
        );
        if (minutes === null) {
            showStatus("Enter a positive number of minutes or use Remove", false);
            return;
        }
        postAction(
            { action: "set_daily_limit", minutes: minutes },
            { button: el, reloadDelay: 1000 }
        );
    },
    "clear-daily-limit": function (el) {
        if (!confirm("Remove the daily allowance default?")) {
            return;
        }
        postAction(
            { action: "clear_usage_limit" },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-bed-time": function (el) {
        const time = document.getElementById("bed-time").value;
        if (!time) {
            showStatus("Please select a bedtime", false);
            return;
        }
        postAction(
            { action: "set_bed_time", time: time },
            { button: el, reloadDelay: 1000 }
        );
    },
    "clear-bed-time": function (el) {
        if (!confirm("Remove the bedtime default?")) {
            return;
        }
        postAction(
            { action: "clear_bed_time" },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-wake-time": function (el) {
        const time = document.getElementById("wake-time").value;
        if (!time) {
            showStatus("Please select a wake-up time", false);
            return;
        }
        postAction(
            { action: "set_wake_time", time: time },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-weekend-enabled": function (el) {
        const enabled = document.getElementById("weekend-enabled").checked;
        postAction(
            { action: "set_weekend_enabled", enabled: enabled },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-weekend-allowance": function (el) {
        const value = document.getElementById("weekend-allowance").value.trim();
        postAction(
            { action: "set_weekend_allowance", minutes: value },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-weekend-bed-time": function (el) {
        const time = document.getElementById("weekend-bed-time").value;
        if (!time) {
            showStatus("Please select a weekend bedtime", false);
            return;
        }
        postAction(
            { action: "set_weekend_bed_time", time: time },
            { button: el, reloadDelay: 1000 }
        );
    },
    "clear-weekend-bed-time": function (el) {
        if (!confirm("Remove the weekend bedtime?")) {
            return;
        }
        postAction(
            { action: "clear_weekend_bed_time" },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-earn-enabled": function (el) {
        const enabled = document.getElementById("earn-enabled").checked;
        postAction(
            { action: "set_earn_enabled", enabled: enabled },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-earn-reward": function (el) {
        const minutes = parsePositiveInt(
            document.getElementById("earn-reward").value
        );
        if (minutes === null) {
            showStatus("Enter a positive number of minutes", false);
            return;
        }
        postAction(
            { action: "set_earn_reward", minutes: minutes },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-earn-questions": function (el) {
        const questions = parsePositiveInt(
            document.getElementById("earn-questions").value
        );
        if (questions === null) {
            showStatus("Enter a positive number of questions", false);
            return;
        }
        postAction(
            { action: "set_earn_questions", questions: questions },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-earn-cap": function (el) {
        const value = parseInt(document.getElementById("earn-cap").value, 10);
        if (!Number.isInteger(value) || value < 0) {
            showStatus("Enter 0 or more minutes for the daily cap", false);
            return;
        }
        postAction(
            { action: "set_earn_cap", minutes: value },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-earn-spelling-difficulty": function (el) {
        const level = document.getElementById("earn-spelling-difficulty").value;
        postAction(
            { action: "set_earn_spelling_difficulty", level: level },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-earn-maths-difficulty": function (el) {
        const level = document.getElementById("earn-maths-difficulty").value;
        postAction(
            { action: "set_earn_maths_difficulty", level: level },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-carryover-enabled": function (el) {
        const enabled = document.getElementById("carryover-enabled").checked;
        postAction(
            { action: "set_carryover_enabled", enabled: enabled },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-carryover-days": function (el) {
        const value = parseInt(document.getElementById("carryover-days").value, 10);
        if (!Number.isInteger(value) || value < 0) {
            showStatus("Enter 0 or more days", false);
            return;
        }
        postAction(
            { action: "set_carryover_max_days", days: value },
            { button: el, reloadDelay: 1000 }
        );
    },
    "save-carryover-balance": function (el) {
        const value = parseInt(document.getElementById("carryover-balance").value, 10);
        if (!Number.isInteger(value) || value < 0) {
            showStatus("Enter 0 or more minutes for the banked balance", false);
            return;
        }
        postAction(
            { action: "set_carryover_balance", minutes: value },
            { button: el, reloadDelay: 1000 }
        );
    },
});
