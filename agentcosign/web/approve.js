(function () {
  "use strict";

  var statusBox = document.getElementById("status");
  var session = null;

  function say(text) {
    statusBox.textContent = text;
    statusBox.hidden = !text;
  }

  function show(id, visible) {
    document.getElementById(id).hidden = !visible;
  }

  // --- base64url and WebAuthn conversion ------------------------------------
  function fromB64url(text) {
    var base = text.replace(/-/g, "+").replace(/_/g, "/");
    var padded = base + "===".slice((base.length + 3) % 4);
    var raw = atob(padded);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i += 1) {
      bytes[i] = raw.charCodeAt(i);
    }
    return bytes.buffer;
  }

  function toB64url(buffer) {
    var bytes = new Uint8Array(buffer);
    var raw = "";
    for (var i = 0; i < bytes.length; i += 1) {
      raw += String.fromCharCode(bytes[i]);
    }
    return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function creationOptions(json) {
    var options = Object.assign({}, json);
    options.challenge = fromB64url(json.challenge);
    options.user = Object.assign({}, json.user, { id: fromB64url(json.user.id) });
    options.excludeCredentials = (json.excludeCredentials || []).map(function (c) {
      return Object.assign({}, c, { id: fromB64url(c.id) });
    });
    return { publicKey: options };
  }

  function requestOptions(json) {
    var options = Object.assign({}, json);
    options.challenge = fromB64url(json.challenge);
    options.allowCredentials = (json.allowCredentials || []).map(function (c) {
      return Object.assign({}, c, { id: fromB64url(c.id) });
    });
    return { publicKey: options };
  }

  function registrationJson(credential) {
    return {
      id: credential.id,
      rawId: toB64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: toB64url(credential.response.clientDataJSON),
        attestationObject: toB64url(credential.response.attestationObject),
      },
    };
  }

  function assertionJson(credential) {
    var handle = credential.response.userHandle;
    return {
      id: credential.id,
      rawId: toB64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: toB64url(credential.response.clientDataJSON),
        authenticatorData: toB64url(credential.response.authenticatorData),
        signature: toB64url(credential.response.signature),
        userHandle: handle ? toB64url(handle) : null,
      },
    };
  }

  // --- talking to the gateway ------------------------------------------------
  function post(path, body, headers) {
    return fetch(path, {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, headers || {}),
      body: JSON.stringify(body || {}),
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          var failure = new Error(data.detail || "The request was not accepted.");
          failure.status = response.status;
          throw failure;
        }
        return data;
      });
    });
  }

  function sessionHeaders() {
    return session ? { "X-Approver-Session": session } : {};
  }

  // --- enrolment ---------------------------------------------------------------
  var enrolToken = new URLSearchParams(window.location.hash.slice(1)).get("enrol");

  function enrol() {
    var name = document.getElementById("enrol-name").value.trim();
    if (!name) {
      say("Please enter your name first.");
      return;
    }
    var headers = { "X-Enrolment-Token": enrolToken };
    say("Follow the prompt on your phone.");
    post("/v1/approver/enrolment/options", { approver: name }, headers)
      .then(function (options) {
        return navigator.credentials.create(creationOptions(options));
      })
      .then(function (credential) {
        return post(
          "/v1/approver/enrolment",
          { approver: name, credential: registrationJson(credential) },
          headers
        );
      })
      .then(function () {
        history.replaceState(null, "", window.location.pathname);
        show("enrol", false);
        show("signin", true);
        say("This phone is set up. Sign in to see what is waiting.");
      })
      .catch(function (error) {
        say(error && error.name === "NotAllowedError"
          ? "You closed the fingerprint prompt. This phone is not set up yet."
          : "Set up did not finish. The link may have run out. Ask for a new link.");
      });
  }

  // --- sign in -------------------------------------------------------------------
  function signIn() {
    say("Follow the prompt on your phone.");
    post("/v1/approver/session/options")
      .then(function (options) {
        return navigator.credentials.get(requestOptions(options));
      })
      .then(function (credential) {
        return post("/v1/approver/session", { credential: assertionJson(credential) });
      })
      .then(function (data) {
        session = data.session;
        show("signin", false);
        show("queue", true);
        say("");
        loadQueue();
      })
      .catch(function (error) {
        if (error && error.status === 409) {
          say("This phone is not set up yet. Ask for a setup link, then open it on this phone.");
          return;
        }
        say(error && error.name === "NotAllowedError"
          ? "You closed the fingerprint prompt. Try again when you are ready."
          : "We could not open your requests. Please try again.");
      });
  }

  // --- the queue ---------------------------------------------------------------------
  function money(amount) {
    var value = Number(amount);
    if (!isFinite(value)) {
      return "$" + amount;
    }
    return "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function clock(iso) {
    return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function ago(iso) {
    var minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 1) {
      return "just now";
    }
    return minutes === 1 ? "1 minute ago" : minutes + " minutes ago";
  }

  function el(tag, text, className) {
    var node = document.createElement(tag);
    if (text) {
      node.textContent = text;
    }
    if (className) {
      node.className = className;
    }
    return node;
  }

  function fact(label, value) {
    var row = el("div", "", "fact");
    row.appendChild(el("span", label));
    row.appendChild(el("span", value));
    return row;
  }

  function showResult(card, kind, title, detail) {
    card.replaceChildren();
    card.className = "card result-card " + kind;
    var heading = el("h3", title, "result-title");
    heading.tabIndex = -1;
    card.appendChild(el("p", kind === "sent" ? "Done" : kind === "stopped" ? "Not sent" : "Check this", "result-badge"));
    card.appendChild(heading);
    if (detail) {
      card.appendChild(el("p", detail, "result-detail"));
    }
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    heading.focus({ preventScroll: true });
    updateTitle();
  }

  function outcomeMessage(item, intent, outcome) {
    var who = item.customer;
    var amount = money(item.amount);
    if (intent === "reject") {
      return ["stopped", "Not sent. Nothing moved, and your assistant was told no.", ""];
    }
    if (outcome.state === "executed" && outcome.paypal_status === "COMPLETED") {
      return ["sent", "Refund sent. " + amount + " is on its way to " + who + ".",
        "PayPal reference " + outcome.paypal_id + ". Saved with your name and the time."];
    }
    if (outcome.state === "executed") {
      return ["check", "PayPal accepted the " + amount + " refund and is still processing it.",
        "PayPal reference " + outcome.paypal_id + ". Do not send it again. PayPal finishes it on its own."];
    }
    if (outcome.state === "pending_approval") {
      return ["check", "This order changed after your assistant checked it. Nothing was sent.",
        "Please look at it again."];
    }
    if (outcome.state === "executing") {
      return ["check", "PayPal has not answered yet. It will not be sent twice.",
        "We keep trying with the same request, so it cannot be sent twice. It stays on this page until PayPal answers."];
    }
    return ["stopped", "We could not send it. Nothing was sent.", "You can ask your assistant to try again."];
  }

  // Progress and problems appear inside the card the person is looking at,
  // never only in the page header, so nothing important needs a scroll.
  function setProgress(card, text, isProblem) {
    var box = card.querySelector(".progress");
    box.textContent = text;
    box.className = isProblem ? "progress problem" : "progress";
    box.setAttribute("role", isProblem ? "alert" : "status");
    box.hidden = !text;
  }

  function setButtons(card, enabled) {
    card.querySelectorAll(".decision button").forEach(function (b) {
      b.disabled = !enabled;
    });
  }

  function isPastDeadline(item) {
    return Date.now() >= new Date(item.decide_by).getTime();
  }

  function waitingCount() {
    return document.querySelectorAll("#requests .decision").length;
  }

  function updateTitle() {
    var count = waitingCount();
    var title = count === 0 ? "Nothing else is waiting"
      : count === 1 ? "1 refund is waiting for your OK"
      : count + " refunds are waiting for your OK";
    document.getElementById("queue-title").textContent = title;
  }

  // Once the signed decision has left the phone we cannot claim nothing was sent:
  // the server may have acted. Ask whether the request is still waiting instead.
  function recheckAfterLostAnswer(item, card) {
    fetch("/v1/approvals", { headers: sessionHeaders() })
      .then(function (response) {
        return response.ok ? response.json() : Promise.reject(new Error("unavailable"));
      })
      .then(function (items) {
        var stillWaiting = items.some(function (i) { return i.request_id === item.request_id; });
        if (stillWaiting) {
          setButtons(card, true);
          setProgress(card, "Your answer did not reach us. Nothing was sent. You can try again.", true);
        } else {
          showResult(card, "check", "We lost the connection after you decided.",
            "Your decision was received, so do not decide again. Check your PayPal activity for the refund.");
        }
      })
      .catch(function () {
        showResult(card, "check", "We could not confirm what happened.",
          "It will not be sent twice. Check again when you are back online, before doing anything else.");
      });
  }

  function decide(item, intent, card) {
    if (isPastDeadline(item)) {
      showResult(card, "stopped", "Time ran out. Nothing was sent.", "Your assistant can ask again if it is still needed.");
      return;
    }
    setButtons(card, false);
    setProgress(card, intent === "approve"
      ? "Your fingerprint confirms this exact refund: " + money(item.amount) + " to " + item.customer + " for order " + item.order_id + "."
      : "Confirm it's you with your fingerprint or screen lock.", false);
    var path = "/v1/approvals/" + encodeURIComponent(item.request_id);
    var signedAndSent = false;
    post(path + "/options", { intent: intent }, sessionHeaders())
      .then(function (options) {
        return navigator.credentials.get(requestOptions(options));
      })
      .then(function (credential) {
        signedAndSent = true;
        setProgress(card, intent === "approve" ? "Sending to PayPal. Please keep this page open." : "Saving your answer.", false);
        return post(path + "/" + intent, { credential: assertionJson(credential) }, sessionHeaders());
      })
      .then(function (outcome) {
        var message = outcomeMessage(item, intent, outcome);
        showResult(card, message[0], message[1], message[2]);
      })
      .catch(function (error) {
        var status = error && error.status;
        if (status === 409) {
          showResult(card, "check", "This request was already handled.",
            "It was decided or ran out of time. Nothing more will be sent from this screen.");
          return;
        }
        if (status === 401) {
          session = null;
          show("queue", false);
          show("signin", true);
          say("Your sign-in ran out. Nothing was sent. Please sign in again.");
          return;
        }
        if (signedAndSent && (!status || status >= 500)) {
          setProgress(card, "Checking what happened. Please wait.", false);
          recheckAfterLostAnswer(item, card);
          return;
        }
        setButtons(card, true);
        if (error && error.name === "NotAllowedError") {
          setProgress(card, "You closed the fingerprint prompt. Nothing was sent.", true);
        } else if (signedAndSent) {
          setProgress(card, "Your fingerprint could not be confirmed. Nothing was sent. Please try again.", true);
        } else {
          setProgress(card, "We could not start this. Nothing was sent. Please try again.", true);
        }
      });
  }

  // Approved but PayPal has not answered: no buttons, nothing to decide.
  function sendingCard(item) {
    var card = el("section", "", "card result-card check");
    card.appendChild(el("p", "Still sending", "result-badge"));
    card.appendChild(el("h3", "Waiting for PayPal to confirm a " + money(item.amount) + " refund.", "result-title"));
    card.appendChild(el("p", "Order " + item.order_id + ". It cannot be sent twice. Do not send it again another way. Last try " + ago(item.last_try) + ".", "result-detail"));
    return card;
  }

  function requestCard(item) {
    var card = el("section", "", "card");
    var amount = money(item.amount);
    var who = item.customer;
    card.appendChild(el("h3", "Send " + who + " a " + amount + " refund?", "question"));
    var paidWith = item.paid_with ? " (" + item.paid_with + ")" : "";
    card.appendChild(el("p", "Order " + item.order_id + ". " + who + paidWith + ".", "order-line"));
    item.reasons.forEach(function (reason) {
      card.appendChild(el("p", reason, "why"));
    });

    // A safety check that did not run is never shown as passed, and the
    // screen does not nudge toward sending while it is missing.
    var checkMissing = item.safety_check !== "passed";
    if (checkMissing) {
      var box = el("div", "", "safety missing");
      box.appendChild(el("p", "! Our safety check did not run", "safety-title"));
      box.appendChild(el("p", "We could not scan the customer's messages for tricks aimed at your assistant. PayPal did confirm the order and payment."));
      box.appendChild(el("p", "Before you send, check: is this your customer, and is " + amount + " right for this order?", "safety-ask"));
      card.appendChild(box);
    } else {
      card.appendChild(el("p", "\u2713 Safety check passed. No attempt to trick your assistant was found.", "safety passed"));
    }

    if (item.paid) {
      var details = el("details", "", "details");
      details.open = checkMissing;
      details.appendChild(el("summary", "Details from PayPal"));
      details.appendChild(fact("Customer paid", money(item.paid)));
      details.appendChild(fact("Already refunded on this order", money(item.already_refunded)));
      details.appendChild(fact("Can still be refunded now", money(item.refundable_now)));
      if (item.fits === "yes") {
        details.appendChild(fact("Left after this refund", money(item.left_after)));
      }
      details.appendChild(fact("Asked", ago(item.asked_at)));
      card.appendChild(details);
    } else {
      card.appendChild(el("p", "We could not load the details from PayPal right now.", "note"));
    }

    // The refund is larger than what can still be refunded on the order:
    // offer no Send button. The server would refuse it anyway.
    if (item.fits === "no") {
      var blocked = el("div", "", "safety missing");
      blocked.appendChild(el("p", "! This refund can't be sent", "safety-title"));
      blocked.appendChild(el("p", "Only " + money(item.refundable_now) + " can still be refunded on this order, less than " + amount + ". It may have been refunded in PayPal directly."));
      card.appendChild(blocked);
      var close = el("button", "Tell my assistant no", "primary");
      close.type = "button";
      card.appendChild(el("p", "", "progress")).hidden = true;
      var closeArea = el("div", "", "decision");
      close.addEventListener("click", function () {
        decide(item, "reject", card);
      });
      closeArea.appendChild(close);
      closeArea.appendChild(el("p", "Nothing is sent. Your assistant is told no.", "consequence"));
      card.appendChild(closeArea);
      return card;
    }

    var progress = el("p", "", "progress");
    progress.hidden = true;
    card.appendChild(progress);
    var decision = el("div", "", "decision");
    var send = el("button", "Send " + amount + " refund", checkMissing ? "secondary" : "primary");
    send.type = "button";
    send.addEventListener("click", function () {
      decide(item, "approve", card);
    });
    decision.appendChild(send);
    decision.appendChild(el("p", "Money goes back to " + who + " now. Can't be undone.", "consequence"));

    var stop = el("button", "Don't send", "secondary");
    stop.type = "button";
    stop.addEventListener("click", function () {
      decide(item, "reject", card);
    });
    decision.appendChild(stop);
    decision.appendChild(el("p", "Nothing is sent. Your assistant is told no.", "consequence"));
    card.appendChild(decision);
    card.appendChild(el("p", "Please decide by " + clock(item.decide_by) + ". If you do nothing, nothing is sent.", "deadline"));
    return card;
  }

  function loadQueue() {
    fetch("/v1/approvals", { headers: sessionHeaders() })
      .then(function (response) {
        if (response.status === 401) {
          session = null;
          show("queue", false);
          show("signin", true);
          throw new Error("Your session ended. Please sign in again.");
        }
        return response.json();
      })
      .then(function (items) {
        var list = document.getElementById("requests");
        list.replaceChildren();
        items.forEach(function (item) {
          list.appendChild(item.status === "sending" ? sendingCard(item) : requestCard(item));
        });
        show("empty", items.length === 0);
        updateTitle();
      })
      .catch(function (error) {
        say(error.message);
      });
  }

  // --- start -------------------------------------------------------------------------
  if (!window.PublicKeyCredential) {
    say("This browser cannot use passkeys. Open this page in Chrome on your phone.");
    return;
  }
  document.getElementById("enrol-button").addEventListener("click", enrol);
  document.getElementById("signin-button").addEventListener("click", signIn);
  document.getElementById("refresh-button").addEventListener("click", loadQueue);
  if (enrolToken) {
    show("enrol", true);
  } else {
    show("signin", true);
  }
})();
