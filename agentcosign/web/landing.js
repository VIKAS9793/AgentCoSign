(function () {
  "use strict";

  var CAPACITY_TEXT = {
    normal: "Live services are available.",
    public_replay: "Live services are busy, so answers come from recordings.",
    replay_only: "Live services are paused, so answers come from recordings.",
  };

  function describe(health) {
    var mode = health.hermetic
      ? "Recorded demo: no live model or PayPal calls."
      : "Connected to live services.";
    var capacity = CAPACITY_TEXT[health.capacity] || "";
    return "Running. " + mode + " " + capacity;
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
