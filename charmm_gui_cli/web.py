"""HTTP-only adapter for the authenticated CHARMM-GUI form workflow.

No browser engine, browser cookies, or JavaScript execution is used. Scientific
step adapters must explicitly translate each form's fields and validate results.
"""

import json
import os
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
import requests

from .auth import ToolError, atomic_json, atomic_write
from .api import AuthError, SubmissionUnknown
from .http_retry import request as request_with_retry, retry_delay, transient_exception, RETRY_STATUSES

WEB_BASE = "https://www.charmm-gui.org/"


def modeling_target(action):
    """Accept only an explicit modeling action on the official entry point."""
    if not action:
        raise ToolError("Modeling form is missing its action.")
    target = urljoin(WEB_BASE, action)
    parsed = urlparse(target)
    docs = parse_qs(parsed.query, keep_blank_values=True).get("doc", [])
    if (parsed.scheme != "https" or parsed.netloc.lower() not in
            ("www.charmm-gui.org", "charmm-gui.org", "www.charmm-gui.org:443", "charmm-gui.org:443")
            or parsed.path not in ("/", "/index.php")
            or len(docs) != 1 or not docs[0].startswith("input/")
            or ".." in docs[0].split("/")):
        raise ToolError("Refusing to submit a non-modeling form.")
    return target


def soup_for(html):
    return BeautifulSoup(html, "html.parser")


def form_data(form):
    """Browser-style successful default controls, preserving repeated names."""
    pairs = []
    for field in form.select("input[name], select[name], textarea[name]"):
        if field.has_attr("disabled"):
            continue
        name = field.get("name")
        if field.name == "input":
            kind = field.get("type", "text").lower()
            if kind in ("button", "submit", "reset", "file", "image"):
                continue
            if kind in ("checkbox", "radio") and not field.has_attr("checked"):
                continue
            value = field.get("value", "on" if kind in ("checkbox", "radio") else "")
            pairs.append((name, value))
        elif field.name == "textarea":
            pairs.append((name, field.get_text()))
        else:
            options = field.find_all("option")
            selected = [option for option in options if option.has_attr("selected")]
            if not selected and not field.has_attr("multiple"):
                selected = options[:1]
            pairs.extend((name, option.get("value", option.get_text())) for option in selected)
    return pairs


def describe_page(html):
    soup = soup_for(html)
    forms = []
    sensitive = ("password", "token", "cookie", "session", "csrf", "email")
    for form in soup.find_all("form"):
        fields = []
        for field in form.select("input, select, textarea, button"):
            name = field.get("name", "")
            value = field.get("value", "")
            secret = field.get("type", "").lower() == "password" or any(word in name.lower() for word in sensitive)
            if secret:
                value = "[redacted]"
            item = {"name": name, "id": field.get("id"), "type": field.get("type", field.name),
                    "value": value, "checked": field.has_attr("checked"), "disabled": field.has_attr("disabled")}
            if field.name == "select":
                item["options"] = [{"value": "[redacted]" if secret else option.get("value"),
                                     "text": "[redacted]" if secret else option.get_text(" ", strip=True),
                                     "selected": option.has_attr("selected")} for option in field.find_all("option")]
            fields.append(item)
        forms.append({"name": form.get("name"), "id": form.get("id"), "action": form.get("action"),
                      "method": form.get("method", "get"), "fields": fields})
    return {"title": soup.title.get_text(strip=True) if soup.title else None,
            "login_form_present": soup.select_one('form input[type="password"]') is not None,
            "forms": forms,
            "scripts": [script["src"] for script in soup.select("script[src]")]}


class WebClient:
    def __init__(self, cookie_path=None, session=None):
        self.session = session or requests.Session()
        self.cookie_path = Path(cookie_path) if cookie_path else None
        self.session.headers.update({"User-Agent": "charmm-gui-cli/0.2 (Python requests; research workflow)"})
        if self.cookie_path and self.cookie_path.is_file():
            try:
                saved = json.loads(self.cookie_path.read_text(encoding="utf-8"))
                for item in saved["cookies"]:
                    if item["domain"].lstrip(".") not in ("charmm-gui.org", "www.charmm-gui.org"):
                        raise ValueError
                    self.session.cookies.set_cookie(requests.cookies.create_cookie(**item))
            except (ValueError, KeyError, TypeError):
                raise AuthError("Invalid saved CHARMM-GUI web session. Run charmm-gui-cli login.") from None

    def save_cookies(self):
        if self.cookie_path:
            cookies = [{"name": cookie.name, "value": cookie.value, "domain": cookie.domain,
                        "path": cookie.path, "secure": cookie.secure, "expires": cookie.expires,
                        "rest": cookie._rest} for cookie in self.session.cookies]
            atomic_json(self.cookie_path, {"version": 1, "cookies": cookies})

    def validate_auth(self):
        # A session cookie cannot prove authentication, but missing/expired
        # saved cookies can be rejected before creating any POST intent.
        if self.cookie_path is not None and not any(not cookie.is_expired() for cookie in self.session.cookies):
            raise AuthError("Website session is missing or expired. Run charmm-gui-cli login.")

    def request(self, method, target, **kwargs):
        url = urljoin(WEB_BASE, target)
        for _ in range(6):
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.netloc.lower() not in
                    ("www.charmm-gui.org", "charmm-gui.org", "www.charmm-gui.org:443", "charmm-gui.org:443")):
                raise ToolError("Refusing to send website session credentials outside CHARMM-GUI HTTPS origins.")
            try:
                result = request_with_retry(self.session, method, url, allow_redirects=False, timeout=(15, 90), **kwargs)
            except requests.RequestException as exc:
                if method.upper() not in ("GET", "HEAD"):
                    raise SubmissionUnknown("Website request failed after a POST attempt; its outcome is unknown. No automatic resubmission.",
                                            cause_category="transient_network" if transient_exception(exc) else "transport_error") from None
                raise ToolError("CHARMM-GUI web HTTP request failed; resume the existing run.",
                                category="transient_network" if transient_exception(exc) else "transport_error",
                                retryable=transient_exception(exc),
                                next_step="Resume the existing run after checking connectivity.") from None
            self.save_cookies()
            if result.is_redirect:
                if result.status_code in (307, 308) and method.upper() not in ("GET", "HEAD"):
                    result.close()
                    raise SubmissionUnknown("Website redirected a submitted request with HTTP 307/308; submission outcome is unknown. No automatic resubmission.")
                url = urljoin(url, result.headers["Location"])
                if result.status_code in (301, 302, 303):
                    method, kwargs = "GET", {}
                result.close()
                continue
            if result.status_code >= 400:
                result.close()
                code = result.status_code
                if code in (401, 403):
                    raise AuthError(f"CHARMM-GUI web request returned HTTP {code}.",
                                    category="authentication" if code == 401 else "access_denied",
                                    next_step=("Run charmm-gui-cli login, then build-resume." if code == 401 else
                                               "Verify this account owns the job; login with the correct account, then build-resume."))
                raise ToolError(f"CHARMM-GUI web request returned HTTP {code}.",
                                category="rate_limited" if code == 429 else ("transient_http" if code in RETRY_STATUSES else "http_error"),
                                retryable=method.upper() == "GET" and code in RETRY_STATUSES,
                                retry_after_seconds=(retry_delay(result.headers["Retry-After"], 0)
                                                     if "Retry-After" in result.headers and code in RETRY_STATUSES else None),
                                next_step="Resume the existing run; do not repeat a modeling POST.")
            return result
        raise ToolError("Too many redirects in website workflow.")

    def login(self, email, password):
        page = self.request("GET", "?doc=sign")
        soup = soup_for(page.text)
        form = next((form for form in soup.find_all("form") if form.select_one('input[type="password"]')), None)
        if form is None:
            if "logout" in page.text.lower():
                return {"web_authenticated": True, "reused_session": True}
            raise ToolError("Cannot identify the website login form.")
        pairs = [(key, value) for key, value in form_data(form) if key not in ("email", "password")]
        pairs.extend([("email", email), ("password", password)])
        result = self.request("POST", urljoin(page.url, form.get("action") or page.url), data=pairs)
        if soup_for(result.text).select_one('form input[type="password"]') or "logout" not in result.text.lower():
            raise AuthError("Website login was not confirmed; the account may require validation or additional login steps.")
        self.save_cookies()
        return {"web_authenticated": True, "reused_session": False}

    def inspect(self, doc, output):
        response = self.request("GET", "", params={"doc": doc})
        atomic_write(output, response.text)
        result = describe_page(response.text)
        result["snapshot"] = str(output)
        return result

    def _modeling_post(self, target, intent_path, intent, **kwargs):
        """Keep uncertainty durable even when the server sends an HTTP error."""
        try:
            return self.request("POST", target, **kwargs)
        except (ToolError, KeyboardInterrupt) as exc:
            error = exc if isinstance(exc, SubmissionUnknown) else SubmissionUnknown(
                "Modeling POST did not return a usable response; its outcome is unknown. No automatic resubmission.",
                cause_category=(exc.cause_category or exc.category) if isinstance(exc, ToolError) else "interrupted",
                retry_after_seconds=exc.retry_after_seconds if isinstance(exc, ToolError) else None,
                next_step=((exc.next_step + " ") if isinstance(exc, ToolError) and exc.next_step else "") +
                          "Inspect/recover this existing website job before continuing; do not repeat the POST.")
            intent.update(state="submission_unknown", last_error=error.as_dict())
            atomic_json(intent_path, intent)
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise error from None

    def upload_structure(self, pdb_path, directory, project="membrane_bilayer"):
        """Start one explicit user-requested upload; never retry this POST."""
        pdb_path, directory = Path(pdb_path), Path(directory)
        if project not in ("membrane_bilayer", "pdbreader"):
            raise ToolError("Unsupported preparation project.")
        if not pdb_path.is_file():
            raise ToolError("Input PDB does not exist.")
        if directory.exists():
            raise ToolError("Upload output directory already exists; inspect/recover it instead of resubmitting.")
        self.validate_auth()
        doc = "input/membrane.bilayer" if project == "membrane_bilayer" else "input/pdbreader"
        page = self.request("GET", "", params={"doc": doc})
        soup = soup_for(page.text)
        form = soup.select_one('form[name="pdb"]')
        if form is None or soup.select_one('input[type="password"]'):
            raise AuthError("No authenticated PDB upload form; run charmm-gui-cli login.")
        data = [(key, value) for key, value in form_data(form) if key not in ("select_project", "pdb_id", "jobid")]
        data.extend([("pdb_id", ""), ("jobid", "")])
        directory.mkdir(parents=True)
        atomic_write(directory / "upload-form.html", page.text)
        manifest = {"state": "upload_submitting", "input_pdb": str(pdb_path.resolve()), "project": project}
        atomic_json(directory / "web-run.json", manifest)
        with pdb_path.open("rb") as handle:
            result = self._modeling_post(modeling_target(form.get("action")), directory / "web-run.json", manifest,
                                         data=data, files={"file": (pdb_path.name, handle, "chemical/x-pdb")})
        atomic_write(directory / "upload-response.html", result.text)
        description = describe_page(result.text)
        job_field = soup_for(result.text).select_one('input[name="jobid"]')
        manifest.update(state="upload_response_received", response_url=result.url,
                        job_id=job_field.get("value") if job_field else None)
        atomic_json(directory / "web-run.json", manifest)
        return {**manifest, "page": description, "snapshot": str(directory / "upload-response.html")}

    def submit_snapshot(self, snapshot, output, overrides=None, uploads=None):
        """Submit one recorded modeling form; output intent guards against replay."""
        snapshot, output = Path(snapshot), Path(output)
        intent_path = output.with_suffix(".intent.json")
        if output.exists() or intent_path.exists():
            raise SubmissionUnknown("This step has already been attempted; inspect its saved response/intent before retrying.")
        self.validate_auth()
        soup = soup_for(snapshot.read_text(encoding="utf-8"))
        if soup.select_one('input[type="password"]'):
            raise AuthError("Saved page requires website authentication. Run charmm-gui-cli login and recover the existing job page before continuing.")
        forms = [form for form in soup.find_all("form") if form.select_one('input[name="jobid"]')]
        if len(forms) != 1:
            raise ToolError("Expected exactly one modeling form in the snapshot.")
        form = forms[0]
        target = modeling_target(form.get("action"))
        overrides, uploads = overrides or {}, uploads or {}
        pairs = [(key, value) for key, value in form_data(form) if key not in overrides and key not in uploads]
        pairs.extend(overrides.items())
        for path in uploads.values():
            if not Path(path).is_file():
                raise ToolError("A requested upload file does not exist.")
        # Exclusive creation is the claim: two processes must never both POST.
        # A partial intent is deliberately retained if a write is interrupted.
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(intent_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SubmissionUnknown("This step has already been attempted; inspect its saved response/intent before retrying.") from None
        intent = {"state": "submitting", "target": target, "fields": pairs, "upload_fields": list(uploads)}
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(intent, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        with ExitStack() as stack:
            files = {key: (Path(path).name, stack.enter_context(Path(path).open("rb")), "application/octet-stream")
                     for key, path in uploads.items()}
            result = self._modeling_post(target, intent_path, intent, data=pairs, files=files or None)
        atomic_write(output, result.text)
        intent.update(state="response_received", snapshot=str(output))
        atomic_json(intent_path, intent)
        return {"snapshot": str(output), "response_url": result.url, "page": describe_page(result.text)}
