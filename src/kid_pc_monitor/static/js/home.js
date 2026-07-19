"use strict";

// Home page: PC card navigation, one-tap bonus time, and periodic refresh.

import { postAction } from "./core.js";

document.addEventListener("click", function (event) {
    // One-tap bonus time on a PC card. Handle this before card navigation so
    // tapping the button grants time instead of opening the control page.
    const bonus = event.target.closest('[data-action="bonus"]');
    if (bonus) {
        event.stopPropagation();
        const minutes = parseInt(bonus.getAttribute("data-minutes"), 10);
        const ip = bonus.getAttribute("data-ip");
        if (!Number.isInteger(minutes) || !ip) {
            return;
        }
        postAction(
            { action: "extend_time", minutes: minutes, ip: ip },
            { button: bonus }
        );
        return;
    }

    const target = event.target.closest('[data-action="navigate"]');
    if (!target) {
        return;
    }
    const href = target.getAttribute("data-href");
    if (href) {
        location.href = href;
    }
});

if (document.body.classList.contains("page-home")) {
    setTimeout(function () {
        location.reload();
    }, 30000);
}
