"""Allowlisted terminal summaries; exclude credentials and full responses."""

from pathlib import Path
import os
import re

from charmm_gui_cli.locales.i18n import t


def _text(value):
    return "".join(c for c in str(value) if c.isprintable())


def _error_text(error):
    message = t(error["message_key"], **error.get("message_params", {})) if error.get("message_key") else error.get("message", "")
    if error.get("server_message") and error["server_message"] not in message:
        message += " " + t("error.server", message=error["server_message"])
    for detail in error.get("details", []):
        from charmm_gui_cli.locales.i18n import MESSAGES
        if isinstance(detail, dict) and detail.get("key") in MESSAGES:
            message += " " + t(detail["key"], **detail.get("params", {}))
    return _text(message)


def _path(value):
    resolved = Path(str(value)).expanduser().resolve()
    try:
        return str(resolved.relative_to(Path.cwd())) or "."
    except ValueError:
        pass
    path = _text(resolved)
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def format_login(value, *, email=None):
    account = " (" + _text(email) + ")" if email else ""
    auth = lambda name: t("output.authenticated") if value.get(name) is True else t("output.unconfirmed")
    lines = [t("output.login", account=account), t("output.api_session", state=auth("api_authenticated")),
             t("output.web_session", state=auth("web_authenticated"))]
    if value.get("saved_to"):
        lines.append(t("output.saved", path=_path(Path(value["saved_to"]).parent),
                       permissions="0600" if value.get("permissions") == "0600" else t("output.unconfirmed")))
    if value.get("cookie_file"):
        lines.append(t("output.cookie_path", path=_path(value["cookie_file"])))
    if value.get("notice"):
        lines.append(t("output.env_override"))
    if value.get("remembered"):
        lines.append(t("session.remembered"))
    if value.get("password_command"):
        lines.append(t("session.command_enabled"))
    lines.append(t("cli.next", action="charmm-gui-cli build --protein protein.pdb --ligand bound-ligand.sdf --name receptor-a"))
    return "\n".join(lines)


def _repair_lines(entries):
    lines, alternates, states = [], [], []
    for entry in entries:
        if entry["key"] == "input.altloc_selected":
            alternates.append(entry)
        elif entry["key"] in ("state.mapped", "state.hydrogen_renamed", "state.disulfide_recorded"):
            states.append(entry)
        else:
            lines.append("• " + t(entry["key"], **entry["params"]))
    for key in ("state.mapped", "state.hydrogen_renamed", "state.disulfide_recorded"):
        group = [entry for entry in states if entry["key"] == key]
        if len(group) <= 5:
            lines.extend("• " + t(entry["key"], **entry["params"]) for entry in group)
        elif key == "state.mapped":
            from collections import Counter
            counts = Counter((entry["params"]["original"], entry["params"]["base"], entry["params"]["state"]) for entry in group)
            choices = ", ".join(f"{source}→{base}/{target}={count}" for (source, base, target), count in sorted(counts.items()))
            lines.append("• " + t("state.mapping_summary", count=len(group), choices=choices))
        else:
            lines.append("• " + t("state.hydrogen_summary" if key == "state.hydrogen_renamed" else "state.disulfide_summary", count=len(group)))
    if alternates:
        from collections import Counter
        counts = Counter(entry["params"]["altloc"] for entry in alternates)
        lines.append("• " + t("input.altloc_summary", count=len(alternates), choices=", ".join(f"{key}={count}" for key, count in sorted(counts.items()))))
    return lines


def _chain_lines(inventory):
    lines = []
    chains = inventory.get("chains", [])
    for chain in chains[:20]:
        residues = chain.get("residues", [])
        if not residues:
            continue
        counts = ", ".join(t("structure.kind." + kind, count=count) for kind, count in chain.get("kind_counts", {}).items())
        labels = [str(value["residue_number"]) + value.get("insertion_code", "") for value in (residues[0], residues[-1])]
        lines.append(t("structure.chain", model=chain["model"], chain=_text(chain["chain"] or "—"),
            segment=_text(chain["segment"] or "—"), block=chain["polymer_block"], count=chain["residue_count"],
            first=_text(labels[0]), last=_text(labels[1]), contents=counts))
        descriptions = []
        for entity in chain.get("declared_molecules", []):
            for value in entity.get("molecules", []) + entity.get("fragments", []):
                if value not in descriptions:
                    descriptions.append(value)
        if descriptions:
            lines.append(t("structure.declared", description=_text("; ".join(descriptions)[:300])))
    if len(chains) > 20:
        lines.append(t("structure.more", count=len(chains) - 20))
    if inventory.get("header_truncated"):
        lines.append(t("structure.header_truncated"))
    return lines


def format_build(value, **context):
    if value.get("background", {}).get("running"):
        background = value["background"]
        name = value.get("name") or _path(value.get("directory", ""))
        return "\n".join([t("output.background", name=_text(name), pid=background.get("pid", "—")),
                         *_repair_lines(value.get("automatic_repairs", [])),
                         t("output.log", path=_path(background["log"])),
                         t("cli.next", action="charmm-gui-cli jobs watch " + str(name))])
    from charmm_gui_cli.presentation.progress import describe_progress
    from charmm_gui_cli.store.auth import recovery_guidance
    state = value.get("state", value.get("status", "unknown"))
    label = describe_progress({"state": state}, localized=True)["label"]
    if state in ("done", "downloading", "downloaded_unverified", "ligands_present", "inputs_ready", "validated"):
        label = t("stage." + state)
    lines = [t("output.task", name=_text(value.get("name") or value.get("job_id") or "—")),
             t("output.state", state=label)]
    lines.extend(_repair_lines(value.get("automatic_repairs", [])))
    lines.extend(_chain_lines(value.get("structure_inventory", {})))
    validation = value.get("validation")
    state_proof = value.get("protein_state_validation") or (validation.get("protein_state_validation", {}) if isinstance(validation, dict) else {})
    if state_proof.get("passed") and state_proof.get("assembly", {}).get("passed"):
        lines.append(t("state.verified", count=len(state_proof["assembly"].get("residues", []))))
    elif value.get("protein_state_intent", {}).get("residues"):
        lines.append(t("state.local_review", count=len(value["protein_state_intent"]["residues"])))
    for warning in value.get("chemistry_warnings", []):
        lines.append("• " + t(warning["key"], **warning["params"]))
    execution = value.get("execution_state")
    if execution in ("failed_remote", "failed_local", "interrupted"):
        lines[1] = t("output.state", state=t("execution." + execution))
    if value.get("remote_status") is not None and not value.get("validation_status") and state not in ("validated", "validation_failed", "downloaded_unverified", "ligands_present"):
        remote = value["remote_status"]
        remote_label = t("stage.done") if remote == "done" else describe_progress({"state": remote}, localized=True)["label"]
        lines.append(t("output.remote_state", state=remote_label))
    for field, key in (("source_job_id", "output.source"), ("build_job_id", "output.build_id"), ("job_id", "output.build_id")):
        identifier = value.get(field)
        if field == "job_id" and value.get("build_job_id"):
            continue
        if isinstance(identifier, str) and re.fullmatch(r"[0-9]{1,32}", identifier):
            lines.append(t(key, jobid=identifier))
    delivery = value.get("delivery", {})
    delivery = delivery if isinstance(delivery, dict) else {}
    if delivery.get("directory"):
        lines.append(t("output.delivery", path=_text(os.path.relpath(Path(delivery["directory"]).expanduser().resolve(), Path.cwd()))))
    for field, key in (() if delivery.get("directory") else (("directory", "output.directory"), ("archive", "output.archive"), ("report", "output.report"))):
        if isinstance(value.get(field), str):
            lines.append(t(key, path=_path(value[field])))
    if not delivery.get("directory") and not value.get("report") and isinstance(value.get("report_file"), str):
        lines.append(t("output.report", path=_path(value["report_file"])))
    passed = value.get("passed")
    validation = value.get("validation")
    if isinstance(validation, dict):
        passed = validation.get("passed", passed)
    if state == "validated" and passed is True:
        lines[1] = t("output.state", state=t("output.passed"))
    elif passed is False or state == "validation_failed":
        lines[1] = t("output.state", state=t("output.failed"))
    if value.get("validation_status") == "stale_or_unreadable":
        lines[1] = t("output.state", state=t("validation.stale_label"))
        lines.append(t("output.stale"))
        for detail in value.get("validation_issues", []):
            from charmm_gui_cli.locales.i18n import MESSAGES
            if detail.get("key") in MESSAGES:
                params = dict(detail.get("params", {}))
                if "path" in params:
                    params["path"] = _path(params["path"])
                lines.append(t(detail["key"], **params))
    compilation = value.get("compilation_status", "unknown")
    if value.get("gromacs_compiled") is True:
        compilation = "passed"
    if compilation in ("passed", "failed"):
        lines.append(t("output.compilation", state=t("output." + compilation)))
    if value.get("review_required") or isinstance(validation, dict) and validation.get("review_required"):
        lines.append(t("output.review"))
    recovery = value.get("recovery")
    recovery = recovery if isinstance(recovery, dict) else {}
    error = recovery.get("error", value.get("last_error"))
    error = error if isinstance(error, dict) else {}
    if error.get("message"):
        lines.append(t("cli.error", message=_error_text(error)))
        if execution is None and error.get("retryable") is False and state != "submission_unknown":
            lines[1] = t("output.state", state=t("execution.failed_local"))
    category = "submission_unknown" if state == "submission_unknown" else error.get("category", state)
    guidance = recovery_guidance(category, cause_category=error.get("cause_category"))
    name = value.get("name")
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        action = "charmm-gui-cli jobs report " + name if state == "validated" and passed is True and not value.get("validation_status") else "charmm-gui-cli jobs resume " + name
    else:
        action = "charmm-gui-cli jobs / charmm-gui-cli build-resume"
    if category in ("authentication", "auth_required", "paused_auth"):
        action = "charmm-gui-cli login; " + action
    elif category == "submission_unknown":
        action = "charmm-gui-cli jobs reconcile " + name if isinstance(name, str) else "charmm-gui-cli jobs list"
        if error.get("cause_category") in ("authentication", "auth_required", "access_denied"):
            action = "charmm-gui-cli login"
    elif error and error.get("retryable") is False:
        action = value.get("next_step") or recovery.get("next_step") or error.get("next_step") or "charmm-gui-cli jobs status " + str(name or "NAME")
    if value.get("protein_state_intent") and value.get("next_step"):
        action = value["next_step"]
    lines.append(t("cli.next", action=action))
    return "\n".join(lines)


def format_check(value, **context):
    lines = [t("check.result", state=t("output.passed") if value.get("passed") else t("output.failed")),
             t("check.protein", residues=value.get("protein", {}).get("residues") or value.get("protein_inventory", {}).get("protein_residues_all_models") or "—")]
    error = value.get("error", {})
    if isinstance(error, dict) and error.get("message"):
        lines.append(t("cli.error", message=_error_text(error)))
    protein = value.get("protein", {})
    inventory = value.get("protein_inventory", {})
    lines.extend(_chain_lines(value.get("structure_inventory", inventory.get("structure_inventory", {}))))
    missing = protein.get("residues_missing_heavy_atoms", len(inventory.get("missing_heavy_atoms", [])))
    gaps = protein.get("numbering_gaps", len(inventory.get("numbering_gaps", [])))
    if missing or gaps:
        lines.append(t("check.structure_review", missing=missing, gaps=gaps))
    if value.get("review_required"):
        lines.append(t("output.review"))
    for component in value.get("chemistry", []):
        lines.append(t("check.ligand", name=component.get("resname", "—"), formula=component.get("molecular_formula", "—"), charge=component.get("net_formal_charge", "—")))
        for warning in component.get("chemistry_warnings", []):
            lines.append("• " + t(warning["key"], **warning["params"]))
    if value.get("chemistry"):
        lines.append(t("structure.charge_review"))
    lines.extend(_repair_lines(value.get("automatic_repairs", [])))
    for prediction in value.get("terminal_predictions", []):
        lines.append(t("check.terminal", chain=prediction["chain"], residue=prediction["first_resname"], patch=prediction["suggested_n_terminal"]))
    states = value.get("input_normalization", {}).get("protein_state_intent", {}).get("residues", [])
    if states:
        lines.append(t("state.local_review", count=len(states)))
    if value.get("input_normalization", {}).get("hetatm_present") is False:
        lines.append(t("input.no_hetero"))
    for image in value.get("preview", {}).get("images", []):
        if image.get("file"):
            lines.append(t("check.preview", path=_path(image["file"])))
    if value.get("check_file"):
        lines.append(t("output.report", path=_path(value["check_file"])))
    lines.append(t("cli.next", action=error.get("next_step") or ("charmm-gui-cli build -h" if value.get("passed") else "charmm-gui-cli check -h")))
    if context.get("full"):
        import json
        lines.append(t("check.full_details"))
        lines.append(json.dumps(value, indent=2, ensure_ascii=False))
    return "\n".join(lines)


def format_jobs(value, **context):
    lines = [t("jobs.columns")]
    for job in value.get("jobs", []):
        background = job.get("background", {})
        outcome = _job_outcome(job)
        label = t("jobs.outcome." + outcome)
        lines.append(f"{_text(job.get('name') or _path(job['directory']))} | {label} | "
                     + t("jobs.running" if background.get("running") else "jobs.stopped") + " | " + _elapsed(background.get('elapsed_seconds', 0)))
        recovery = job.get("recovery") or {}
        error = recovery.get("error") or job.get("last_error") or {}
        if outcome.startswith("failed") or outcome == "interrupted":
            if error.get("message"):
                lines.append("  " + t("cli.error", message=_error_text(error)))
    lines.append(t("cli.next", action="charmm-gui-cli jobs status NAME"))
    return "\n".join(lines)


def _elapsed(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _job_outcome(job):
    state = job.get("state")
    if state == "validated" and job.get("passed") is True and not job.get("validation_status"):
        return "passed"
    if state in ("remote_error", "preparation_failed") or job.get("execution_state") == "failed_remote":
        return "failed_remote"
    if state in ("validation_failed", "local_failed") or job.get("execution_state") == "failed_local":
        return "failed_local"
    background = job.get("background") or {}
    if background.get("running"):
        return "running"
    if job.get("validation_status") == "stale_or_unreadable":
        return "failed_local"
    if (job.get("execution_state") == "interrupted" or state == "interrupted"
            or background.get("pid") and not background.get("finished_at")):
        return "interrupted"
    return "ready" if state in ("inputs_ready", "prepared_reused", "pending") else "running"


def format_job_status(value, **context):
    display = dict(value)
    display.pop("background", None)
    if value.get("background", {}).get("running") and value.get("execution_state") not in ("failed_local", "failed_remote", "submission_unknown") and value.get("state") != "submission_unknown":
        # The previous execution's recovery history is not this worker's state.
        for key in ("recovery", "last_error", "next_step", "execution_state"):
            display.pop(key, None)
    result = format_build(display)
    background = value.get("background", {})
    result += "\n" + t("jobs.running" if background.get("running") else "jobs.stopped")
    result += " | " + t("progress.elapsed", elapsed=_elapsed(background.get('elapsed_seconds', 0)))
    if value.get("download_progress"):
        from ..locales.transfers import transfer_text
        if (transfer := transfer_text(value["download_progress"])):
            result += "\n" + transfer
    if value.get("network_wait", {}).get("active"):
        wait = value["network_wait"]
        result += "\n" + t("network.waiting", attempt=wait.get("retry_attempt", "—"),
                           elapsed=_elapsed(wait.get("waiting_elapsed_seconds", 0)))
    if background.get("running"):
        lines = result.splitlines()
        if lines:
            lines = [line for line in lines if not line.startswith(t("cli.next", action=""))]
        result = "\n".join(lines) + "\n" + t("cli.next", action="charmm-gui-cli jobs watch " + str(value.get("name") or _path(value["directory"])))
    elif background.get("exit_code", 0) and not value.get("recovery", {}).get("error") and not value.get("last_error"):
        result += "\n" + t("background.worker_failed")
    for record in value.get("recent_stages", []):
        result += "\n" + _text(record)
    return result


def format_update(value, **context):
    lines = [t("update.versions", current=value["current_version"], latest=value["latest_version"])]
    key = {"update_available": "available", "up_to_date": "up_to_date", "local_newer": "local_newer", "updated": "updated"}[value["state"]]
    lines.append(t("update." + key))
    if value.get("command_path"):
        lines.append(t("update.command", path=_path(value["command_path"])))
    return "\n".join(lines)


def format_relink(value, **context):
    return t("jobs.relinked", name=_text(value["name"]), path=_path(value["directory"])) + "\n" + t("cli.next", action=value["next_step"])


def format_logout(value, **context):
    return "\n".join([t("session.logged_out", count=len(value.get("removed_files", []))),
                      t("session.logout_notice"), t("cli.next", action=value.get("next_step", "charmm-gui-cli login"))])


def format_doctor(value, **context):
    lines = [t("session.doctor_header", state=t("output.passed" if value.get("passed") else "output.failed"))]
    for name, check in value.get("checks", {}).items():
        lines.append(t("session.doctor_check", name=name, state=t("output.passed" if check.get("passed") else "output.failed")))
    remaining = value.get("session", {}).get("remaining_seconds")
    lines.append(t("session.doctor_expiry", seconds=remaining) if remaining is not None else t("session.doctor_unknown" if value.get("session", {}).get("format") == "JWT" else "session.doctor_missing"))
    lines.append(t("cli.next", action=value.get("next_step", "charmm-gui-cli login")))
    return "\n".join(lines)


def format_batch(value, **context):
    from charmm_gui_cli.presentation.progress import describe_progress
    from charmm_gui_cli.locales.i18n import MESSAGES
    queue = value.get("batch", value)
    def label(state):
        key = "batch.state." + state
        return t(key) if key in MESSAGES else t("stage." + state) if "stage." + state in MESSAGES else describe_progress({"state": state}, localized=True)["label"]
    directory = value.get("sweep_directory", value.get("directory", queue.get("directory")))
    name = Path(directory).name if "sweep_directory" in value or "batch" in value else queue.get("name", Path(directory).name)
    lines = [t("batch.header", name=_text(name)),
             t("output.state", state=label(queue["state"])),
             t("batch.progress", completed=queue.get("completed", 0), total=queue["total"], active=queue.get("active", 0), failed=queue["failed"]),
             t("progress.elapsed", elapsed=_elapsed(queue.get("elapsed_seconds", 0))),
             t("batch.path", path=_path(directory))]
    if queue.get("preparation_reused"):
        lines.append(t("batch.reused", count=queue["preparation_reused"]))
    for row in queue["jobs"]:
        valid = lambda value: value if isinstance(value, str) and re.fullmatch(r"[0-9]{1,32}", value) else "—"
        lines.append(t("batch.row", name=_text(row["name"]), state=label(row["state"]),
                       source=valid(row.get("source_job_id")), build=valid(row.get("build_job_id"))))
        if row.get("error"):
            error = row.get("error_detail") or {"message": row["error"]}
            lines.append("  " + _error_text(error))
    reports = queue.get("reports", {})
    if reports.get("json") and reports.get("csv"):
        lines.append(t("batch.reports", json=_path(reports["json"]), csv=_path(reports["csv"])))
    if context.get("dry_run"):
        lines.append(t("batch.dry_run"))
    lines.append(t("cli.next", action=queue["next_step"]))
    return "\n".join(lines)


def format_preview(value, **context):
    lines = [t("preview.header", path=_path(value["model"]))]
    for item in value.get("images", []):
        lines.append(t("preview.image", view=_text(item["view"]), path=_path(item["file"])))
    return "\n".join(lines)


FORMATTERS = {"doctor": format_doctor, "logout": format_logout, "update": format_update, "login": format_login, "build": format_build, "build-resume": format_build,
              "status": format_build, "jobs.status": format_job_status, "jobs.resume": format_build,
              "check": format_check, "jobs.list": format_jobs, "jobs.relink": format_relink}
FORMATTERS.update({name: format_batch for name in ("batch", "batch-status", "batch-resume", "sweep", "sweep-status", "sweep-resume")})
FORMATTERS["preview"] = format_preview
