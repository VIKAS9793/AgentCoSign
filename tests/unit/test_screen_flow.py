"""The approval app's screen flow and layout rules, checked in the shipped files.

Edge cases (written before the change):

| Case | Expected |
|---|---|
| Phone | The queue comes before the summary tiles; tiles show figure only |
| Laptop | Tiles above the queue; the stop button in the top bar |
| Stop control | One control drawn in both places, never two behaviours |
| Flow | At most four screens: dashboard, review, confirm, done |
| Buttons | Hug their label; only cards and rows that open run full width |
| Dashboard | Settings fold away; recent refunds is a link, not a list |
| Behind the scenes | Hidden unless the hosted demo; off until switched on |
| Behind the scenes, laptop | The app and the backend side by side |
| Behind the scenes, steps | Shown as text only, never as markup |
| Behind the scenes, sign-out or reset | Polling stops; a new setup starts over |
| Behind the scenes, failed step | Where the money stands, before the step's title |
| Behind the scenes, end of a run | One outcome block with the server's headline |
| Behind the scenes, connections | From the server's health report, never assumed |
| Behind the scenes, first visit | A cue points at the switch until it is opened once |
| Done screen | Next steps spaced in one row; history sits with the record |
| Scenario needs approval | Opens straight into review for that request |
| Scenario decided alone | Ends on the outcome screen, with no review steps |
| Demo restarted | Its notice stays until the visitor sets up again |

Phone and laptop rendering is checked in Chrome at both widths; these tests
keep the rules from being undone silently.
"""

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "agentcosign" / "web"
HTML = (WEB / "approve.html").read_text(encoding="utf-8")
CSS = (WEB / "approve.css").read_text(encoding="utf-8")
JS = (WEB / "approve.js").read_text(encoding="utf-8")


def _block(css: str, media: str) -> str:
    """The body of the first media block that starts with ``media``."""
    start = css.index(media)
    depth, index = 0, css.index("{", start)
    for end in range(index, len(css)):
        depth += {"{": 1, "}": -1}.get(css[end], 0)
        if depth == 0:
            return css[index:end]
    raise AssertionError(media)


@pytest.mark.edge
def test_on_a_phone_the_queue_comes_before_the_summary_tiles() -> None:
    phone = _block(CSS, "@media (max-width: 1023px)")
    queue = re.search(r"#requests,[^{]*\{\s*order:\s*(\d+)", phone)
    tiles = re.search(r"\.summary \{\s*order:\s*(\d+)", phone)
    assert queue and tiles
    assert int(queue.group(1)) < int(tiles.group(1))
    assert ".tile .small" in phone


@pytest.mark.edge
def test_the_stop_control_is_one_control_shown_in_two_places() -> None:
    assert 'id="header-stop"' in HTML
    assert 'id="stop-control"' in HTML
    assert '["stop-control", "header-stop"].forEach' in JS
    laptop = _block(CSS, "@media (min-width: 1024px)")
    assert re.search(r"#stop-control \{\s*display:\s*none", laptop)
    assert re.search(r"\.header-stop \{[^}]*display:\s*block", laptop)


@pytest.mark.edge
def test_the_flow_is_never_more_than_four_screens() -> None:
    steps = re.search(r"var STEPS = \[([^\]]*)\]", JS)
    assert steps is not None
    assert steps.group(1).count('"') == 6  # Review, Confirm, Done after the dashboard


@pytest.mark.edge
def test_buttons_hug_their_label_and_cards_stay_full_width() -> None:
    base = re.search(r"\nbutton \{([^}]*)\}", CSS)
    assert base is not None
    assert "width: auto" in base.group(1)
    assert "width: 100%" in re.search(r"\n\.card \{([^}]*)\}", CSS).group(1)  # type: ignore[union-attr] -- the card rule exists in the stylesheet
    assert "min-width: 14rem" not in CSS


def test_the_dashboard_folds_settings_and_links_recent_refunds() -> None:
    assert '<details class="fold">' in HTML
    assert 'id="done-list"' not in HTML
    assert '"See recent refunds ("' in JS


@pytest.mark.edge
def test_demo_progress_shows_above_the_scenarios() -> None:
    """Progress appears where the eyes are, before the list that was tapped."""
    assert HTML.index('id="demo-progress"') < HTML.index('id="scenario-list"')


@pytest.mark.edge
def test_a_closed_workspace_sends_the_visitor_to_set_up_again() -> None:
    assert JS.count("response.status === 410") >= 2  # both reads and writes
    assert "The demo restarted and cleared its test data." in JS
    assert '"X-Workspace": workspace' in JS


def _trace_script() -> str:
    """The panel's part of the app script."""
    start = JS.index("// --- behind the scenes")
    return JS[start : JS.index("// --- start ---", start)]


@pytest.mark.edge
def test_behind_the_scenes_is_hidden_unless_the_hosted_demo() -> None:
    assert re.search(r'<aside id="trace"[^>]*\shidden', HTML)
    assert re.search(r'id="trace-toggle"[^>]*\shidden', HTML)
    assert 'show("trace-toggle", demoMode);' in JS
    assert 'demoMode && recall("trace") === "on"' in JS


@pytest.mark.edge
def test_behind_the_scenes_sits_beside_the_app_on_a_laptop() -> None:
    laptop = CSS[CSS.index("/* laptop: the merchant app on the left") :]
    assert re.search(r"body\.trace-on \{[^}]*display:\s*grid", laptop)
    assert re.search(r"body\.trace-on \.trace \{[^}]*position:\s*sticky", laptop)


@pytest.mark.edge
def test_behind_the_scenes_shows_steps_as_text_only() -> None:
    script = _trace_script()
    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
    assert "el(" in script


@pytest.mark.edge
def test_behind_the_scenes_stops_on_sign_out_and_starts_over_on_reset() -> None:
    signed_out = JS[JS.index("function signedOut()") :][:200]
    gone = JS[JS.index("function workspaceGone()") :][:250]
    assert "stopTrace();" in signed_out
    assert "stopTrace();" in gone
    assert "resetTrace();" in gone
    script = _trace_script()
    assert "setInterval" not in script
    assert "if (!session || traceBusy)" in script  # one read in flight at a time


@pytest.mark.edge
def test_a_failed_step_says_where_the_money_stands_first() -> None:
    script = _trace_script()
    item = script[script.index("function stepItem(step)") :]
    assert item.index('"trace-safe"') < item.index('"trace-title"')
    assert 'k !== "safe"' in item  # not repeated in the folded details


@pytest.mark.edge
def test_a_run_ends_with_the_servers_outcome_block() -> None:
    script = _trace_script()
    assert 'step.layer === "outcome" ? outcomeCard(step) : stepItem(step)' in script
    card = script[script.index("function outcomeCard(step)") :]
    assert 'el("p", step.title, "trace-headline")' in card
    assert '["Money moved", d.money_moved]' in card


@pytest.mark.edge
def test_connections_come_from_the_servers_health_report() -> None:
    script = _trace_script()
    assert 'serverModes.paypal_execution === "sandbox"' in script
    assert 'serverModes.ai_models === "live"' in script
    assert "serverModes = health;" in JS
    assert 'id="trace-links"' in HTML


@pytest.mark.edge
def test_a_cue_points_at_the_switch_until_it_is_opened() -> None:
    assert 'classList.toggle("cue", demoMode && recall("trace-seen") !== "yes")' in JS
    assert 'remember("trace-seen", "yes");' in _trace_script()
    assert re.search(r"\.trace-toggle\.cue::before \{[^}]*content: \"\"", CSS)
    reduced = CSS[CSS.index("@keyframes trace-cue-hand") :]
    assert re.search(r"prefers-reduced-motion[^{]*\{[^@]*animation: none", reduced)


@pytest.mark.edge
def test_done_screen_next_steps_are_spaced_in_one_row() -> None:
    assert re.search(r"\.done-actions \{[^}]*display: flex[^}]*gap: 12px", CSS)
    done = JS[JS.index('var next = el("div", "", "done-actions")') :][:600]
    assert done.index('"primary", openHome') < done.index('"secondary", openActivity')
    assert "record.appendChild(history);" in done


FUNCTION_END = "\n  }\n"
ON_ITS_OWN_ENDING = (
    "showOutcome(ending[0], ending[1], ending[2], item, ending[3], true);"
)
NO_STEPPER_ON_ITS_OWN = "if (!onItsOwn) {\n      view.appendChild(stepper(2));"
HOLD_RESTART_NOTICE = "if (restartNotice) {\n      return;"


def _function(name: str) -> str:
    """One top-level function of the app script, by name."""
    start = JS.index(f"  function {name}(")
    return JS[start : JS.index(FUNCTION_END, start)]


@pytest.mark.edge
def test_a_scenario_that_needs_approval_opens_its_review() -> None:
    result = _function("showDemoResult")
    assert 'outcome.decision === "escalate"' in result
    assert "openReview(waiting);" in result
    assert ON_ITS_OWN_ENDING in result
    run = _function("runDemo")
    assert "showDemoResult(result)" in run
    assert "toast(demoResult(result))" not in run


@pytest.mark.edge
def test_a_scenario_decided_alone_has_no_review_steps() -> None:
    outcome = _function("showOutcome")
    assert NO_STEPPER_ON_ITS_OWN in outcome
    assert 'onItsOwn ? "Saved with the time."' in outcome
    ending = _function("demoEnding")
    sent = ending.index("refunded to")
    assert 'outcome.paypal_status === "COMPLETED"' in ending[:sent]


@pytest.mark.edge
def test_the_restart_notice_is_not_replaced_until_setup_starts_again() -> None:
    assert HOLD_RESTART_NOTICE in _function("say")
    assert HOLD_RESTART_NOTICE in _function("toast")
    gone = _function("workspaceGone")
    assert gone.index("say(") < gone.index("restartNotice = true;")
    assert "restartNotice = false;" in _function("enrol")


@pytest.mark.edge
def test_the_panel_keeps_close_in_reach_and_the_cue_inside_the_switch() -> None:
    assert re.search(r"\.trace-head \{[^}]*position: sticky", CSS)
    cue = CSS[CSS.index(".trace-toggle.cue::before {") :][:400]
    assert "position: absolute" not in cue
    assert "display: inline-block" in cue
    assert re.search(r"\.trace-toggle \{[^}]*white-space: nowrap", CSS)


@pytest.mark.edge
def test_long_paypal_paths_break_inside_the_panel() -> None:
    breaking = r"\.trace-title,\s*\.trace-headline,\s*\.trace-safe \{([^}]*)\}"
    rule = re.search(breaking, CSS)
    assert rule and "overflow-wrap: anywhere" in rule.group(1)
