"""Small HTTP boundary. Submission is deliberately never retried."""

import os
from pathlib import Path
import re
import tempfile

import requests

from ..auth import ToolError, normalize_token, token_info, recovery_guidance
from ..i18n import t
from .archive_transport import normalize_download, staging_directory
from ..http_retry import request as request_with_retry, retry_delay, transient_exception, RETRY_STATUSES
from .diagnostics import diagnostic, secret_values

API_BASE = "https://charmm-gui.org/api"
ARCHIVE_PREPARING_MESSAGE = b"Start compressing now, please download after 1-2 minutes"
ARCHIVE_PREPARING_MESSAGES = (
    ARCHIVE_PREPARING_MESSAGE,
    b"Still running compressing process, please download after 1-2 minutes",
)


class AuthError(ToolError):
    def __init__(self, message=None, **kwargs):
        kwargs.setdefault("category", "authentication")
        kwargs.setdefault("next_step", "" if kwargs.get("key") == "login.bad_creds" else recovery_guidance(kwargs["category"]))
        super().__init__(message, **kwargs)


class SubmissionUnknown(ToolError):
    def __init__(self, message=None, **kwargs):
        kwargs.setdefault("category", "submission_unknown")
        kwargs.setdefault("next_step", recovery_guidance("submission_unknown", cause_category=kwargs.get("cause_category")))
        super().__init__(message, **kwargs)


def job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,32}", value):
        raise ToolError(key="api.invalid_jobid")
    return value


class Client:
    def __init__(self, token=None, session=None, *, renewer=None):
        self.token = token
        self.session = session or requests.Session()
        self.renewer = renewer

    def _secrets(self):
        values = [self.token] if isinstance(self.token, str) else []
        try:
            values.extend(value for value in self.session.cookies.values() if isinstance(value, str))
        except (AttributeError, TypeError):
            pass
        return values

    def validate_auth(self):
        """Local preflight; run before recording a new submission intent."""
        if not self.token:
            raise AuthError(key="api.token_missing")
        if self.renewer is not None and self.renewer.profile:
            self.token = self.renewer.token(self.token)
        try:
            token, _ = normalize_token(self.token)
            expired = token_info(token)["expired"]
        except ToolError:
            raise AuthError(key="api.token_invalid") from None
        if expired:
            raise AuthError(key="api.token_expired")
        return token

    def _request(self, method, endpoint, *, authenticated=True, **kwargs):
        try:
            return self._request_once(method, endpoint, authenticated=authenticated, **kwargs)
        except AuthError as exc:
            if method.upper() != "GET" or not authenticated or exc.category != "authentication" or self.renewer is None or not self.renewer.profile:
                raise
            self.token = self.renewer.token(self.token, force=True)
            return self._request_once(method, endpoint, authenticated=authenticated, **kwargs)

    def _request_once(self, method, endpoint, *, authenticated=True, **kwargs):
        headers = {"Authorization": "Bearer " + self.validate_auth()} if authenticated else {}
        try:
            response = request_with_retry(self.session,
                method, API_BASE + endpoint, headers=headers,
                timeout=(10, 60), allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            raise ToolError(key="api.network",
                            category="transient_network" if transient_exception(exc) else "transport_error",
                            retryable=method.upper() == "GET" and transient_exception(exc),
                            next_step=recovery_guidance("transient_network" if transient_exception(exc) else "transport_error")) from None
        code = response.status_code
        # Login uses 404 for bad credentials. Never echo the response body.
        if endpoint == "/login" and not authenticated and code == 404 and "incorrect password" in response.text.lower():
            response.close()
            raise AuthError(key="login.bad_creds")
        if 300 <= code < 400:
            response.close()
            raise ToolError(key="api.redirect")
        if code in (401, 403):
            response.close()
            raise AuthError(key="api.auth_http", params={"code": code},
                            category="authentication" if code == 401 else "access_denied",
                            next_step=recovery_guidance("authentication" if code == 401 else "access_denied"))
        if code >= 400:
            summary = None
            if authenticated:
                try:
                    body = response.json()
                    if isinstance(body, dict) and body.get("error"):
                        summary = diagnostic(body, secrets=self._secrets() + secret_values(body))
                except ValueError:
                    pass
            response.close()
            raise ToolError(key="api.http", params={"code": code},
                            category="rate_limited" if code == 429 else ("transient_http" if code in RETRY_STATUSES else "http_error"),
                            retryable=method.upper() == "GET" and code in RETRY_STATUSES,
                            retry_after_seconds=(retry_delay(response.headers["Retry-After"], 0)
                                                 if "Retry-After" in response.headers and code in RETRY_STATUSES else None),
                            server_message=summary,
                            next_step=recovery_guidance("transient_http") if code in RETRY_STATUSES else "charmm-gui-cli build -h")
        return response

    @staticmethod
    def _json(response, *, secrets=(), diagnostics=True):
        try:
            data = response.json()
        except ValueError:
            raise ToolError(key="api.not_json",
                            category="unexpected_response", next_step=t("api.response_next")) from None
        finally:
            response.close()
        if not isinstance(data, dict):
            raise ToolError(key="api.response_shape", category="unexpected_response")
        if data.get("error"):
            error = data["error"]
            match = re.fullmatch(r"Invalid lipid name ([A-Za-z0-9_+-]{1,20})[.!]?", error, re.I) if isinstance(error, str) else None
            if match and diagnostics:
                message = "Invalid lipid name " + match.group(1)
                if diagnostic(message, secrets=list(secrets) + secret_values(data)) == message:
                    raise ToolError(key="api.invalid_lipid", params={"lipid": match.group(1)},
                                    category="api_error", server_message=message,
                                    next_step="charmm-gui-cli build -h")
            if isinstance(error, str) and error.strip().lower() in (
                    "invalid token", "token expired", "unauthorized", "authentication required"):
                raise AuthError(key="api.token_invalid")
            summary = diagnostic(data, secrets=list(secrets) + secret_values(data)) if diagnostics else None
            raise ToolError(key="api.rejected" if summary else "api.reported_error", category="api_error",
                            server_message=summary,
                            next_step="charmm-gui-cli build -h")
        return data

    def login(self, email, password):
        response = self._request("POST", "/login", authenticated=False,
                                 json={"email": email, "password": password})
        data = self._json(response, diagnostics=False)
        token = data.get("token")
        if not isinstance(token, str):
            raise AuthError(key="api.no_token")
        self.token, _ = normalize_token(token)
        if token_info(self.token)["expired"]:
            raise AuthError(key="api.expired_login")
        return self.token

    def status(self, jobid=None):
        # Live service returns an HTML "NO JOBID!!" notice for the
        # upstream client's check_rq=true request without a job ID.
        if jobid is None:
            raise ToolError(key="api.status_jobid")
        params = {"jobid": job_id(jobid)}
        try:
            return self._json(self._request("GET", "/check_status", params=params), secrets=self._secrets())
        except AuthError as exc:
            if exc.category != "authentication" or self.renewer is None or not self.renewer.profile:
                raise
            self.token = self.renewer.token(self.token, force=True)
            return self._json(self._request_once("GET", "/check_status", params=params), secrets=self._secrets())

    def submit(self, payload):
        # Once a POST has been attempted, a bad/missing response cannot prove
        # that no job was created. Callers must retain the submission intent.
        self.validate_auth()
        try:
            data = self._json(self._request("POST", "/quick_bilayer", data=payload), secrets=self._secrets())
            if data.get("submitted") not in (True, "true"):
                raise ToolError(key="api.not_acknowledged")
            identifier = job_id(data.get("jobid"))
        except ToolError as exc:
            raise SubmissionUnknown(
                key="api.no_resubmit", params={"message": exc.localized()},
                cause_category=exc.cause_category or exc.category,
                server_message=exc.server_message,
                retry_after_seconds=exc.retry_after_seconds,
                next_step=recovery_guidance("submission_unknown", cause_category=exc.cause_category or exc.category)
            ) from None
        return identifier

    def download(self, jobid, destination):
        destination = Path(destination)
        if destination.exists():
            raise ToolError(key="api.destination_exists")
        response = self._request("GET", "/download", params={"jobid": job_id(jobid)}, stream=True)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".download-", dir=staging_directory(destination))
        except BaseException:
            response.close()
            raise
        temporary = Path(name)
        try:
            from .transfers import ByteProgress
            with os.fdopen(fd, "wb") as handle:
                progress = ByteProgress(response)
                for chunk in response.iter_content(1024 * 1024):
                    handle.write(chunk)
                    progress.add(len(chunk))
                handle.flush()
                os.fsync(handle.fileno())
            # The live endpoint may start archive compression on the first
            # download GET. Recognize only this complete, observed response;
            # arbitrary text/HTML and malformed gzip still fail normalization.
            if temporary.stat().st_size <= max(map(len, ARCHIVE_PREPARING_MESSAGES)) + 2:
                if temporary.read_bytes() in tuple(message + suffix for message in ARCHIVE_PREPARING_MESSAGES
                                                    for suffix in (b"", b"\n", b"\r\n")):
                    raise ToolError("官网正在压缩此任务的归档，尚未提供可下载的模型文件；原始响应已保留。", key="api.archive_preparing",
                                    category="archive_preparing", retryable=True,
                                    retry_after_seconds=max(20, retry_delay(response.headers.get("Retry-After"), 0)))
            normalize_download(temporary, destination)
            progress.emit(complete=True)
        except requests.RequestException as exc:
            raise ToolError(key="api.download_interrupted",
                            category="transient_network" if transient_exception(exc) else "transport_error",
                            retryable=transient_exception(exc),
                            next_step=(t("api.download_next")
                                       if transient_exception(exc) else recovery_guidance("transport_error"))) from None
        finally:
            response.close()
            # Successful normalization removes its owned temporary files;
            # failed responses remain available for recovery/diagnosis.
        return destination
