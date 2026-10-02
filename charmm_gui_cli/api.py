"""Small HTTP boundary. Submission is deliberately never retried."""

import os
from pathlib import Path
import re
import tempfile

import requests

from .auth import ToolError, normalize_token, token_info
from .archive_transport import normalize_download

API_BASE = "https://charmm-gui.org/api"


class AuthError(ToolError):
    pass


class SubmissionUnknown(ToolError):
    pass


def job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,32}", value):
        raise ToolError("jobid must be a quoted string of digits.")
    return value


class Client:
    def __init__(self, token=None, session=None):
        self.token = token
        self.session = session or requests.Session()

    def _request(self, method, endpoint, *, authenticated=True, **kwargs):
        headers = {}
        if authenticated:
            if not self.token:
                raise AuthError("API token missing. Run login first.")
            token, _ = normalize_token(self.token)
            if token_info(token)["expired"]:
                raise AuthError("API token has expired. Run login again.")
            headers["Authorization"] = "Bearer " + token
        try:
            response = self.session.request(
                method, API_BASE + endpoint, headers=headers,
                timeout=(10, 60), allow_redirects=False, **kwargs,
            )
        except requests.RequestException:
            raise ToolError("Network request failed. Check connectivity/proxy and retry status queries.") from None
        code = response.status_code
        if 300 <= code < 400:
            response.close()
            raise ToolError("API redirected the request; credentials were not forwarded. Check the API URL.")
        if code in (401, 403):
            response.close()
            raise AuthError(f"API returned HTTP {code}. Token may be expired/rejected, or job access is denied.")
        if code >= 400:
            response.close()
            raise ToolError(f"API returned HTTP {code}. Response body omitted to protect credentials.")
        return response

    @staticmethod
    def _json(response):
        try:
            data = response.json()
        except ValueError:
            raise ToolError("API did not return JSON (possibly a login/proxy HTML page).") from None
        finally:
            response.close()
        if not isinstance(data, dict):
            raise ToolError("Unexpected API response shape; expected an object.")
        if data.get("error"):
            raise ToolError("API reported an error. Check authentication, job access and preparation stage.")
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
        try:
            data = self._json(self._request("POST", "/quick_bilayer", data=payload))
            if data.get("submitted") not in (True, "true"):
                raise ToolError("Submission was not acknowledged.")
            identifier = job_id(data.get("jobid"))
        except ToolError as exc:
            raise SubmissionUnknown(
                f"{exc} No automatic resubmission. Check website jobs; use attach if a job was created."
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
            raise ToolError("Download interrupted. Resume will retry the download, not submit a new job.") from None
        finally:
            response.close()
            # Retain the temporary response for recovery/diagnosis, including
            # after failure; the current user explicitly prohibits deletion.
        return destination
