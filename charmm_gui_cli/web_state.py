"""Conservative, read-only interpretation of CHARMM-GUI HTML job pages.

``ready`` means the rendered step permits continuation, not that an entire
system has been built or its chemistry validated. The legacy monitor only
streams log text and never establishes success by itself.
"""

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from .web import WEB_BASE, soup_for


def _script_value(scripts, name, number=False):
    value = r"(\d+)" if number else r"['\"]([^'\"]*)['\"]"
    match = re.search(r"\b(?:var|let|const)\s+" + re.escape(name) + r"\s*=\s*" + value, scripts)
    return match.group(1) if match else None


def _same_origin_url(target, base_url):
    if not target:
        return None
    url = urljoin(base_url, target)
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme == "https" and parsed.hostname in ("charmm-gui.org", "www.charmm-gui.org")
                 and not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        return None
    if not valid:
        return None
    return url


def parse_page_state(html, page_url=WEB_BASE):
    """Return state, diagnostics, and candidate GET/POST URLs without I/O.

    Only explicit rendered error blocks and actual formstop declarations count
    as failures; the generic error strings present in every page's JavaScript
    do not. Polling/recovery links are informational and are never followed here.
    """
    soup = soup_for(html)
    scripts = "\n".join(script.get_text() for script in soup.find_all("script"))
    job_field = soup.select_one('input[name="jobid"]')
    job_id = (job_field.get("value") if job_field else None) or _script_value(scripts, "jobid")
    if not job_id or not re.fullmatch(r"\d+", job_id):
        job_id = None
    project_field = soup.select_one('input[name="project"]')
    project = (project_field.get("value") if project_field else None) or _script_value(scripts, "project")
    raw_stop = _script_value(scripts, "formstop", number=True)
    formstop = int(raw_stop) if raw_stop is not None else None
    errors = []
    for element in soup.select('[id="error_msg"]'):
        message = element.get_text(" ", strip=True)
        if message and message not in errors:
            errors.append(message)
    login = soup.select_one('form input[type="password"]') is not None
    forms = [form for form in soup.find_all("form")
             if form.select_one('input[name="jobid"]') and form.get("action")]
    next_url = _same_origin_url(forms[0].get("action"), page_url) if len(forms) == 1 else None
    if next_url and not parse_qs(urlparse(next_url).query).get("doc", [""])[0].startswith("input/"):
        next_url = None
    if login:
        state = "auth_required"
    elif formstop == 1:
        state = "error"
    elif formstop == 2:
        # Some running pages put their informational message in error_msg.
        state = "running"
    elif errors:
        state = "error"
    elif next_url and (formstop == 0 or (formstop is None and job_id)):
        state = "ready"
    else:
        state = "unknown"
    recovery_url = None
    if job_id:
        for link in soup.select('.strip a[href]'):
            candidate = _same_origin_url(link.get("href"), page_url)
            if not candidate:
                continue
            query = parse_qs(urlparse(candidate).query)
            if query.get("jobid") == [job_id] and query.get("doc", [""])[0].startswith("input/"):
                recovery_url = candidate
                break
    artifacts = []
    if job_id:
        for link in soup.select('a[href]'):
            candidate = _same_origin_url(link.get("href"), page_url)
            if candidate and urlparse(candidate).path.startswith(f"/uploaded_pdb/{job_id}/"):
                if candidate not in artifacts:
                    artifacts.append(candidate)
    monitor_url = None
    if job_id:
        monitor_url = WEB_BASE.rstrip("/") + "/docs/input/monitor.php?" + urlencode({"jobid": job_id, "update": 1})
    next_label = soup.select_one("#nextBtn .nav_title")
    return {
        "state": state, "job_id": job_id, "project": project, "formstop": formstop,
        "errors": errors, "next_url": next_url if state == "ready" else None,
        "next_method": forms[0].get("method", "get").upper() if state == "ready" else None,
        "next_label": next_label.get_text(" ", strip=True) if next_label else None,
        "recovery_url": recovery_url, "monitor_url": monitor_url, "artifacts": artifacts,
        "step_number": _script_value(scripts, "nstep", number=True),
        "step_name": _script_value(scripts, "step"),
        "workflow_complete": False,
    }


def interpret_monitor(text):
    """Legacy monitor content is progress evidence, never a success signal."""
    return {"has_output": bool(text.strip()) and not text.lstrip().startswith("Waiting output..."),
            "completion_confirmed": False, "text": text}


def interpret_api_job_status(data):
    """Legacy form jobs can have null API status; queue statistics are global."""
    status = data.get("status")
    if status not in ("pending", "running", "done", "error"):
        status = "unknown"
    return {"status": status, "requires_page_check": status in ("unknown", "done"),
            "workflow_complete": False}
