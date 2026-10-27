(function () {
  "use strict";

  // Say exactly where refunds go in this mode; never claim live calls that
  // are not happening.
  function describe(health) {
    var CAPACITY_TEXT = {
      public_replay: "The service is busy, so answers come from recordings.",
      replay_only: "The service is paused, so answers come from recordings.",
    };
    var payments = health.paypal_execution === "sandbox"
      ? "Refunds go to the PayPal sandbox, with test money only."
      : "Refunds use recorded PayPal results. Nothing is sent to PayPal.";
    var checks = health.ai_models === "live"
      ? "The AI checks run on live models."
      : "The AI checks are off right now.";
    var capacity = CAPACITY_TEXT[health.capacity] || "";
    return "Running. " + payments + " " + checks + " " + capacity;
  }

  var box = document.getElementById("status");
  if (!box) {
    return;
  }

  fetch("/health", { headers: { Accept: "application/json" } })
    .then(function (response) {
      if (!response.ok) {
        throw new Error("status " + response.status);
      }
      return response.json();
    })
    .then(function (health) {
      box.textContent = describe(health).trim();
    })
    .catch(function () {
      box.textContent =
        "The status is unavailable right now. Please try again in a minute.";
    });
})();
