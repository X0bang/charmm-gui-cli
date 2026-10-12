"""GET-only reconciliation; an absent result never authorizes a replay."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uuid

from ..api import job_id
from ..auth import ToolError, atomic_json, atomic_write
from ..i18n import t
from ..progress import official_links
from ..web import soup_for
from ..web_state import parse_page_state
from ..workflow import run_lock


def reconcile(directory, web, *, confirm_not_submitted=False, source_job_id=None):
    directory = Path(directory).resolve()
    preparation = directory / "preparation"
    with run_lock(directory):
        manifest = json.loads((directory / "full-run.json").read_text())
        candidates = [(preparation / "web-run.json", preparation / "upload-response.html", preparation / "upload-form.html"),
                      (preparation / "selection.intent.json", preparation / "selection.html", preparation / "upload-response.html"),
                      (preparation / "parameterization.intent.json", preparation / "parameterization.html", preparation / "selection.html")]
        selected = None
        for intent_path, output, previous in candidates:
            if intent_path.is_file() and not output.is_file():
                intent = json.loads(intent_path.read_text())
                if intent.get("state") in ("submission_unknown", "submitting", "upload_submitting"):
                    selected = intent_path, output, previous, intent
                    break
        if selected is None:
            return {"state": "no_unresolved_intent", "directory": str(directory), "post_sent": False,
                    "next_step": "charmm-gui-cli jobs status NAME"}
        intent_path, output, previous, intent = selected
        identifier = source_job_id or manifest.get("source_job_id") or intent.get("job_id")
        if not identifier:
            fields = dict(intent.get("fields", []))
            identifier = fields.get("jobid")
        if not identifier and previous.is_file():
            field = soup_for(previous.read_text()).select_one('input[name="jobid"]')
            identifier = field.get("value") if field else None
        if not identifier:
            return {"state": "unknown", "directory": str(directory), "post_sent": False,
                    "url": "https://www.charmm-gui.org/", "next_step": t("reconcile.no_id")}
        identifier = job_id(identifier)
        url = official_links(source_job_id=identifier)["source_preparation"]
        response = web.request("GET", url)
        page = parse_page_state(response.text, url)
        snapshot = preparation / ("reconcile-" + uuid.uuid4().hex + ".html")
        atomic_write(snapshot, response.text)
        old = parse_page_state(previous.read_text(), url) if previous.is_file() else {}
        soup = soup_for(response.text)
        accepted = False
        if page["job_id"] == identifier and page["state"] != "auth_required":
            if output.name == "selection.html":
                accepted = soup.select_one('input[type="file"][name^="sdf_cgenff["]') is not None
            elif output.name == "parameterization.html":
                next_doc = parse_qs(urlparse(page.get("next_url") or "").query).get("doc", [""])[0]
                accepted = next_doc == "input/membrane.bilayer" and bool(page["artifacts"])
            elif source_job_id:
                # Explicit user association is required when the upload response lost its ID.
                accepted = soup.select_one('input[name^="chains["]') is not None
            if page.get("step_number") and old.get("step_number"):
                accepted = accepted or int(page["step_number"]) > int(old["step_number"])
        result = {"directory": str(directory), "snapshot": str(snapshot), "url": url,
                  "source_job_id": identifier, "post_sent": False}
        if accepted:
            atomic_write(output, response.text)
            intent.update(state="response_received", snapshot=str(output), reconciled=True)
            atomic_json(intent_path, intent)
            manifest.update(source_job_id=identifier)
            atomic_json(directory / "full-run.json", manifest)
            result.update(state="response_received", next_step="charmm-gui-cli jobs resume NAME")
        else:
            # A matching old form is only a candidate: explicit human confirmation
            # is still needed, because server work may not yet have rendered.
            same_step = (page["job_id"] == identifier and page["state"] == "ready"
                         and old.get("step_number") is not None and page.get("step_number") == old["step_number"]
                         and page.get("next_url") == old.get("next_url"))
            if same_step and output.name != "upload-response.html":
                result.update(state="confirmation_required", next_step=t("reconcile.confirm"))
                if confirm_not_submitted:
                    archive = intent_path.with_name(intent_path.name + ".retired-" + uuid.uuid4().hex)
                    intent_path.rename(archive)
                    result.update(state="not_submitted_confirmed", retired_intent=str(archive), next_step="charmm-gui-cli jobs resume NAME")
            else:
                result.update(state="unknown", next_step=t("reconcile.inspect"))
        return result
