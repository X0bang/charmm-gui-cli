"""Small HTTP boundary. Submission is deliberately never retried."""

import os
from pathlib import Path
import re
import tempfile

import requests

from .auth import ToolError, normalize_token, token_info
from .archive_transport import normalize_download
from .http_retry import request as request_with_retry, retry_delay, transient_exception, RETRY_STATUSES

API_BASE = "https://charmm-gui.org/api"


class AuthError(ToolError):
    def __init__(self, message, **kwargs):
        kwargs.setdefault("category", "authentication")
        kwargs.setdefault("next_step", "Run charmm-gui-cli login, then resume the existing run.")
        super().__init__(message, **kwargs)


class SubmissionUnknown(ToolError):
    def __init__(self, message, **kwargs):
        kwargs.setdefault("category", "submission_unknown")
        kwargs.setdefault("next_step", "Inspect website jobs and attach the existing job; do not submit again.")
        super().__init__(message, **kwargs)


def job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,32}", value):
        raise ToolError("jobid must be a quoted string of digits.")
    return value


class Client:
    def __init__(self, token=None, session=None):
        self.token = token
        self.session = session or requests.Session()

    def validate_auth(self):
        """Local preflight; run before recording a new submission intent."""
        if not self.token:
            raise AuthError("API token missing. Run login first.")
        try:
            token, _ = normalize_token(self.token)
            expired = token_info(token)["expired"]
        except ToolError:
            raise AuthError("Saved API credential is invalid. Run charmm-gui-cli login.") from None
        if expired:
            raise AuthError("API token has expired. Run login again.")
        return token

    def _request(self, method, endpoint, *, authenticated=True, **kwargs):
        headers = {"Authorization": "Bearer " + self.validate_auth()} if authenticated else {}
        try:
            response = request_with_retry(self.session,
                method, API_BASE + endpoint, headers=headers,
                timeout=(10, 60), allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise ToolError("Network request failed. Check connectivity/proxy and resume the existing run.",
                            category="transient_network" if transient_exception(exc) else "transport_error",
                            retryable=method.upper() == "GET" and transient_exception(exc),
                            next_step="Resume the existing run; do not create a new build.") from None
        code = response.status_code
        if 300 <= code < 400:
            response.close()
            raise ToolError("API redirected the request; credentials were not forwarded. Check the API URL.")
        if code in (401, 403):
            response.close()
            raise AuthError(f"API returned HTTP {code}.",
                            category="authentication" if code == 401 else "access_denied",
                            next_step=("Run charmm-gui-cli login, then resume the existing run." if code == 401 else
                                       "Verify the logged-in account owns this job; login with the correct account, then resume."))
        if code >= 400:
            response.close()
            raise ToolError(f"API returned HTTP {code}. Response body omitted to protect credentials.",
                            category="rate_limited" if code == 429 else ("transient_http" if code in RETRY_STATUSES else "http_error"),
                            retryable=method.upper() == "GET" and code in RETRY_STATUSES,
                            retry_after_seconds=(retry_delay(response.headers["Retry-After"], 0)
                                                 if "Retry-After" in response.headers and code in RETRY_STATUSES else None),
                            next_step="Resume the existing run later; do not create a new build.")
        return response

    @staticmethod
    def _json(response):
        try:
            data = response.json()
        except ValueError:
            raise ToolError("API did not return JSON (possibly a login/proxy HTML page).",
                            category="unexpected_response", next_step="Check connectivity and login, then resume the existing run.") from None
        finally:
            response.close()
        if not isinstance(data, dict):
            raise ToolError("Unexpected API response shape; expected an object.", category="unexpected_response")
        if data.get("error"):
            raise ToolError("API reported an error. Check authentication, job access and preparation stage.",
                            category="api_error", next_step="Inspect the existing job and account access; do not resubmit.")
        return data

    def login(self, email, password):
        response = self._request("POST", "/login", authenticated=False,
                                 json={"email": email, "password": password})
        data = self._json(response)
        token = data.get("token")
        if not isinstance(token, str):
            raise AuthError("Login returned no API token.")
        self.token, _ = normalize_token(token)
        if token_info(self.token)["expired"]:
            raise AuthError("Login returned an already expired token.")
        return self.token

    def status(self, jobid=None):
        # Live service returns an HTML "NO JOBID!!" notice for the
        # upstream client's check_rq=true request without a job ID.
        if jobid is None:
            raise ToolError("The live status API requires a job ID; use auth --check --jobid JOB_ID.")
        params = {"jobid": job_id(jobid)}
        return self._json(self._request("GET", "/check_status", params=params))

    def submit(self, payload):
        # Once a POST has been attempted, a bad/missing response cannot prove
        # that no job was created. Callers must retain the submission intent.
        self.validate_auth()
        try:
            data = self._json(self._request("POST", "/quick_bilayer", data=payload))
            if data.get("submitted") not in (True, "true"):
                raise ToolError("Submission was not acknowledged.")
            identifier = job_id(data.get("jobid"))
        except ToolError as exc:
            raise SubmissionUnknown(
                f"{exc} No automatic resubmission. Check website jobs; use attach if a job was created.",
                cause_category=exc.cause_category or exc.category,
                retry_after_seconds=exc.retry_after_seconds,
                next_step=((exc.next_step + " ") if exc.next_step else "") +
                          "Inspect website jobs and attach the existing cloned build ID; do not submit again."
            ) from None
        return identifier

    def download(self, jobid, destination):
        destination = Path(destination)
        if destination.exists():
            raise ToolError("Download destination already exists; refusing to overwrite it.")
        response = self._request("GET", "/download", params={"jobid": job_id(jobid)}, stream=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            normalize_download(temporary, destination)
        except requests.RequestException:
            raise ToolError("Download interrupted. Resume will retry the download, not submit a new job.",
                            category="transient_network", retryable=True,
                            next_step="Resume the existing run to retry the download.") from None
        finally:
            response.close()
            # Retain the temporary response for recovery/diagnosis, including
            # after failure; the current user explicitly prohibits deletion.
        return destination
