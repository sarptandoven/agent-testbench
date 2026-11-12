# Threat model

agent-testbench lets a coding agent run code on real machines and spend real money doing it. This page says who can
do what, what testbench stops, how, and which test proves it. A claim with no test is not on this page as a
guarantee: it is under "What is not covered" in its section, with what to do instead.

Tests are named `file::test`; they run in the default suite (`python -m pytest -q`) unless marked otherwise (the
Docker and kind tests need those on the machine). Fuzz tests run a few hundred examples each by default and many
more with `TESTBENCH_FUZZ_EXAMPLES=20000`. `python tools/mutate.py` checks that the tests of the money paths fail
when that code is wrong (mutation testing; [Checking the checks](#checking-the-checks)).

## What is protected

- **Money**: nothing launches whose worst case is above the limits, and nothing above the approval threshold runs
  without a person's yes.
- **The limits themselves**: the agent cannot raise them, lower a price, or move testbench to another limits folder
  without the person being asked.
- **Secrets**: they go by name; their values are never written to testbench.yaml, state, plans, reports, evidence,
  notifications or exports.
- **The proof records**: evidence that changed after a run no longer verifies; a CI run can be signed.
- **This machine**: the dashboard and the MCP HTTP server answer this machine only and launch nothing on a web
  page's say-so; a Docker sandbox stays a sandbox unless the person says otherwise.

## Actors

| Actor | Can | Cannot (each is a section below) |
|---|---|---|
| **The person** | Everything: set the limits in `~/.testbench/limits.yaml` and testbench.yaml, approve spending, install plug-ins. Trusted. | - |
| **The coding agent**, through the CLI, the MCP server, the Claude Code plugin or the Python API | Write and run code, edit testbench.yaml (plans, checks), launch plans and sessions inside the limits, ask for approval. | Spend above max_run_usd or a day limit; approve its own spending above ask_above_usd; raise a limit, lower a price, change the guard or loosen a sandbox without the person being asked; read a secret's value through testbench; make a value an option. |
| **Code under test** (the project's tests, notebooks, scripts, an app a web step serves) | Whatever its machine lets it: on this machine (`local`) all the person can; in a Docker container with `network: none` nothing outside it. Print anything, write anything into its step folder. | Run past its step's max_minutes on a remote machine; leave a secret's value in what the run keeps; make its run's evidence change without `testbench verify` saying so. |
| **A plug-in package** (an entry point: a backend, check, sink, notifier...) | Anything: it is code testbench imports and runs, like any Python package you install. | Nothing is guaranteed against a malicious plug-in. Install only plug-ins you trust; `testbench doctor` lists them (and one that fails to load is reported, not fatal: `test_backends.py::test_a_broken_plugin_is_reported_not_fatal`). |
| **A web page a web step opens** (or any page the person visits) | Run JavaScript in a browser on this machine, send requests to 127.0.0.1 through DNS rebinding. | Read or change anything through the dashboard or the MCP HTTP server (Host and Origin are checked; the dashboard is read-only). |
| **A webhook receiver, a tracker** (notify:, export:) | Read what is sent: a run's verdict, numbers, failures, cost. Answer slowly or not at all. | Get a secret's value; hold up or fail a run; see the webhook URL's secret path in any log. |

### The agent: mistaken, hurried or misled, not a determined attacker with a shell

The guard hook and the MCP server's checks are written for an agent that makes mistakes, is in a hurry, or was told
by a web page or a README to approve its own spending. They are not a sandbox. An agent with an unrestricted shell
on the person's account can write any file the person can - `~/.testbench/limits.yaml` included - with a script the
hook never reads. What holds then:

- the hard limits are checked by testbench itself, at every launch, from files the agent did not have to touch;
- every remote run and session has a hard stop on the remote side, so even a run launched past every check is
  bounded in time (and so in cost);
- `testbench spend` and the machine's ledger show what was spent.

To make the machine's limits out of the agent's reach, make the file the person's and not the agent's: run the
agent as another user, or `sudo chown root ~/.testbench/limits.yaml && sudo chmod 644 ~/.testbench/limits.yaml`
(testbench only reads it). Keep the cloud provider's own spending limit set too: estimates use published prices.

## Spend above the budget or this machine's limits

**Cannot**: launch a run, watch, loop try, session or check whose worst case is above max_run_usd, or that would take
today's total above max_day_usd (the project's, or this machine's for every project together).

**Mechanism**: every launch goes through `budget.check` (`runs.launch`, `sessions.start`, `loop`, `watch`, `check`,
`api`, `core`). The worst case is the hardware's rate times the sum of the steps' max_minutes plus start-up; each limit
is the project's, capped by `~/.testbench/limits.yaml`, which a project can lower but never raise. Running work counts
at its worst case until it finishes. A machine-wide lock is held from the check to the ledger entry, so two launches
at once cannot both fit into room for one. Every number goes through `config.number`: NaN, infinity, true/false and
integers too big for a float are not limits, prices or minutes. A limits file or a ledger line testbench cannot read
refuses every launch.

**Proof**:
- `test_runpod.py::test_the_budget_refuses_before_any_call_to_runpod` (a plan above max_run_usd: no request reaches RunPod)
- `test_limits.py::test_machine_limits_cap_a_generous_project`, `test_budget_rules.py::test_the_machine_caps_a_limit_and_never_raises_it`
- `test_limits.py::test_the_machine_day_limit_counts_every_project`, `test_budget_rules.py::test_the_machine_day_limit_counts_every_project_to_the_cent`, `test_budget_rules.py::test_the_project_day_limit_holds_to_the_cent`, `test_budget_rules.py::test_at_max_run_usd_is_allowed_and_a_cent_over_is_refused`
- `test_limits.py::test_launches_at_once_cannot_overspend_a_day` (four launches at once, room for one)
- `test_units.py::test_prices_and_worst_case`, `test_budget_rules.py::test_a_worst_case_is_rounded_to_a_hundredth_of_a_cent`
- `test_guardrails.py::test_a_budget_that_is_not_a_number_is_refused`, `test_guardrails.py::test_minutes_and_prices_that_are_not_numbers_are_refused`, `test_guardrails.py::test_the_check_fails_closed_on_numbers_that_are_not_numbers`, `test_budget_rules.py::test_anything_that_is_not_a_number_is_refused_unpriced`, `test_trust.py::test_a_number_too_big_for_a_float_is_no_limit`
- `test_limits.py::test_a_broken_limits_file_refuses_everything`, `test_guardrails.py::test_machine_limits_and_ledgers_that_are_not_numbers_refuse_everything`, `test_budget_rules.py::test_a_ledger_line_testbench_did_not_write_refuses_everything`
- `test_sessions.py::test_budget_applies_to_sessions`, `test_diff_watch.py::test_watch_needs_a_free_plan_or_a_persons_yes`, `test_loop.py::test_refusals_say_what_to_do` (a loop's --budget-usd), `test_check.py::test_with_a_config_free_plans_run_and_paid_ones_are_only_priced`
- Fuzzed: `test_fuzz_budget.py::test_an_allowed_launch_is_inside_every_limit_and_one_inside_them_is_allowed` (any budgets, machine limits, ledgers and estimates: allowed exactly when inside every limit), `test_fuzz_budget.py::test_any_plan_that_loads_is_priced_and_held_to_its_limits`, `test_fuzz_budget.py::test_any_ledger_is_counted_or_refuses_everything`, `test_fuzz_budget.py::test_any_limits_file_is_limits_or_a_refusal`, `test_fuzz_config.py` (any testbench.yaml that loads has finite limits).

**What is not covered**: the price list can be behind the provider's; `testbench prices --update` fetches the
catalog, and the provider's own spending limit is the last word. Time on this machine (`local`) is free and only
bounded by each step's max_minutes.

## Approve its own spending

**Cannot**: launch anything above ask_above_usd without a person's yes.

**Mechanism**: above ask_above_usd a launch needs `--approve` (CLI), `approved_by_person=true` (MCP) or `approve=True`
(Python), which an agent may pass only after the person said yes. That rule is enforced where testbench can see who
says yes:
- *MCP with a client that can ask (elicitation)*: the server asks the person directly and launches only on their yes.
  `approved_by_person=true` does not skip that question: the agent's --approve is dropped and the person is asked.
- *Claude Code (the plug-in's guard hook)*: any command that could pass --approve to a launch - in quotes, with
  backslashes, in a variable, from `$(...)`, through xargs, `bash -c`, `env`, a subshell, or Python's
  `approve=True` - becomes a question to the person, with the plan's worst case; so does an MCP call with
  `approved_by_person=true`.
- *No abbreviations*: `--appr` is not `--approve` (argparse's prefix matching is off for every command).
- *An MCP argument is never an option*: a plan named `--approve` is refused; values go after `--` or as `--opt=value`.
- *The GitHub Action*: `approve: true` is a standing yes from whoever commits the workflow.

**Proof**:
- `test_mcp.py::test_a_cost_above_the_threshold_is_asked_of_the_person`, `test_fuzz_mcp.py::test_approved_by_person_never_skips_asking_a_person_who_can_be_asked`, `test_mcp.py::test_without_elicitation_it_says_what_needs_approval`
- `test_guardrails.py::test_the_hook_asks_about_approvals_anywhere`, `test_integration.py::test_guard_decisions`, `test_guard_hook.py::test_approvals_ask_with_the_plan_s_estimate`, `test_guard_hook.py::test_mcp_launches_ask_only_with_approved_by_person`, `test_sessions.py::test_guard_asks_before_an_approved_session`, `test_loop.py::test_loop_over_mcp_and_the_guard`
- Fuzzed: `test_fuzz_guard.py::test_approve_in_disguise_always_asks`, `test_fuzz_guard.py::test_approve_through_a_variable_or_subshell_asks`, `test_fuzz_guard.py::test_arguments_from_xargs_or_find_ask`, `test_fuzz_guard.py::test_approve_from_python_asks`, and the other way: `test_fuzz_guard.py::test_the_same_launches_without_approve_pass`
- `test_guardrails.py::test_options_are_spelled_out`, `test_trust.py::test_no_abbreviation_of_approve_approves`
- `test_guardrails.py::test_mcp_names_never_become_options`, `test_fuzz_mcp.py::test_no_tool_argument_becomes_an_option`
- `test_action.py::test_a_refused_plan_fails_with_the_reason_and_approve_is_a_standing_yes`, `test_api.py::test_a_person_approves_through_ask`

**What is not covered**: outside Claude Code there is no hook, and an agent can type `--approve` into a shell; in an
MCP client without elicitation it can pass `approved_by_person=true`. Approval is the person's speed bump; the hard
limits (max_run_usd, the day limits) are the wall, and they hold whatever was approved. A script the agent writes
and runs is not read by the hook. To make approval a person's only, give the agent an MCP client that asks the
person (elicitation) and set ask_above_usd and max_run_usd in `~/.testbench/limits.yaml` to what you would approve
without being there.

## Raise the limits

**Cannot**, without the person being asked: raise a budget in testbench.yaml, change `pricing:` or `guard:`, lower a
backend's own price (`ssh.usd_per_hour`, ...), edit `~/.testbench/limits.yaml` or `~/.testbench/prices.json`, point
testbench at another limits folder, or make the price catalog lower a price. Changing or deleting a ledger is refused.

**Mechanism**: the guard hook reads every edit of testbench.yaml as the file would be after it and compares the
limits (a budget that is not a number asks too; lowering one does not); it asks about edits of the machine's limits
and prices, and about shell commands that write them; it refuses commands that set TESTBENCH_USER_DIR, HOME or
USERPROFILE for testbench, and asks about an edit that sets TESTBENCH_USER_DIR in a settings file (an MCP config's
`env`, an rc file), or that turns hooks off or takes the plug-in out of Claude Code's settings. If the hook itself
fails on a call that touches testbench, it asks instead of staying silent.
The price catalog can only raise prices or add hardware at the dearest known price. `.testbench/check.json` (what
`testbench check` ran) is read for its local plans only: it cannot set a budget, a price or a paid machine. Recipes
never set `budget:`, `pricing:` or `guard:`.

**Proof**:
- `test_limits.py::test_guard_asks_only_when_an_edit_raises_the_limits`, `test_guard_hook.py::test_what_an_edit_does_to_the_limits`, `test_guard_hook.py::test_prices_pricing_and_the_guard_in_an_edit`, `test_guardrails.py::test_the_hook_sees_numbers_prices_and_sandboxes`
- `test_limits.py::test_guard_protects_machine_limits_and_ledgers`, `test_guard_hook.py::test_edits_of_limits_ledgers_prices_and_settings`, `test_guard_hook.py::test_an_edit_that_moves_the_limits_folder_asks`, `test_guard_hook.py::test_an_edit_that_turns_the_guard_off_asks`
- `test_guard_hook.py::test_a_call_the_hook_cannot_read_asks_only_when_it_touches_testbench`, `test_fuzz_guard.py::test_no_edit_makes_the_hook_fail`, `test_fuzz_guard.py::test_no_command_makes_the_hook_fail`
- Fuzzed: `test_fuzz_guard.py::test_an_edit_that_raises_or_breaks_a_budget_asks` (any old and new budgets, NaN and huge integers included)
- `test_prices.py::test_a_catalog_raises_and_adds_but_never_lowers`, `test_guardrails.py::test_a_catalog_cannot_add_cheap_hardware`, `test_bad_inputs.py::test_a_hostile_price_catalog_never_lowers_a_price_or_adds_a_free_gpu`, `test_guard_hook.py::test_prices_from_another_catalog_ask`
- `test_check.py::test_the_record_check_keeps_cannot_set_a_budget_or_a_paid_machine`, `test_recipes.py::test_the_recipes_that_ship_are_well_formed`

**What is not covered**: code the agent runs can write these files (see [the agent](#the-agent-mistaken-hurried-or-misled-not-a-determined-attacker-with-a-shell)).

## Leave a sandbox

**Cannot**, without the person being asked: turn the Docker sandbox's network on, add `docker create` arguments
(`run_args`, a mount of `/`), change the container command, or move a plan out of the container (to `local`, to
Modal, to a short form that runs here).

**Mechanism**: the guard hook compares the `docker:` section and each sandboxed plan's backend before and after an
edit. Containers get secrets by name (`-e NAME`, the value from this machine's environment).

**Proof**: `test_guardrails.py::test_the_hook_sees_numbers_prices_and_sandboxes`, `test_guard_hook.py::test_ways_out_of_the_docker_sandbox`,
fuzzed in `test_fuzz_guard.py::test_a_way_out_of_the_docker_sandbox_asks`; that `network: none` is offline:
`test_docker.py::test_network_none_is_a_sandbox_without_a_network` (needs Docker).

**What is not covered**: a container shares this machine's kernel ([Docker](docker.md) says what it isolates);
`local` is no sandbox at all.

## Run on after a hard stop

**Cannot**: run on a remote machine past its max_minutes, even if this machine goes offline.

**Mechanism**: each backend stops the work on the remote side: a Modal function timeout, a watchdog on the Colab VM,
the ssh box and in the container, Kubernetes' own `activeDeadlineSeconds`, and a RunPod pod's first process, which
has RunPod terminate the pod at its priced worst case (with the key RunPod gives each pod), when nothing was started
in it, or once its work is over; a pod that has no way to do that is refused unless the person allows it
(`runpod.allow_without_remote_stop`, which the guard hook asks about). A watchdog given a limit that is not a
number stops at once; it wakes at max_minutes whatever check interval the environment asked for
(TESTBENCH_WATCHDOG_SECONDS, which a run's environment carries into a container or pod, can make it check more
often, never less, and never stop it).

**Proof**: `test_units.py::test_generated_modal_app`, `test_watchdog.py::test_stops_the_kernel_at_max_minutes`,
`test_watchdog.py::test_colab_starts_the_watchdog_before_anything_else`, `test_ssh.py::test_a_plan_past_its_limit_is_stopped_from_here`,
`test_docker.py::test_the_hard_stop_fires_inside_the_container` (Docker), `test_kubernetes.py::test_a_plan_is_a_job_with_kubernetes_own_hard_stop`,
`test_kubernetes.py::test_kind_kubernetes_ends_the_job_at_its_deadline` (kind), `test_sessions.py::test_sessions_stop_themselves`,
`test_guardrails.py::test_a_watchdog_with_no_real_limit_stops_at_once`, `test_trust.py::test_the_hard_stop_holds_whatever_the_watchdog_tick_says`,
`test_runpod.py::test_the_pod_terminates_itself_at_its_deadline_with_its_own_key` (this machine silent after the
work started), `test_runpod.py::test_a_pod_nothing_is_started_in_ends_itself`,
`test_runpod.py::test_the_watchdog_in_the_pod_stops_the_work_before_the_deadline`, `test_runpod.py::test_cancel_terminates_the_pod`,
`test_runpod.py::test_failures_say_what_to_do_and_a_failed_create_costs_nothing` (a pod that cannot end itself is
refused), `test_guard_hook.py::test_a_pod_without_a_hard_stop_of_its_own_asks`.

**What is not covered**: if RunPod refuses a pod's own key, the pod ends every process testbench started and its
first process exits, so the container stops with nothing running (`test_runpod.py::test_a_pod_runpod_will_not_terminate_still_stops_everything_and_exits`),
but only testbench here (or the person, at runpod.io) can then terminate it; RunPod's own spend limit is the last
word. testbench was written against RunPod's published API and a fake of it, not a live account
([what is known](runpod.md#what-is-known-about-runpods-api-and-what-is-not)). A plug-in backend's hard stop is the
plug-in's own: the Backend interface asks every backend for one ([Backend plugins](plugins.md)), and testbench
cannot check that a plug-in keeps it.

## Read or leak a secret

**Cannot**: put a secret's value into testbench.yaml, state.json, plan.json, the ledgers, a report, the evidence, a
notification, an export or a PR comment; read one through the dashboard.

**Mechanism**: plans and sessions name secrets (`secrets: [HF_TOKEN]`), and the value comes from the environment,
a Modal or Kubernetes secret, or Colab. A backend's own key (`RUNPOD_API_KEY`) is read by name, goes only to the
provider's own hosts over https (any other `runpod.api_url` or `proxy_url` is refused, so an edit cannot send it
elsewhere), never to the pod, and is in no log, state or report; the pod's agent answers only a token made from it.
A run's values are never written: when it finishes, every value of a secret its plan names (6 characters or more,
as this machine's environment holds it) is replaced by `[secret]` in the report
and in every evidence file, before the evidence is hashed - so the dashboard, notifications, exports and the
Action's comment never see it. Export settings refuse anything that looks like a key or token; a webhook URL is shown
by scheme and host only. The dashboard serves its pages, reports and step evidence and nothing else (never state.json,
plan.json, ledgers, session folders, or a link out of them). In a project, the guard refuses commands that print
`.env` files or secret variables. Copies of the project never include `.env` files.

**Proof**: `test_trust.py::test_a_secret_the_code_prints_is_kept_nowhere` (every file under .testbench, the webhook's
payloads, the jsonl export, the report, and `testbench verify` still passing), `test_trust.py::test_a_short_or_unknown_secret_is_left_alone`,
`test_ui_serve.py::test_names_are_escaped_and_secrets_never_shown`, `test_ui_serve.py::test_nothing_outside_the_pages_reports_and_evidence_is_served`,
`test_fuzz_dashboard.py::test_the_dashboard_serves_only_its_pages_reports_and_evidence` (any path: dot-dot, encoded
slashes, links to state.json and out of the project), `test_notify.py::test_webhook_env_keeps_the_url_out_of_the_file`,
`test_notify.py::test_the_guard_lets_notify_edits_through_and_keeps_the_url_secret`, `test_export.py::test_the_export_section_says_what_to_write`,
`test_integration.py::test_guard_decisions`, `test_secrets_paths.py::test_connect_dry_run_never_prints_the_other_servers_secrets`,
`test_secrets_paths.py::test_a_loop_try_never_commits_an_untracked_env_file`,
`test_runpod.py::test_a_plan_runs_in_a_pod_of_its_own_and_the_pod_is_terminated` (the key and a secret in no file under
.testbench, the key not sent to the pod), `test_runpod.py::test_the_runpod_section_is_checked` (the key goes to no
other host), `test_runpod.py::test_the_agent_answers_only_with_its_token`.

**What is not covered**: a value this machine never holds (a Modal or Kubernetes secret) cannot be recognized, so if
the code prints it, it is kept as printed; values under 6 characters are left (they would blank out ordinary words);
the live log while a run goes, the agent's own terminal, and what a live session's code prints (it comes back to
the agent as it is) show what the code prints. A `local` step sees this machine's whole environment, not only the
secrets it names.

## Forge proof

**Cannot**: change a run's evidence, or the report's list of it, without `testbench verify` saying so; pass off a
locally made report as one a CI workflow signed.

**Mechanism**: the report records each evidence file's sha256 and one sha256 over all of them, printed when the run
ends; `testbench verify` recomputes both. The report names the commit and a hash of uncommitted changes. With the
Action's `attest: true`, report.json and every evidence file get a GitHub artifact attestation, and
`testbench verify --attestation` checks it was made by the repository's workflow, at the report's commit.

**Proof**: `test_proof.py::test_verify_finds_evidence_changed_after_the_run`, `test_proof.py::test_a_report_names_its_code_machine_and_evidence`,
`test_bad_inputs.py::test_verify_catches_an_edited_or_broken_evidence_list`, `test_fuzz.py::test_verify_finds_any_changed_added_or_removed_file`,
`test_action.py::test_attest_signs_the_report_and_every_evidence_file_and_keeps_the_bundle`,
`test_action.py::test_attest_refuses_evidence_that_no_longer_matches_its_report`, `test_action.py::test_verify_attestation_runs_gh_and_checks_the_commit`.

**What is not covered**: whoever can write `.testbench/` can rewrite a report and its evidence together so they agree;
`verify` then passes. A local record is a lab notebook, not a signature: what ties it to the run is the
evidence_sha256 the person saw printed, or an attestation made in CI.

## The dashboard and the MCP HTTP server

**Cannot**: be reached from another machine, or by a web page through DNS rebinding; launch, approve, cancel or spend
anything through the dashboard.

**Mechanism**: both listen on 127.0.0.1 (the MCP server warns when told to listen elsewhere). The dashboard answers
only a Host header of 127.0.0.1 or localhost and only GET and HEAD; the MCP HTTP server checks Host and Origin
(mcp's DNS-rebinding protection, set for 127.0.0.1, localhost and ::1 on its port).

**Proof**: `test_ui_serve.py::test_only_requests_for_127_0_0_1_or_localhost_are_answered` (and POST, PUT, DELETE,
PATCH refused), `test_fuzz_dashboard.py::test_only_this_machine_s_names_are_answered`,
`test_trust.py::test_the_mcp_http_server_answers_only_this_machine_s_names`.

**What is not covered**: neither server authenticates: another user on this machine can reach them. On a shared
machine, use the stdio MCP server and do not leave the dashboard running.

## Checking the checks

`tools/mutate.py` changes budget.py and the guard hook's decision functions one small step at a time (a comparison
turned, a number moved by one, True and False swapped, a `not` removed, `and` and `or` swapped) and runs their fast
tests against each change, in a copy of the repository. A change no test notices is either a missing test or a change
that does not alter what the code does; the run lists them. Run it by hand (`python tools/mutate.py --jobs 2`, many
minutes) or with the manual `mutation` workflow; `test_mutate_tool.py` checks it still runs.

## Reporting a problem

Privately, as [SECURITY.md](https://github.com/sarptandoven/agent-testbench/blob/main/SECURITY.md) says.
