"""Allowlisted terminal summaries; never render credentials or server bodies."""

from pathlib import Path
import re

from .i18n import t


def _text(value):
    return "".join(c for c in str(value) if c.isprintable())


def _path(value):
    path = _text(value)
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
    lines.append(t("cli.next", action="charmm-gui-cli build --protein protein.pdb --ligand bound-ligand.sdf --name receptor-a"))
    return "\n".join(lines)


def format_build(value, **context):
    from .progress import describe_progress
    state = value.get("state", value.get("status", "unknown"))
    label = describe_progress({"state": state})["label"]
    lines = [t("output.task", name=_text(value.get("name") or value.get("job_id") or "—")),
             t("output.state", state=label)]
    if value.get("remote_status") is not None:
        lines.append(t("output.remote_state", state=describe_progress({"state": value["remote_status"]})["label"]))
    for field, key in (("source_job_id", "output.source"), ("build_job_id", "output.build_id"), ("job_id", "output.build_id")):
        identifier = value.get(field)
        if field == "job_id" and value.get("build_job_id"):
            continue
        if isinstance(identifier, str) and re.fullmatch(r"[0-9]{1,32}", identifier):
            lines.append(t(key, jobid=identifier))
    for field, key in (("directory", "output.directory"), ("archive", "output.archive"), ("report", "output.report")):
        if isinstance(value.get(field), str):
            lines.append(t(key, path=_path(value[field])))
    passed = value.get("passed")
    validation = value.get("validation")
    if isinstance(validation, dict):
        passed = validation.get("passed", passed)
    if state == "validated" and passed is True:
        lines[1] = t("output.state", state=t("output.passed"))
    elif passed is False or state == "validation_failed":
        lines[1] = t("output.state", state=t("output.failed"))
    else:
        lines.append(t("output.pending"))
    compilation = value.get("compilation_status", "unknown")
    if value.get("gromacs_compiled") is True:
        compilation = "passed"
    lines.append(t("output.compilation", state=t("output." + compilation) if compilation in ("passed", "failed", "not_run") else t("output.unconfirmed")))
    if value.get("review_required") or isinstance(validation, dict) and validation.get("review_required"):
        lines.append(t("output.review"))
    lines.append(t("output.simulation"))
    name = value.get("name")
    if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        action = "charmm-gui-cli jobs report " + name if state == "validated" and passed is True else "charmm-gui-cli jobs resume " + name
    else:
        action = "charmm-gui-cli jobs / charmm-gui-cli build-resume"
    lines.append(t("cli.next", action=action))
    return "\n".join(lines)


FORMATTERS = {"login": format_login, "build": format_build, "build-resume": format_build,
              "status": format_build, "jobs.status": format_build, "jobs.resume": format_build}
