(function () {
  "use strict";

  var statusBox = document.getElementById("status");
  var session = null;

  function say(text) {
    statusBox.textContent = text;
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
          throw new Error(data.detail || "The request was not accepted.");
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
        say("Set up did not finish: " + error.message);
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
        say("Sign in did not finish: " + error.message);
      });
  }

  // --- the queue -------------------------------------------------------------------
  function money(amount, currency) {
    return currency === "USD" ? "$" + amount : amount + " " + currency;
  }

  function fact(label, value) {
    var row = document.createElement("div");
    row.className = "fact";
    var name = document.createElement("span");
    name.textContent = label;
    var data = document.createElement("span");
    data.textContent = value;
    row.appendChild(name);
    row.appendChild(data);
    return row;
  }

  function paragraph(text, className) {
    var p = document.createElement("p");
    p.textContent = text;
    if (className) {
      p.className = className;
    }
    return p;
  }

  function decide(item, intent, card) {
    var buttons = card.querySelectorAll("button");
    buttons.forEach(function (b) {
      b.disabled = true;
    });
    say("Follow the prompt on your phone.");
    var path = "/v1/approvals/" + encodeURIComponent(item.request_id);
    post(path + "/options", { intent: intent }, sessionHeaders())
      .then(function (options) {
        return navigator.credentials.get(requestOptions(options));
      })
      .then(function (credential) {
        return post(path + "/" + intent, { credential: assertionJson(credential) }, sessionHeaders());
      })
      .then(function (outcome) {
        var text = intent === "approve" ? "Approved. " : "Declined. Nothing was sent. ";
        if (outcome.state === "executed") {
          text = "Approved and sent through PayPal (reference " + outcome.paypal_id + ").";
        } else if (outcome.state === "pending_approval") {
          text = "The order changed, so this needs a fresh look.";
        } else if (outcome.state === "failed") {
          text = "Approved, but PayPal did not complete it. Nothing was sent.";
        }
        card.appendChild(paragraph(text, "result"));
        say("");
      })
      .catch(function (error) {
        card.appendChild(paragraph("Not done: " + error.message, "result failed"));
        buttons.forEach(function (b) {
          b.disabled = false;
        });
        say("");
      });
  }

  function requestCard(item) {
    var card = document.createElement("section");
    card.className = "card";
    var amount = money(item.amount, item.currency);
    var heading = document.createElement("h3");
    heading.textContent = "Refund " + amount + " to the customer on order " + item.order_id + "?";
    card.appendChild(heading);
    card.appendChild(fact("Amount", amount));
    card.appendChild(fact("Order", item.order_id));
    card.appendChild(fact("Asked at", new Date(item.created_at).toLocaleTimeString()));
    card.appendChild(paragraph("Why it needs you: " + item.explanation, "why"));

    var approve = document.createElement("button");
    approve.type = "button";
    approve.className = "primary";
    approve.textContent = "Approve refund of " + amount;
    approve.addEventListener("click", function () {
      decide(item, "approve", card);
    });
    card.appendChild(approve);
    card.appendChild(paragraph("Sends " + amount + " back through PayPal. This cannot be undone.", "consequence"));

    var decline = document.createElement("button");
    decline.type = "button";
    decline.className = "secondary";
    decline.textContent = "Decline";
    decline.addEventListener("click", function () {
      decide(item, "reject", card);
    });
    card.appendChild(decline);
    card.appendChild(paragraph("Nothing is sent.", "consequence"));
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
          list.appendChild(requestCard(item));
        });
        show("empty", items.length === 0);
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
