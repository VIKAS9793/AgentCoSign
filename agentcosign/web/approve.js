(function () {
  "use strict";

  var statusBox = document.getElementById("status");
  var session = null;
  var approver = "";
  var account = "";
  var home = null;
  var skew = 0;
  var current = { view: "", requestId: "" };
  var deciding = false;
  var stream = null;
  var RECONNECT_FIRST_MS = 1000;
  var RECONNECT_MAX_MS = 30000;
  var MS_PER_MINUTE = 60000;
  var MS_PER_DAY = 86400000;
  var TOAST_SHOW_MS = 6000;
  var retryDelay = RECONNECT_FIRST_MS;
  var sessionMinutes = 0;
  var lastVersion = null;
  var reading = null;
  var readAgain = false;
  var lastRead = 0;
  var QUIET_AFTER_READ_MS = 5000;
  var pageBuild = (document.querySelector('meta[name="build"]') || {}).content || "";
  var stale = false;
  var VIEWS = ["enrol", "signin", "home", "review", "final", "sending", "outcome", "paused-view", "activity", "history"];

  function say(text) {
    statusBox.textContent = text;
    statusBox.hidden = !text;
  }

  function show(id, visible) {
    document.getElementById(id).hidden = !visible;
  }

  // One screen at a time, opened at its top.
  function go(view, requestId) {
    VIEWS.forEach(function (name) {
      show(name, name === view);
    });
    current = { view: view, requestId: requestId || "" };
    window.scrollTo(0, 0);
  }

  // Remembered on this phone only, so the sign-in screen can say whose refunds these are.
  function remember(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (ignored) {
      return;
    }
  }

  function recall(key) {
    try {
      return window.localStorage.getItem(key) || "";
    } catch (ignored) {
      return "";
    }
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
  function sessionHeaders() {
    return session ? { "X-Approver-Session": session } : {};
  }

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

  function get(path) {
    return fetch(path, { headers: sessionHeaders() }).then(function (response) {
      if (response.status === 401) {
        signedOut();
        throw new Error("signed out");
      }
      if (!response.ok) {
        throw new Error("unavailable");
      }
      return response.json();
    });
  }

  function signedOut() {
    session = null;
    stopListening();
    showSignin();
    say(sessionMinutes
      ? "Signed out after " + plural(sessionMinutes, "minute", "minutes") + ", for your safety. Open again with your fingerprint."
      : "Signed out for your safety. Open again with your fingerprint.");
  }

  // --- formatting (the server decides; the phone only words and formats) ----------
  function money(amount) {
    var value = Number(amount);
    if (amount === "" || amount === undefined || !isFinite(value)) {
      return "$" + amount;
    }
    return "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function clock(iso) {
    return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function zone() {
    try {
      var parts = new Intl.DateTimeFormat([], { timeZoneName: "short" }).formatToParts(new Date());
      var found = parts.filter(function (p) { return p.type === "timeZoneName"; })[0];
      return found ? " " + found.value : "";
    } catch (ignored) {
      return "";
    }
  }

  // Today, tomorrow or the date; the time zone only where a record is read later.
  function when(iso, withZone) {
    var moment = new Date(iso);
    var today = new Date(serverNow());
    var tomorrow = new Date(today.getTime() + MS_PER_DAY);
    var day = moment.toDateString() === today.toDateString() ? "today"
      : moment.toDateString() === tomorrow.toDateString() ? "tomorrow"
      : moment.toLocaleDateString([], { day: "numeric", month: "short" });
    return day + " at " + clock(iso) + (withZone ? zone() : "");
  }

  // The server's clock, so a wrong phone clock never shortens or stretches the time.
  function serverNow() {
    return Date.now() + skew;
  }

  function timeLeft(iso) {
    var minutes = Math.round((new Date(iso).getTime() - serverNow()) / MS_PER_MINUTE);
    if (minutes <= 0) {
      return "Time ran out";
    }
    return "Approve by " + clock(iso) + " (" + minutes + " min left)";
  }

  function ordinal(n) {
    var value = Number(n);
    var tens = value % 100;
    var suffix = tens >= 11 && tens <= 13 ? "th" : ["th", "st", "nd", "rd"][value % 10] || "th";
    return value + suffix;
  }

  function greeting() {
    var hour = new Date(serverNow()).getHours();
    var part = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    return approver ? part + ", " + approver.split(" ")[0] : part;
  }

  function plural(count, one, many) {
    var value = Number(count);
    return value + " " + (value === 1 ? one : many);
  }

  function capitalised(text) {
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
  }

  // --- building blocks -------------------------------------------------------------
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

  function row(label, value, tone) {
    var line = el("div", "", "row");
    line.appendChild(el("span", label, "row-label"));
    line.appendChild(el("span", value, tone ? "row-value " + tone : "row-value"));
    return line;
  }

  function button(text, className, onClick, intent) {
    var node = el("button", text, className);
    node.type = "button";
    if (intent) {
      node.setAttribute("data-intent", intent);
    }
    node.addEventListener("click", onClick);
    return node;
  }

  function backLink(text, onClick) {
    return withIcon("back", button(text, "back", onClick));
  }

  function pill(text, tone, iconName) {
    var node = el("span", text, "pill " + tone);
    return iconName ? withIcon(iconName, node) : node;
  }

  // Material Symbols, rounded, weight 400 (Apache License 2.0). Drawn inline, so
  // the page loads no outside font and makes no outside request.
  var ICONS = {
    done: "m421-389-98-98q-9-9-22-9t-23 10q-9 9-9 22t9 22l122 123q9 9 21 9t21-9l239-239q10-10 10-23t-10-23q-10-9-23.5-8.5T635-603L421-389Zm59 309q-82 0-155-31.5t-127.5-86Q143-252 111.5-325T80-480q0-83 31.5-156t86-127Q252-817 325-848.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 82-31.5 155T763-197.5q-54 54.5-127 86T480-80Zm0-60q142 0 241-99.5T820-480q0-142-99-241t-241-99q-141 0-240.5 99T140-480q0 141 99.5 240.5T480-140Zm0-340Z",
    problem: "M503.5-289.48q9.5-9.48 9.5-23.5t-9.48-23.52q-9.48-9.5-23.5-9.5t-23.52 9.48q-9.5 9.48-9.5 23.5t9.48 23.52q9.48 9.5 23.5 9.5t23.52-9.48Zm1-152.15q8.5-8.62 8.5-21.37v-193q0-12.75-8.68-21.38-8.67-8.62-21.5-8.62-12.82 0-21.32 8.62-8.5 8.63-8.5 21.38v193q0 12.75 8.68 21.37 8.67 8.63 21.5 8.63 12.82 0 21.32-8.63ZM480.27-80q-82.74 0-155.5-31.5Q252-143 197.5-197.5t-86-127.34Q80-397.68 80-480.5t31.5-155.66Q143-709 197.5-763t127.34-85.5Q397.68-880 480.5-880t155.66 31.5Q709-817 763-763t85.5 127Q880-563 880-480.27q0 82.74-31.5 155.5Q817-252 763-197.68q-54 54.31-127 86Q563-80 480.27-80Zm.23-60Q622-140 721-239.5t99-241Q820-622 721.19-721T480-820q-141 0-240.5 98.81T140-480q0 141 99.5 240.5t241 99.5Zm-.5-340Z",
    caution: "M92-120q-9 0-15.5-4T66-135q-4-7-4.5-14.5T66-165l388-670q5-8 11.5-11.5T480-850q8 0 14.5 3.5T506-835l388 670q5 8 4.5 15.5T894-135q-4 7-10.5 11t-15.5 4H92Zm52-60h672L480-760 144-180Zm361.5-65.5Q514-254 514-267t-8.5-21.5Q497-297 484-297t-21.5 8.5Q454-280 454-267t8.5 21.5Q471-237 484-237t21.5-8.5Zm0-111Q514-365 514-378v-164q0-13-8.5-21.5T484-572q-13 0-21.5 8.5T454-542v164q0 13 8.5 21.5T484-348q13 0 21.5-8.5ZM480-470Z",
    time: "M513-492v-171q0-13-8.5-21.5T483-693q-13 0-21.5 8.5T453-663v183q0 6 2 11t6 10l144 149q9 10 22.5 9.5T650-310q9-9 9-22t-9-22L513-492ZM480-80q-82 0-155-31.5t-127.5-86Q143-252 111.5-325T80-480q0-82 31.5-155t86-127.5Q252-817 325-848.5T480-880q82 0 155 31.5t127.5 86Q817-708 848.5-635T880-480q0 82-31.5 155t-86 127.5Q708-143 635-111.5T480-80Zm0-400Zm0 340q140 0 240-100t100-240q0-140-100-240T480-820q-140 0-240 100T140-480q0 140 100 240t240 100Z",
    hold: "M421.5-328.63q8.5-8.62 8.5-21.37v-260q0-12.75-8.68-21.38-8.67-8.62-21.5-8.62-12.82 0-21.32 8.62-8.5 8.63-8.5 21.38v260q0 12.75 8.68 21.37 8.67 8.63 21.5 8.63 12.82 0 21.32-8.63Zm160 0q8.5-8.62 8.5-21.37v-260q0-12.75-8.68-21.38-8.67-8.62-21.5-8.62-12.82 0-21.32 8.62-8.5 8.63-8.5 21.38v260q0 12.75 8.68 21.37 8.67 8.63 21.5 8.63 12.82 0 21.32-8.63ZM480.27-80q-82.74 0-155.5-31.5Q252-143 197.5-197.5t-86-127.34Q80-397.68 80-480.5t31.5-155.66Q143-709 197.5-763t127.34-85.5Q397.68-880 480.5-880t155.66 31.5Q709-817 763-763t85.5 127Q880-563 880-480.27q0 82.74-31.5 155.5Q817-252 763-197.68q-54 54.31-127 86Q563-80 480.27-80Zm.23-60Q622-140 721-239.5t99-241Q820-622 721.19-721T480-820q-141 0-240.5 98.81T140-480q0 141 99.5 240.5t241 99.5Zm-.5-340Z",
    working: "M323-111q-73-31-127-85t-85-127q-31-73-31-157t31-157q31-73 85-127t127-85q73-31 157-31 12 0 21 9t9 21q0 12-9 21t-21 9q-141 0-240.5 99.5T140-480q0 141 99.5 240.5T480-140q141 0 240.5-99.5T820-480q0-12 9-21t21-9q12 0 21 9t9 21q0 84-31 157t-85 127q-54 54-127 85T480-80q-84 0-157-31Z",
    blocked: "M324-111.5Q251-143 197-197t-85.5-127Q80-397 80-480t31.5-156Q143-709 197-763t127-85.5Q397-880 480-880t156 31.5Q709-817 763-763t85.5 127Q880-563 880-480t-31.5 156Q817-251 763-197t-127 85.5Q563-80 480-80t-156-31.5ZM480-140q61.01 0 117.51-20.5Q654-181 699-220L220-699q-38 46-59 102.17T140-480q0 142.37 98.81 241.19Q337.63-140 480-140Zm259-121q37-45 59-101.49 22-56.5 22-117.51 0-142.38-98.81-241.19T480-820q-60.66 0-116.83 21T261-739l478 478ZM480-480Z",
    fingerprint: "M481-779q108 0 203.5 46T843-601q3 5 2.5 8t-3.5 6q-3 3-7.5 3t-8.5-5q-59-82-150.5-126T481-759q-103 0-193 44.5T138-589q-4 5-7.5 6t-7.5-1q-4-2-4-6.5t2-8.5q62-86 157-133t203-47Zm0 96q136 0 233.5 90T812-371q0 46-34 78t-82 32q-49 0-84-32t-35-78q0-39-28.5-65T481-462q-39 0-68 26t-29 65q0 104 63 173.5T604-100q6 2 7.5 5t.5 7q-1 5-4 7t-8 0q-103-26-169.5-103T364-371q0-47 34.5-79t82.5-32q48 0 82.5 32t34.5 79q0 38 29.5 64t68.5 26q38 0 66.5-26t28.5-64q0-123-91.5-206T481-660q-127 0-218.5 83T171-371q0 24 5.5 62.5T200-221q2 5 0 7.5t-5 4.5q-4 2-8.5 1t-6.5-6q-13-38-20.5-77.5T152-371q0-129 98-220.5T481-683Zm0-197q65 0 127.5 16T728-819q5 2 5.5 6t-1.5 7q-2 3-5.5 5t-8.5 0q-55-27-115-42.5T481-859q-62 0-121 14.5T247-801q-5 2-7.5.5T235-805q-2-2-2-6t3-6q57-31 119.5-47T481-880Zm0 298q92 0 158.5 61T706-371q0 5-2.5 7.5T696-361q-5 0-8-2.5t-3-7.5q0-81-60.5-136T481-562q-83 0-142.5 55T279-371q0 85 29.5 145T396-106q4 4 3.5 7.5T396-92q-2 2-6.5 3.5T381-92q-58-60-90.5-126T258-371q0-89 65.5-150T481-582Zm-1 200q5 0 7.5 3t2.5 8q0 81 59.5 133.5T687-185q8 0 19-1t24-3q5-1 8 1.5t4 5.5q1 4-.5 7t-6.5 4q-18 5-31.5 5.5t-16.5.5q-88 0-152.5-58.5T470-371q0-5 2.5-8t7.5-3Z",
    back: "m274-450 227 227q9 9 9 21t-9 21q-9 9-21 9t-21-9L181-459q-5-5-7-10t-2-11q0-6 2-11t7-10l278-278q9-9 21-9t21 9q9 9 9 21t-9 21L274-510h496q13 0 21.5 8.5T800-480q0 13-8.5 21.5T770-450H274Z",
    copy: "M300-200q-24 0-42-18t-18-42v-560q0-24 18-42t42-18h440q24 0 42 18t18 42v560q0 24-18 42t-42 18H300Zm0-60h440v-560H300v560ZM180-80q-24 0-42-18t-18-42v-590q0-12.75 8.68-21.38 8.67-8.62 21.5-8.62 12.82 0 21.32 8.62 8.5 8.63 8.5 21.38v590h470q12.75 0 21.38 8.68 8.62 8.67 8.62 21.5 0 12.82-8.62 21.32Q662.75-80 650-80H180Zm120-180v-560 560Z",
    history: "M477-120q-142 0-243.5-95.5T121-451q-1-12 7.5-21t21.5-9q12 0 20.5 8.5T181-451q11 115 95 193t201 78q127 0 215-89t88-216q0-124-89-209.5T477-780q-68 0-127.5 31T246-667h75q13 0 21.5 8.5T351-637q0 13-8.5 21.5T321-607H172q-13 0-21.5-8.5T142-637v-148q0-13 8.5-21.5T172-815q13 0 21.5 8.5T202-785v76q52-61 123.5-96T477-840q75 0 141 28t115.5 76.5Q783-687 811.5-622T840-482q0 75-28.5 141t-78 115Q684-177 618-148.5T477-120Zm34-374 115 113q9 9 9 21.5t-9 21.5q-9 9-21 9t-21-9L460-460q-5-5-7-10.5t-2-11.5v-171q0-13 8.5-21.5T481-683q13 0 21.5 8.5T511-653v159Z",
    refresh: "M480-160q-133 0-226.5-93.5T160-480q0-133 93.5-226.5T480-800q85 0 149 34.5T740-671v-99q0-13 8.5-21.5T770-800q13 0 21.5 8.5T800-770v194q0 13-8.5 21.5T770-546H576q-13 0-21.5-8.5T546-576q0-13 8.5-21.5T576-606h138q-38-60-97-97t-137-37q-109 0-184.5 75.5T220-480q0 109 75.5 184.5T480-220q75 0 140-39.5T717-366q5-11 16.5-16.5t22.5-.5q12 5 16 16.5t-1 23.5q-39 84-117.5 133.5T480-160Z",
    declined: "m480-438 129 129q9 9 21 9t21-9q9-9 9-21t-9-21L522-480l129-129q9-9 9-21t-9-21q-9-9-21-9t-21 9L480-522 351-651q-9-9-21-9t-21 9q-9 9-9 21t9 21l129 129-129 129q-9 9-9 21t9 21q9 9 21 9t21-9l129-129Zm0 358q-82 0-155-31.5t-127.5-86Q143-252 111.5-325T80-480q0-83 31.5-156t86-127Q252-817 325-848.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 82-31.5 155T763-197.5q-54 54.5-127 86T480-80Zm0-60q142 0 241-99.5T820-480q0-142-99-241t-241-99q-141 0-240.5 99T140-480q0 141 99.5 240.5T480-140Zm0-340Z",
    info: "M504.5-288.63q8.5-8.62 8.5-21.37v-180q0-12.75-8.68-21.38-8.67-8.62-21.5-8.62-12.82 0-21.32 8.62-8.5 8.63-8.5 21.38v180q0 12.75 8.68 21.37 8.67 8.63 21.5 8.63 12.82 0 21.32-8.63Zm-1-314.57q9.5-9.2 9.5-22.8 0-14.45-9.48-24.22-9.48-9.78-23.5-9.78t-23.52 9.78Q447-640.45 447-626q0 13.6 9.48 22.8 9.48 9.2 23.5 9.2t23.52-9.2ZM480.27-80q-82.74 0-155.5-31.5Q252-143 197.5-197.5t-86-127.34Q80-397.68 80-480.5t31.5-155.66Q143-709 197.5-763t127.34-85.5Q397.68-880 480.5-880t155.66 31.5Q709-817 763-763t85.5 127Q880-563 880-480.27q0 82.74-31.5 155.5Q817-252 763-197.68q-54 54.31-127 86Q563-80 480.27-80Zm.23-60Q622-140 721-239.5t99-241Q820-622 721.19-721T480-820q-141 0-240.5 98.81T140-480q0 141 99.5 240.5t241 99.5Zm-.5-340Z",
    offline: "M248-171q-88 0-148-59T40-377q0-80 50.5-134T217-577q2-14 6.5-31.5T236-640L91-785q-9-9-9-21t9-21q9-9 21-9t21 9l707 707q9 9 9 21t-9 21q-9 9-21.5 9T797-78l-94-93H248Zm0-60h397L285-591q-11 15-14.5 34t-3.5 37h-19q-62 0-105 39.5t-43 101q0 61.5 43 105T248-231Zm216-181Zm390 210-47-47q25-17 39-38t14-50q0-43-31-73.5T755-441h-67v-81q0-88-61-147.5T478.47-729q-28.47 0-60.97 9T358-691l-42-42q36-29 77.5-42.5T478-789q111 0 190.5 79T748-520v21q72-1 122 45t50 117q0 35-16.5 73.5T854-202ZM583-470Z",
    verified: "m437-433-73-76q-9-10-22-10t-23 9q-10 10-10 23t10 23l97 96q9 9 21 9t21-9l183-182q9-9 9-22t-10-22q-9-8-21.5-7.5T598-593L437-433ZM332-84l-62-106-124-25q-11-2-18.5-12t-5.5-21l14-120-79-92q-8-8-8-20t8-20l79-91-14-120q-2-11 5.5-21t18.5-12l124-25 62-107q6-10 17-14t22 1l109 51 109-51q11-5 22-1.5t17 13.5l63 108 123 25q11 2 18.5 12t5.5 21l-14 120 79 91q8 8 8 20t-8 20l-79 92 14 120q2 11-5.5 21T814-215l-123 25-63 107q-6 10-17 13.5T589-71l-109-51-109 51q-11 5-22 1t-17-14Zm41-55 107-45 110 45 67-100 117-30-12-119 81-92-81-94 12-119-117-28-69-100-108 45-110-45-67 100-117 28 12 119-81 94 81 92-12 121 117 28 70 100Zm107-341Z",
    tampered: "m480-436 72 69q9 9 22 9t22-9q10-10 10-23t-10-22l-70-69 70-70q10-9 10-22t-10-23q-9-9-22-9t-22 9l-72 69-72-69q-9-9-21.5-9.5T364-596q-9 9-9.5 22.5T364-551l70 70-70 69q-10 9-10 22t10 23q9 9 22 9t22-9l72-69Zm-9.5 351q-4.5-1-9.5-3-139-47-220-168.5T160-523v-196q0-19 11-34.5t28-22.5l260-97q11-4 21-4t21 4l260 97q17 7 28 22.5t11 34.5v196q0 145-81 266.5T499-88q-5 2-9.5 3t-9.5 1q-5 0-9.5-1Zm9.5-58q115-38 187.5-143.5T740-523v-196l-260-98-260 98v196q0 131 72.5 236.5T480-143Zm0-337Z",
    lock: "M220-80q-24.75 0-42.37-17.63Q160-115.25 160-140v-434q0-24.75 17.63-42.38Q195.25-634 220-634h70v-96q0-78.85 55.61-134.42Q401.21-920 480.11-920q78.89 0 134.39 55.58Q670-808.85 670-730v96h70q24.75 0 42.38 17.62Q800-598.75 800-574v434q0 24.75-17.62 42.37Q764.75-80 740-80H220Zm0-60h520v-434H220v434Zm314.5-162.03Q557-324.06 557-355q0-30-22.67-54.5t-54.5-24.5q-31.83 0-54.33 24.5t-22.5 55q0 30.5 22.67 52.5t54.5 22q31.83 0 54.33-22.03ZM350-634h260v-96q0-54.17-37.88-92.08-37.88-37.92-92-37.92T388-822.08q-38 37.91-38 92.08v96ZM220-140v-434 434Z",
  };

  function icon(name, className) {
    var svgNs = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(svgNs, "svg");
    svg.setAttribute("viewBox", "0 -960 960 960");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", className ? "icon " + className : "icon");
    var path = document.createElementNS(svgNs, "path");
    path.setAttribute("d", ICONS[name]);
    svg.appendChild(path);
    return svg;
  }

  // An icon before the words; the words always carry the meaning too.
  function withIcon(name, node, className) {
    node.insertBefore(icon(name, className), node.firstChild);
    node.classList.add("has-icon");
    return node;
  }

  function progressLine() {
    var box = el("p", "", "progress");
    box.hidden = true;
    return box;
  }

  function paymentLine(item) {
    var parts = ["Order ending " + item.order_ending];
    if (item.paid_with) {
      parts.push(capitalised(item.paid_with));
    }
    return parts.join(" · ");
  }

  function allItems() {
    if (!home) {
      return [];
    }
    return Object.keys(home.sections).reduce(function (list, key) {
      return list.concat(home.sections[key]);
    }, []);
  }

  function findItem(requestId) {
    return allItems().filter(function (i) { return i.request_id === requestId; })[0] || null;
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
        return post("/v1/approver/enrolment", { approver: name, credential: registrationJson(credential) }, headers);
      })
      .then(function () {
        history.replaceState(null, "", window.location.pathname);
        remember("approver", name);
        showSignin();
        say("This phone is set up. Open your refunds with your fingerprint.");
      })
      .catch(function (error) {
        say(error && error.name === "NotAllowedError"
          ? "You closed the fingerprint prompt. This phone is not set up yet."
          : "Set up did not finish. The link may have run out. Ask for a new link.");
      });
  }

  // --- sign in -------------------------------------------------------------------
  function setIdentity() {
    var line = document.getElementById("identity");
    line.textContent = approver + (account ? " · PayPal ending " + account : "");
    line.hidden = !session;
  }

  function showSignin() {
    var known = recall("approver");
    var knownAccount = recall("account");
    document.getElementById("signin-who").textContent = known;
    document.getElementById("signin-account").textContent = knownAccount ? "PayPal account ending " + knownAccount : "";
    show("signin-card", Boolean(known));
    setIdentity();
    go("signin");
  }

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
        approver = data.approver || "";
        account = data.account || "";
        sessionMinutes = Number(data.session_minutes) || 0;
        remember("approver", approver);
        remember("account", account);
        say("");
        setIdentity();
        openHome();
        listen();
      })
      .catch(function (error) {
        if (error && error.status === 409) {
          say("This phone is not set up yet. Ask for a setup link, then open it on this phone.");
          return;
        }
        say(error && error.name === "NotAllowedError"
          ? "Fingerprint cancelled. Try again when you're ready."
          : "Couldn't open your refund requests. Try again.");
      });
  }

  // --- reading the server -----------------------------------------------------------
  // One read at a time: changes that arrive meanwhile become a single follow-up
  // read, so bursts never turn into a flood of PayPal checks.
  function readHome() {
    if (reading) {
      readAgain = true;
      return reading;
    }
    reading = get("/v1/approvals/home").then(function (data) {
      home = data;
      skew = new Date(data.server_now).getTime() - Date.now();
      approver = data.approver_display || approver;
      account = data.account_ending || account;
      lastRead = Date.now();
      setIdentity();
      checkBuild(data.build);
      return data;
    });
    reading.then(followUp, followUp);
    return reading;
  }

  // The server was updated while this page was open. Sending waits for a reload,
  // so an old screen never acts on a newer server. Nothing is reloaded for them.
  function checkBuild(build) {
    if (!build || build === pageBuild || stale) {
      return;
    }
    stale = true;
    removeSendButtons();
    statusBox.replaceChildren();
    statusBox.appendChild(el("span", "App updated. No money moved. Reload to continue. "));
    statusBox.appendChild(button("Reload", "link", function () {
      window.location.reload();
    }));
    statusBox.hidden = false;
  }

  function removeSendButtons() {
    document.querySelectorAll(".decision").forEach(function (box) {
      box.remove();
    });
  }

  function followUp() {
    reading = null;
    if (readAgain) {
      readAgain = false;
      somethingChanged();
    }
  }

  // --- home: what needs the merchant, then their day ----------------------------------
  function openHome() {
    go("home");
    say("");
    document.getElementById("greeting").textContent = greeting();
    if (!home) {
      show("home-loading", true);
      show("home-body", false);
    }
    return readHome()
      .then(renderHome)
      .catch(function (error) {
        if (error.message !== "signed out") {
          show("home-loading", false);
          say("Couldn't load your refund requests. No money moved. Try again.");
        }
      });
  }

  function renderHome(data) {
    document.getElementById("greeting").textContent = greeting();
    var counts = data.counts;
    var parts = [plural(counts.needs_decision, "waiting for your approval", "waiting for your approval")];
    if (Number(counts.paused)) {
      parts.push(counts.paused + " on hold");
    }
    document.getElementById("home-count").textContent = parts.join(" · ");
    document.getElementById("freshness").textContent = freshness(data);

    var notices = document.getElementById("notices");
    notices.replaceChildren();
    data.sections.failed.forEach(function (item) { notices.appendChild(noticeCard(item, "failed")); });
    data.sections.sending.forEach(function (item) { notices.appendChild(noticeCard(item, "sending")); });
    reconciliationNotices(data.reconciliation || {}).forEach(function (card) { notices.appendChild(card); });

    fill("requests", data.sections.needs_decision, requestCard);
    fill("cant-send-list", data.sections.cant_send, requestCard);
    fill("paused-list", data.sections.paused, pausedCard);
    show("empty", data.sections.needs_decision.length === 0);
    show("cant-send-section", data.sections.cant_send.length > 0);
    show("paused-section", data.sections.paused.length > 0);
    document.getElementById("today-title").textContent = "Your last " + data.settings.window_hours + " hours";
    renderToday(data.today, data.settings);
    renderDone(data.done);
    renderSettings(data.settings);
    renderStop(data.refunds_stopped === "yes");
    show("home-loading", false);
    show("home-body", true);
  }

  // Only says PayPal was checked when the server says it really was.
  function freshness(data) {
    if (data.paypal_checked === "no") {
      return "Can't reach PayPal. No refunds can go out until it's back.";
    }
    if (data.paypal_checked === "partial") {
      return "Some PayPal details couldn't be checked, so those refunds can't go out for now.";
    }
    return data.checked_at ? "Up to date with PayPal " + when(data.checked_at) : "";
  }

  function fill(id, items, make) {
    var box = document.getElementById(id);
    box.replaceChildren();
    items.forEach(function (item) { box.appendChild(make(item)); });
  }

  var CANT_SEND_WORDS = {
    time_up: "Time ran out. No money moved.",
    paypal_unavailable: "Can't reach PayPal for this order",
    not_enough_left: "Not enough left on this order",
    stopped: "Refunds are stopped. You can still decline.",
  };

  // A short card: who, how much, which order, and how long is left.
  function requestCard(item) {
    var card = el("button", "", "card request-card");
    card.type = "button";
    var top = el("span", "", "card-top");
    top.appendChild(el("span", item.customer, "card-name"));
    top.appendChild(el("span", money(item.amount), "card-amount"));
    card.appendChild(top);
    card.appendChild(el("span", paymentLine(item), "muted small"));
    if (item.can_send === "yes") {
      card.appendChild(withIcon("time", el("span", timeLeft(item.decide_by), "caution-text small strong")));
    } else {
      card.appendChild(withIcon("blocked", el("span", CANT_SEND_WORDS[item.cant_send_reason] || "Can't be refunded", "danger-text small strong")));
    }
    card.addEventListener("click", function () {
      openReview(item);
    });
    return card;
  }

  // The last daily comparison with PayPal, as the server recorded it.
  function reconciliationNotices(run) {
    var cards = [];
    var checked = run.at ? " at the last check (" + when(run.at) + ")" : "";
    if (Number(run.breaks) > 0) {
      var problem = el("div", "", "card notice danger-box");
      problem.appendChild(withIcon("problem", el("p", "Some refunds don't match PayPal", "box-title")));
      problem.appendChild(el("p", plural(run.breaks, "refund differs", "refunds differ") + " from PayPal's records" + checked + ". Check your PayPal activity. This check never moves money.", "small"));
      cards.push(problem);
    }
    if (Number(run.unchecked) > 0) {
      var unchecked = el("div", "", "card notice caution-box");
      unchecked.appendChild(withIcon("offline", el("p", plural(run.unchecked, "order", "orders") + " couldn't be checked with PayPal" + checked + ". We'll try again.", "small")));
      cards.push(unchecked);
    }
    if (Number(run.outside) > 0) {
      cards.push(withIcon("info", el("p", plural(run.outside, "refund was", "refunds were") + " made in PayPal directly in the last " + run.lookback_days + " days.", "muted small")));
    }
    return cards;
  }

  function noticeCard(item, kind) {
    var card = el("div", "", "card notice " + (kind === "failed" ? "danger-box" : "caution-box"));
    if (kind === "failed") {
      card.appendChild(pill("Didn't go through", "danger-pill", "problem"));
      card.appendChild(el("p", "The " + money(item.amount) + " refund to " + item.customer + " didn't go through at PayPal.", "strong"));
      card.appendChild(el("p", "Order ending " + item.order_ending + ". No money reached them. You may want to contact them.", "small"));
    } else {
      card.appendChild(pill("Processing at PayPal", "caution-pill", "working"));
      card.appendChild(el("p", "Waiting for PayPal to confirm the " + money(item.amount) + " refund.", "strong"));
      card.appendChild(el("p", home && home.refunds_stopped === "yes"
        ? "Order ending " + item.order_ending + ". Refunds are stopped: if PayPal hasn't received it, it is sent when you turn refunds back on. It can't go out twice."
        : "Order ending " + item.order_ending + ". It can't go out twice. Don't refund it again in PayPal.", "small"));
    }
    return card;
  }

  function repeatLine(item) {
    var limit = home ? home.settings.repeat_limit : "";
    var hours = home ? home.settings.window_hours : "";
    return "This would be this customer's " + ordinal(item.count_in_window) + " refund in " + hours + " hours. Your limit is " + limit + ".";
  }

  function pausedCard(item) {
    var card = el("button", "", "card request-card held");
    card.type = "button";
    var top = el("span", "", "card-top");
    top.appendChild(el("span", item.customer, "card-name"));
    top.appendChild(el("span", money(item.amount), "card-amount"));
    card.appendChild(top);
    card.appendChild(el("span", repeatLine(item), "muted small"));
    card.appendChild(withIcon("hold", el("span", item.rechecking === "yes" ? "Being reviewed again now" : "Reviewed again " + when(item.release_at), "small strong")));
    card.addEventListener("click", function () {
      openPaused(item);
    });
    return card;
  }

  var OUTCOME_WORDS = { executed: "Refunded", denied: "Not refunded", expired: "Time ran out", failed: "Not refunded" };

  function doneRow(item, back) {
    var line = el("button", "", "row row-button");
    line.type = "button";
    line.appendChild(el("span", item.customer + " · " + money(item.amount), "row-label"));
    var confirmed = item.outcome === "executed" && item.paypal_status === "COMPLETED";
    var word = item.outcome === "executed" && !confirmed ? "Processing at PayPal" : OUTCOME_WORDS[item.outcome] || "Closed";
    var mark = confirmed ? "done" : item.outcome === "executed" ? "working" : "declined";
    line.appendChild(withIcon(mark, el("span", word, confirmed ? "row-value ok-text" : "row-value")));
    line.addEventListener("click", function () {
      openHistory(item.request_id, back);
    });
    return line;
  }

  function renderDone(done) {
    var box = document.getElementById("done-list");
    box.replaceChildren();
    done.recent.forEach(function (item) { box.appendChild(doneRow(item, openHome)); });
    if (Number(done.total) > done.recent.length) {
      box.appendChild(button("See all " + done.total, "link", openActivity));
    }
    show("done-section", done.recent.length > 0);
  }

  function renderToday(day, settings) {
    var box = document.getElementById("today");
    box.replaceChildren();
    box.appendChild(el("p", money(day.sent_total) + " refunded", "big-number"));
    var lines = el("div", "", "list");
    lines.appendChild(row("Refunded with your approval", day.by_you_count));
    lines.appendChild(row("Refunded by your assistant", day.by_assistant_count));
    lines.appendChild(row("Not refunded", day.not_sent_count));
    if (Number(day.processing_count) > 0) {
      lines.appendChild(row("Processing at PayPal", day.processing_count + " (" + money(day.processing_total) + ")"));
    }
    lines.appendChild(row("Daily limit used", money(day.limit_used) + " of " + money(settings.daily_cap)));
    box.appendChild(lines);
    if (Number(day.waiting_total) > 0) {
      box.appendChild(el("p", money(day.waiting_total) + " of this is held for open requests", "muted small"));
    }
  }

  function renderSettings(settings) {
    var box = document.getElementById("settings");
    box.replaceChildren();
    box.appendChild(row("Your assistant refunds on its own up to", money(settings.auto_limit)));
    box.appendChild(row("Needs your approval up to", money(settings.max_limit)));
    box.appendChild(row("Refunds per customer in " + settings.window_hours + " hours", settings.repeat_limit));
    box.appendChild(row("Daily refund limit", money(settings.daily_cap)));
    box.appendChild(row("Time to approve", settings.decide_minutes + " minutes"));
    box.appendChild(el("p", "You can't change these in the app yet.", "muted small"));
  }

  // --- emergency stop: one action stops every refund; only a fingerprint resumes ----
  function renderStop(stopped) {
    var banner = document.getElementById("stop-banner");
    banner.replaceChildren();
    if (stopped) {
      banner.appendChild(withIcon("blocked", el("p", "All refunds are stopped", "box-title")));
      banner.appendChild(el("p", "No money moves until you turn refunds back on. You can still decline requests.", "small"));
      banner.appendChild(withIcon("fingerprint", button("Turn refunds back on", "primary", function () {
        control("resume");
      })));
    }
    banner.hidden = !stopped;
    var box = document.getElementById("stop-control");
    box.replaceChildren();
    if (!stopped) {
      box.appendChild(withIcon("blocked", button("Stop all refunds", "secondary", function () {
        control("stop");
      })));
      box.appendChild(el("p", "Stops new refunds and approvals at once. Refunds already with PayPal will finish.", "muted small"));
    }
  }

  function control(intent) {
    var path = "/v1/controls/" + intent;
    var signedAndSent = false;
    post(path + "/options", {}, sessionHeaders())
      .then(function (options) {
        return navigator.credentials.get(requestOptions(options));
      })
      .then(function (credential) {
        signedAndSent = true;
        return post(path, { credential: assertionJson(credential) }, sessionHeaders());
      })
      .then(function (result) {
        var still = Number(result.still_sending);
        if (result.refunds_stopped === "yes") {
          toast("All refunds are stopped." + (still ? " " + plural(still, "refund", "refunds") + " already with PayPal will finish." : ""));
        } else {
          toast("Refunds are back on.");
        }
        return openHome();
      })
      .catch(function (failure) {
        if (failure && failure.status === 401) {
          signedOut();
          return;
        }
        var status = failure && failure.status;
        if (signedAndSent && (!status || status >= 500)) {
          // the answer was lost after the fingerprint left the phone: read the
          // real state instead of guessing what happened
          readHome().then(function (data) {
            toast(data.refunds_stopped === "yes"
              ? "We couldn't confirm the change. Refunds are stopped right now."
              : "We couldn't confirm the change. Refunds are on right now.");
            renderHome(data);
          }).catch(function () {
            toast("We couldn't confirm the change. Check again before doing anything else.");
          });
          return;
        }
        toast(controlFailureText(status, failure));
      });
  }

  // --- review one request ----------------------------------------------------------
  function openReview(item) {
    var view = document.getElementById("review");
    view.replaceChildren();
    var sendable = item.can_send === "yes";

    view.appendChild(backLink("Refund requests", openHome));
    view.appendChild(el("h1", sendable ? "Refund " + money(item.amount) + " to " + item.customer + "?"
      : item.customer + " · " + money(item.amount), "question"));
    view.appendChild(el("p", paymentLine(item), "muted small"));
    var fresh = item.checked_at ? "Up to date with PayPal " + when(item.checked_at) : "";
    var meta = sendable ? [fresh, timeLeft(item.decide_by)] : [fresh];
    view.appendChild(el("p", meta.filter(Boolean).join(" · "), "muted small"));
    view.appendChild(el("div", "", "change-banner-slot"));
    view.appendChild(sendable ? whyBox(item) : stopBox(item));
    if (item.paid) {
      view.appendChild(factsTable(item));
    }
    view.appendChild(progressLine());

    var actions = el("div", "", "actions decision");
    if (sendable) {
      actions.appendChild(button("Continue to refund", "primary", function () {
        openFinal(item);
      }));
      actions.appendChild(button("Decline", "secondary", function () {
        decide(item, "reject", view);
      }, "reject"));
      actions.appendChild(el("p", "If you do nothing, no money moves.", "muted small center"));
    } else if (item.cant_send_reason === "not_enough_left" || item.cant_send_reason === "stopped") {
      actions.appendChild(button("Decline request", "primary", function () {
        decide(item, "reject", view);
      }, "reject"));
      actions.appendChild(el("p", "No money moves. Your assistant is told it can't be refunded.", "muted small center"));
    } else {
      actions.appendChild(button("Back to refund requests", "secondary", openHome));
    }
    view.appendChild(actions);
    go("review", item.request_id);
    if (stale) {
      removeSendButtons();
    }
  }

  function whyBox(item) {
    var box = el("div", "", "card caution-box");
    box.appendChild(withIcon("caution", el("p", "Needs your approval", "box-title")));
    item.reasons.forEach(function (reason) {
      box.appendChild(el("p", reason, "small"));
    });
    if (item.scan_note) {
      box.appendChild(el("p", item.scan_note, "small"));
    }
    if (item.safety_check !== "passed") {
      box.appendChild(el("p", "Check the customer, order and amount before you approve.", "small strong"));
    }
    return box;
  }

  // The figures that decide, in one table that always adds up.
  function factsTable(item) {
    var table = el("div", "", "card list");
    table.appendChild(row("Customer paid", money(item.paid)));
    if (Number(item.refunded_outside) > 0) {
      table.appendChild(row("Refunded through this app", money(item.refunded_in_app)));
      table.appendChild(row("Refunded in PayPal directly", money(item.refunded_outside)));
    } else {
      table.appendChild(row("Already refunded", money(item.already_refunded)));
    }
    if (item.waiting_note) {
      table.appendChild(row("Held for other requests", money(item.waiting_on_order)));
    }
    table.appendChild(row("Can still be refunded", money(item.refundable_now)));
    table.appendChild(row("This refund", money(item.amount), "strong"));
    if (item.can_send === "yes") {
      table.appendChild(row("Left to refund after this", money(item.left_after)));
    }
    return table;
  }

  function stopBox(item) {
    if (item.cant_send_reason === "stopped") {
      var held = el("div", "", "card caution-box");
      held.appendChild(withIcon("blocked", el("p", "All refunds are stopped", "box-title")));
      held.appendChild(el("p", "Turn refunds back on to approve this. No money moved.", "small"));
      return held;
    }
    var box = el("div", "", "card danger-box");
    if (item.cant_send_reason === "time_up") {
      box.appendChild(el("p", "Time ran out", "box-title"));
      box.appendChild(el("p", "No money moved. Your assistant can ask again if it's still needed.", "small"));
      return box;
    }
    if (item.cant_send_reason === "paypal_unavailable") {
      box.appendChild(el("p", "Can't reach PayPal for this order", "box-title"));
      box.appendChild(el("p", "You can approve once PayPal answers. No money moved.", "small"));
      return box;
    }
    box.appendChild(el("p", Number(item.refundable_now) > 0
      ? "Only " + money(item.refundable_now) + " can still be refunded on this order, less than " + money(item.amount) + "."
      : "Nothing more can be refunded on this order.", "box-title"));
    if (Number(item.refunded_outside) > 0) {
      box.appendChild(el("p", money(item.refunded_outside) + " was refunded from your PayPal account outside this app. If that was not you, check your PayPal activity.", "small"));
    }
    return box;
  }

  // --- final check: restates the exact refund the fingerprint will sign ------------
  function openFinal(item) {
    if (item.can_send !== "yes") {
      openReview(item);
      return;
    }
    var view = document.getElementById("final");
    view.replaceChildren();
    view.appendChild(backLink("Review request", function () { openReview(item); }));
    view.appendChild(el("p", "Check before you refund", "eyebrow caution-text"));
    var head = el("div", "", "card center");
    head.appendChild(el("p", money(item.amount), "big-number"));
    head.appendChild(el("p", "to " + item.customer, "strong"));
    head.appendChild(el("p", paymentLine(item), "muted small"));
    view.appendChild(head);
    var table = el("div", "", "card list");
    table.appendChild(row("Customer paid", money(item.paid)));
    table.appendChild(row("Already refunded", money(item.already_refunded)));
    if (Number(item.waiting_on_order) > 0) {
      table.appendChild(row("Held for other requests", money(item.waiting_on_order)));
    }
    table.appendChild(row("Left to refund after this", money(item.left_after)));
    view.appendChild(table);
    view.appendChild(el("div", "", "change-banner-slot"));
    var warn = el("div", "", "card caution-box");
    warn.appendChild(withIcon("caution", el("p", money(item.amount) + " goes back to " + item.customer + " now. Refunds can't be undone.", "box-title")));
    warn.appendChild(el("p", "From your PayPal account ending " + account + ". Your fingerprint approves this refund only.", "small"));
    view.appendChild(warn);
    view.appendChild(progressLine());
    var actions = el("div", "", "actions decision");
    actions.appendChild(withIcon("fingerprint", button("Refund " + money(item.amount), "primary", function () {
      decide(item, "approve", view);
    }, "approve")));
    actions.appendChild(button("Go back", "secondary", function () {
      openReview(item);
    }));
    actions.appendChild(el("p", "No money moves until you confirm with your fingerprint.", "muted small center"));
    view.appendChild(actions);
    go("final", item.request_id);
    if (stale) {
      removeSendButtons();
    }
  }

  // --- deciding ----------------------------------------------------------------------
  function setProgress(view, text, isProblem) {
    var box = view.querySelector(".progress");
    box.textContent = text;
    box.className = isProblem ? "progress problem" : "progress";
    box.setAttribute("role", isProblem ? "alert" : "status");
    box.hidden = !text;
  }

  function setButtons(view, enabled) {
    view.querySelectorAll(".decision button").forEach(function (b) {
      b.disabled = !enabled;
      if (enabled && b.getAttribute("data-label")) {
        b.textContent = b.getAttribute("data-label");
        b.classList.remove("busy");
      }
    });
  }

  function busy(view, intent, text) {
    var target = view.querySelector('.decision button[data-intent="' + intent + '"]');
    if (!target) {
      return;
    }
    if (!target.getAttribute("data-label")) {
      target.setAttribute("data-label", target.textContent);
    }
    target.textContent = text;
    target.classList.add("busy");
  }

  function isPastDeadline(item) {
    return Boolean(item.decide_by) && serverNow() >= new Date(item.decide_by).getTime();
  }

  function decide(item, intent, view) {
    if (stale) {
      return;
    }
    if (intent === "approve" && isPastDeadline(item)) {
      showOutcome("stopped", "Time ran out. No money moved.", ["Your assistant can ask again if it's still needed."], item);
      return;
    }
    deciding = true;
    setButtons(view, false);
    busy(view, intent, "Waiting for your fingerprint");
    setProgress(view, intent === "approve"
      ? "Confirm with your fingerprint to refund " + money(item.amount) + " to " + item.customer + "."
      : "Confirm it's you with your fingerprint.", false);
    var path = "/v1/approvals/" + encodeURIComponent(item.request_id);
    var signedAndSent = false;
    post(path + "/options", { intent: intent }, sessionHeaders())
      .then(function (options) {
        return navigator.credentials.get(requestOptions(options));
      })
      .then(function (credential) {
        signedAndSent = true;
        if (intent === "approve") {
          startTracker();
          document.querySelector("#sending h1").textContent = "Refunding " + money(item.amount) + " to " + item.customer;
          go("sending", item.request_id);
        } else {
          busy(view, intent, "Saving");
        }
        return post(path + "/" + intent, { credential: assertionJson(credential) }, sessionHeaders());
      })
      .then(function (outcome) {
        deciding = false;
        if (intent === "approve") {
          finishTracker(item, outcome);
          return;
        }
        afterDecision(item, intent, outcome);
      })
      .catch(function (failure) {
        deciding = false;
        var status = failure && failure.status;
        if (status === 409) {
          // the server refused the decision: read it again to say why
          readHome().then(function (data) {
            var refused = refusedOutcome(data, findItem(item.request_id));
            showOutcome(refused[0], refused[1], refused[2], item);
          }).catch(function () {
            showOutcome("check", "This refund request is already closed.", ["No money will move from this screen."], item);
          });
          return;
        }
        if (status === 401) {
          signedOut();
          return;
        }
        if (signedAndSent && (!status || status >= 500)) {
          recheckAfterLostAnswer(item, intent, view);
          return;
        }
        if (current.view === "sending") {
          go(intent === "approve" ? "final" : "review", item.request_id);
        }
        setButtons(view, true);
        if (failure && failure.name === "NotAllowedError") {
          setProgress(view, "Fingerprint cancelled. No money moved.", true);
        } else if (signedAndSent) {
          setProgress(view, "Fingerprint not recognised. No money moved. Try again.", true);
        } else {
          setProgress(view, "Couldn't start the fingerprint check. No money moved. Try again.", true);
        }
      });
  }

  function afterDecision(item, intent, outcome) {
    var amount = money(item.amount);
    if (intent === "reject") {
      showOutcome("stopped", "Declined", [
        "No money moved. Your assistant has been told.",
        "If the customer asks again, you'll see a new request.",
      ], item);
      return;
    }
    if (outcome.state === "executed" && outcome.paypal_status === "COMPLETED") {
      showOutcome("sent", amount + " refunded to " + item.customer, [
        "PayPal confirmed it. Nothing else to do.",
      ], item, outcome.paypal_id);
      return;
    }
    if (outcome.state === "executed") {
      showOutcome("check", "Processing at PayPal", [
        "PayPal accepted the " + amount + " refund and is finishing it. Don't refund it again.",
      ], item, outcome.paypal_id);
      return;
    }
    if (outcome.state === "pending_approval") {
      showChanged(item);
      return;
    }
    if (outcome.state === "executing") {
      showOutcome("check", "Waiting for PayPal", ["It can't go out twice. You'll see the result in your refund requests as soon as PayPal answers."], item);
      return;
    }
    showOutcome("stopped", "Couldn't refund. No money moved.", ["Ask your assistant to try again."], item);
  }

  // The order moved under the merchant: show then and now, and ask again.
  function showChanged(item) {
    var title = "The order changed before the refund went out. No money moved.";
    readHome().then(function () {
      var latest = findItem(item.request_id);
      var lines = ["Review the new figures and approve again if it's still right."];
      if (latest && latest.refundable_now) {
        lines.unshift("Can still be refunded: was " + money(item.refundable_now) + ", now " + money(latest.refundable_now) + ".");
      }
      showOutcome("check", title, lines, item);
    }).catch(function () {
      showOutcome("check", title, ["Review it again in your refund requests."], item);
    });
  }

  // Once the signed decision has left the phone we cannot claim nothing was sent:
  // the server may have acted. Ask whether the request is still waiting instead.
  function recheckAfterLostAnswer(item, intent, view) {
    readHome()
      .then(function () {
        var outcome = lostAnswerOutcome(findItem(item.request_id), intent);
        if (outcome) {
          showOutcome(outcome[0], outcome[1], outcome[2], item);
          return;
        }
        go(intent === "approve" ? "final" : "review", item.request_id);
        setButtons(view, true);
        setProgress(view, "Your answer didn't reach us. No money moved. You can try again.", true);
      })
      .catch(function () {
        showOutcome("check", "We can't confirm the result yet", ["It can't go out twice. Check again when you're back online."], item);
      });
  }

  // What a lost answer means, from the server's current view of the request (or
  // null when it is still waiting and can be answered again). It says only what
  // the server shows and never assumes the answer arrived.
  function lostAnswerOutcome(latest, intent) {
    var section = latest ? latest.section : "";
    var reached = intent === "approve" ? "Your approval reached us" : "This refund was already approved";
    if (section === "needs_decision") {
      return null;
    }
    if (section === "sending") {
      return ["check", reached, ["It's processing at PayPal, so don't answer again."]];
    }
    if (section === "failed") {
      return ["check", reached, ["PayPal couldn't complete it, so no money moved. See it on your home screen."]];
    }
    if (latest) {
      return ["check", "Your answer didn't reach us", ["No money moved. This request can't be sent right now. See it on your home screen."]];
    }
    return ["check", "This request is closed", ["Don't answer it again. Its history shows what happened."]];
  }

  // The server says when it could not rebuild the on-hold message exactly as the
  // customer saw it (their name could not be read from PayPal just now).
  function customerLineNote(item) {
    if (item.customer_message_exact === "yes") {
      return "";
    }
    return "Couldn't confirm the customer's name in this message.";
  }

  // Why the server refused a decision, from a fresh read of home.
  function refusedOutcome(data, latest) {
    if (data.refunds_stopped === "yes") {
      return ["stopped", "Refunds are stopped", ["No money moved. Turn refunds back on to approve requests."]];
    }
    if (latest && latest.cant_send_reason === "time_up") {
      return ["stopped", "Time ran out. No money moved.", ["Your assistant can ask again if it's still needed."]];
    }
    return ["check", "This refund request is already closed.", ["No money will move from this screen."]];
  }

  // What to say when stopping or resuming refunds did not go through.
  function controlFailureText(status, failure) {
    if (status === 403) {
      return "Fingerprint not recognised. Nothing changed. Try again.";
    }
    if (failure && failure.name === "NotAllowedError") {
      return "Fingerprint cancelled. Nothing changed.";
    }
    return "That didn't go through. Nothing changed. Try again.";
  }

  // --- outcome: what happened, the PayPal reference, and what next ------------------
  var BADGES = { sent: ["Refunded", "ok-pill", "done"], stopped: ["Not refunded", "neutral-pill", "declined"], check: ["Check this", "caution-pill", "caution"] };

  function showOutcome(kind, title, lines, item, reference) {
    if (kind === "sent") {
      try {
        navigator.vibrate(40);
      } catch (ignored) {
        // no vibration on this device
      }
    }
    var view = document.getElementById("outcome");
    view.replaceChildren();
    var box = el("div", "", kind === "sent" ? "card ok-box center" : "card center");
    box.setAttribute("role", "status");
    box.appendChild(pill(BADGES[kind][0], BADGES[kind][1], BADGES[kind][2]));
    var heading = el("h1", title, "question");
    heading.tabIndex = -1;
    box.appendChild(heading);
    lines.forEach(function (line) {
      box.appendChild(el("p", line));
    });
    view.appendChild(box);
    if (reference) {
      var record = el("div", "", "card");
      record.appendChild(el("p", "PayPal reference", "eyebrow"));
      var refRow = el("div", "", "reference-row");
      refRow.appendChild(el("span", reference, "reference"));
      var copyButton = withIcon("copy", button("Copy", "link", function () {
        copyText(reference, copyButton);
      }));
      refRow.appendChild(copyButton);
      record.appendChild(refRow);
      record.appendChild(el("p", "Saved with your name and the time.", "muted small"));
      view.appendChild(record);
    }
    view.appendChild(button("Back to refund requests", "primary", openHome));
    view.appendChild(button("All refund activity", "secondary", openActivity));
    view.appendChild(withIcon("history", button("This refund's history", "link", function () {
      openHistory(item.request_id, function () { go("outcome"); });
    })));
    go("outcome", item.request_id);
    heading.focus({ preventScroll: true });
  }

  function copyText(text, target) {
    try {
      navigator.clipboard.writeText(text).then(function () {
        target.lastChild.textContent = "Copied";
      }, function () {
        target.lastChild.textContent = "Can't copy on this device";
      });
    } catch (ignored) {
      target.lastChild.textContent = "Can't copy on this device";
    }
  }

  // --- paused detail: never sendable, only declinable ---------------------------------
  function openPaused(item) {
    var view = document.getElementById("paused-view");
    view.replaceChildren();
    view.appendChild(backLink("Refund requests", openHome));
    view.appendChild(pill(item.rechecking === "yes" ? "Being reviewed again now" : "On hold until " + when(item.release_at), "neutral-pill", "hold"));
    view.appendChild(el("h1", item.customer + " · " + money(item.amount), "question"));
    view.appendChild(el("p", "Order ending " + item.order_ending + " · asked " + when(item.asked_at), "muted small"));
    view.appendChild(el("div", "", "change-banner-slot"));
    var facts = el("div", "", "card held");
    facts.appendChild(el("p", repeatLine(item), "strong"));
    facts.appendChild(el("p", "No one can refund it before then. After that, your usual limits apply to the latest PayPal figures.", "small"));
    view.appendChild(facts);
    if (item.many_requests === "yes") {
      view.appendChild(el("p", "This customer has asked for many refunds in the last " + home.settings.window_hours + " hours. You may want to contact them.", "caution-text strong"));
    }
    view.appendChild(el("p", "Message given to your assistant for the customer", "eyebrow"));
    view.appendChild(el("blockquote", item.customer_message, "quote"));
    var lineNote = customerLineNote(item);
    if (lineNote) {
      view.appendChild(el("p", lineNote, "muted small"));
    }
    view.appendChild(progressLine());
    var actions = el("div", "", "actions decision");
    actions.appendChild(button("Decline", "secondary", function () {
      decide(item, "reject", view);
    }, "reject"));
    actions.appendChild(el("p", "No money moves. Your assistant is told it can't be refunded.", "muted small center"));
    view.appendChild(actions);
    go("paused-view", item.request_id);
  }

  // --- activity and the record for one request ------------------------------------
  function openActivity() {
    var view = document.getElementById("activity");
    view.replaceChildren();
    view.appendChild(backLink("Refund requests", openHome));
    view.appendChild(el("h1", "Refund activity", "question"));
    view.appendChild(el("p", "Loading your recent refunds.", "muted"));
    go("activity");
    get("/v1/approvals/done").then(function (answer) {
      var items = answer.items;
      view.replaceChildren();
      view.appendChild(backLink("Refund requests", openHome));
      view.appendChild(el("h1", "Refund activity", "question"));
      var hours = home ? home.settings.window_hours : "";
      var shown = Number(answer.total) > items.length ? "Latest " + items.length + " of " + answer.total + ", last " : "Last ";
      view.appendChild(el("p", shown + hours + " hours", "muted small"));
      var list = el("div", "", "card list");
      items.forEach(function (item) { list.appendChild(doneRow(item, openActivity)); });
      view.appendChild(list);
    }).catch(function (failure) {
      if (failure.message !== "signed out") {
        view.appendChild(el("p", "Couldn't load your activity. Try again.", "muted"));
      }
    });
  }

  function openHistory(requestId, back) {
    var view = document.getElementById("history");
    view.replaceChildren();
    view.appendChild(backLink("Back", back));
    view.appendChild(el("p", "Loading the history.", "muted"));
    go("history");
    get("/v1/approvals/" + encodeURIComponent(requestId) + "/history")
      .then(function (record) {
        view.replaceChildren();
        view.appendChild(backLink("Back", back));
        view.appendChild(el("h1", money(record.amount) + " refund history", "question"));
        view.appendChild(el("p", "Order ending " + record.order_ending, "muted small"));
        var list = el("ol", "", "timeline");
        record.events.forEach(function (event) {
          var step = el("li", "", "timeline-item");
          step.appendChild(el("span", capitalised(when(event.at, true)), "muted small"));
          step.appendChild(el("span", event.title, "strong"));
          if (event.detail) {
            step.appendChild(el("span", event.detail, "muted small"));
          }
          list.appendChild(step);
        });
        view.appendChild(list);
        view.appendChild(withIcon(record.sealed ? "verified" : "tampered", el("p", record.sealed
          ? "This history hasn't been changed."
          : "This history was changed after it was saved. Don't rely on it until it has been checked.", record.sealed ? "card ok-box small" : "card danger-box small")));
      })
      .catch(function (failure) {
        if (failure.message !== "signed out") {
          view.appendChild(el("p", "Couldn't load this history. Try again.", "muted"));
        }
      });
  }

  // --- live updates: the server says something changed, the phone re-reads ---------
  function setLive(state) {
    var line = document.getElementById("live-line");
    line.textContent = state === "reconnecting" ? "Reconnecting. No money moves while we reconnect." : "";
    if (line.textContent) {
      withIcon("offline", line);
    }
  }

  function stopListening() {
    if (stream) {
      stream.abort();
      stream = null;
    }
    lastVersion = null;
    setLive("");
  }

  function listen() {
    if (!session || stream) {
      return;
    }
    var controller = new AbortController();
    stream = controller;
    fetch("/v1/approvals/stream", { headers: sessionHeaders(), signal: controller.signal })
      .then(function (response) {
        if (response.status === 401) {
          signedOut();
          return null;
        }
        if (!response.ok || !response.body) {
          throw new Error("unavailable");
        }
        setLive("live");
        retryDelay = RECONNECT_FIRST_MS;
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        function pump() {
          return reader.read().then(function (part) {
            if (part.done) {
              throw new Error("closed");
            }
            buffer += decoder.decode(part.value, { stream: true });
            var events = buffer.split("\n\n");
            buffer = events.pop();
            events.forEach(handleEvent);
            return pump();
          });
        }
        return pump();
      })
      .catch(function () {
        if (stream === controller) {
          stream = null;
        }
        if (!session || controller.signal.aborted) {
          return;
        }
        setLive("reconnecting");
        window.setTimeout(listen, retryDelay);
        retryDelay = Math.min(retryDelay * 2, RECONNECT_MAX_MS);
      });
  }

  function handleEvent(text) {
    if (text.indexOf("event: signed_out") === 0) {
      signedOut();
      return;
    }
    if (text.indexOf("event: changed") !== 0) {
      return;
    }
    var match = /data: (.*)/.exec(text);
    var version = match ? JSON.parse(match[1]).version : null;
    if (version === lastVersion) {
      return;
    }
    var firstSync = lastVersion === null;
    lastVersion = version;
    if (!firstSync) {
      somethingChanged();
    }
  }

  // The figures that matter for a decision; any difference means look again.
  function fingerprint(item) {
    return [item.section, item.can_send, item.amount, item.paid, item.already_refunded,
      item.refundable_now, item.waiting_on_order, item.release_at || ""].join("|");
  }

  // Re-read the server. Home redraws; an open request is never replaced under the
  // merchant's thumb: they are told it changed and sending waits until they look.
  function somethingChanged() {
    if (!session) {
      return;
    }
    if (current.view === "sending") {
      refreshTracker(current.requestId);
      return;
    }
    if (deciding) {
      return;
    }
    var view = current.view;
    var previous = home;
    var before = current.requestId ? findItem(current.requestId) : null;
    readHome().then(function (data) {
      if (current.view !== view) {
        return;
      }
      if (view === "home") {
        noticesFor(previous, data).forEach(toast);
        renderHome(data);
        return;
      }
      if (["review", "final", "paused-view"].indexOf(view) === -1 || !before) {
        return;
      }
      var after = findItem(current.requestId);
      if (!after) {
        changeBanner(view, "This refund request is already closed. No money will move from this screen.", null);
      } else if (fingerprint(after) !== fingerprint(before)) {
        changeBanner(view, "This refund request changed. Review the new figures before you approve.", after);
      }
    }).catch(function () {
      return null;
    });
  }

  function changeBanner(view, text, latest) {
    var screen = document.getElementById(view);
    screen.querySelectorAll(".decision .primary").forEach(function (b) { b.remove(); });
    var slot = screen.querySelector(".change-banner-slot");
    if (!slot) {
      return;
    }
    slot.replaceChildren();
    var banner = el("div", "", "card caution-box");
    banner.setAttribute("role", "alert");
    banner.appendChild(withIcon("caution", el("p", text, "box-title")));
    banner.appendChild(latest
      ? button("Review new figures", "secondary", function () {
        if (latest.section === "paused") {
          openPaused(latest);
        } else {
          openReview(latest);
        }
      })
      : button("Back to refund requests", "secondary", openHome));
    slot.appendChild(banner);
    window.scrollTo(0, 0);
  }


  // --- the send tracker: each step ticks only when the server has recorded it ------
  var TRACKER_STEPS = ["approved", "rechecked", "paypal_completed"];
  var REVEAL_GAP_MS = 550;
  var HOLD_AFTER_TRACKER_MS = 1300;
  var shown = [];

  function startTracker() {
    shown = [];
    markSteps(shown);
  }

  // Steps the server recorded together are revealed one after another, in order,
  // so the merchant can follow them. Only recorded steps are ever ticked.
  function revealSteps(recorded, then) {
    var next = TRACKER_STEPS.filter(function (step) {
      return recorded.indexOf(step) !== -1 && shown.indexOf(step) === -1;
    })[0];
    if (!next || current.view !== "sending") {
      if (then) {
        then();
      }
      return;
    }
    shown = shown.concat([next]);
    markSteps(shown);
    window.setTimeout(function () {
      revealSteps(recorded, then);
    }, REVEAL_GAP_MS);
  }

  function markSteps(done) {
    var activeSet = false;
    TRACKER_STEPS.forEach(function (step) {
      var line = document.querySelector('#tracker li[data-step="' + step + '"]');
      line.className = "";
      var old = line.querySelector("svg");
      if (old) {
        old.remove();
      }
      if (done.indexOf(step) !== -1) {
        line.className = "done";
        line.insertBefore(icon("done"), line.firstChild);
      } else if (!activeSet) {
        line.className = "active";
        line.insertBefore(icon("working", "spin"), line.firstChild);
        activeSet = true;
      }
    });
  }

  function refreshTracker(requestId) {
    if (!requestId) {
      return;
    }
    get("/v1/approvals/" + encodeURIComponent(requestId) + "/history").then(function (record) {
      if (current.view !== "sending") {
        return;
      }
      revealSteps(record.events.map(function (event) { return event.step; }), null);
    }).catch(function () {
      return null;
    });
  }

  // The answer has arrived: tick from the recorded steps, then show the result.
  function finishTracker(item, outcome) {
    function result() {
      window.setTimeout(function () {
        afterDecision(item, "approve", outcome);
      }, HOLD_AFTER_TRACKER_MS);
    }
    get("/v1/approvals/" + encodeURIComponent(item.request_id) + "/history").then(function (record) {
      revealSteps(record.events.map(function (event) { return event.step; }), result);
    }).catch(result);
  }

  // --- notices: short messages about changes the server reported -------------------
  function noticesFor(previous, next) {
    if (!previous) {
      return [];
    }
    var notes = [];
    var before = {};
    Object.keys(previous.sections).forEach(function (key) {
      previous.sections[key].forEach(function (item) { before[item.request_id] = key; });
    });
    next.sections.needs_decision.forEach(function (item) {
      if (!before[item.request_id]) {
        notes.push("New refund request: " + money(item.amount) + " for " + item.customer);
      } else if (before[item.request_id] === "paused") {
        notes.push(money(item.amount) + " for " + item.customer + " is off hold and needs your approval");
      }
    });
    next.sections.paused.forEach(function (item) {
      if (!before[item.request_id]) {
        notes.push("New refund request for " + item.customer + " is on hold");
      }
    });
    next.done.recent.forEach(function (item) {
      if (before[item.request_id] === "sending" && item.outcome === "executed" && item.paypal_status === "COMPLETED") {
        notes.push(money(item.amount) + " refunded to " + item.customer + ". PayPal confirmed it.");
      }
    });
    next.sections.failed.forEach(function (item) {
      if (before[item.request_id] !== "failed") {
        notes.push("The " + money(item.amount) + " refund to " + item.customer + " didn't go through. No money reached them.");
      }
    });
    return notes;
  }

  function toast(text) {
    var box = document.getElementById("toasts");
    var note = withIcon("info", el("p", text, "toast"));
    box.appendChild(note);
    window.setTimeout(function () {
      note.remove();
    }, TOAST_SHOW_MS);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && session) {
      listen();
      if (Date.now() - lastRead > QUIET_AFTER_READ_MS) {
        somethingChanged();
      }
    }
  });

  // --- start -------------------------------------------------------------------------
  if (!window.PublicKeyCredential) {
    say("This browser can't use your fingerprint. Open this page in Chrome.");
    return;
  }
  withIcon("fingerprint", document.getElementById("enrol-button"));
  withIcon("fingerprint", document.getElementById("signin-button"));
  withIcon("refresh", document.getElementById("refresh-button"));
  document.getElementById("enrol-button").addEventListener("click", enrol);
  document.getElementById("signin-button").addEventListener("click", signIn);
  document.getElementById("refresh-button").addEventListener("click", function () {
    var refresh = document.getElementById("refresh-button");
    refresh.disabled = true;
    refresh.lastChild.textContent = "Checking PayPal";
    openHome().then(function () {
      refresh.disabled = false;
      refresh.lastChild.textContent = "Check for new requests";
    });
  });
  if (enrolToken) {
    go("enrol");
  } else {
    showSignin();
  }
})();
