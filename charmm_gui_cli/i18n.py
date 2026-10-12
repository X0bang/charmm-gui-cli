"""Small bilingual catalog; parse environment strings without OS locale APIs."""

import os


MESSAGES = {
    "recovery.resume": {"zh": "修复后使用 charmm-gui-cli jobs resume NAME 或 build-resume 原任务目录；不要重新 build。", "en": "After fixing the issue, use charmm-gui-cli jobs resume NAME or build-resume with the original run directory; do not build again."},
    "recovery.login_first": {"zh": "先运行 charmm-gui-cli login 恢复登录；", "en": "First run charmm-gui-cli login to restore authentication; "},
    "recovery.submission_unknown": {"zh": "POST 提交结果未知，禁止自动重发。先核对官网已有任务；确认独立建膜任务后用 charmm-gui-cli jobs attach NAME --jobid BUILD_ID，再恢复原任务。若是网页上传/准备步骤，请核对保存的 intent 和响应，不要删除记录或重传。", "en": "The POST submission outcome is unknown; automatic resubmission is prohibited. Inspect existing website jobs first. After confirming the independent bilayer job, use charmm-gui-cli jobs attach NAME --jobid BUILD_ID, then resume the original run. For website upload/preparation steps, inspect saved intents and responses; do not delete records or upload again."},
    "recovery.authentication": {"zh": "运行 charmm-gui-cli login 更新登录，然后恢复原任务；已存在的远端任务不会重新提交。", "en": "Run charmm-gui-cli login to refresh authentication, then resume the original run; existing remote jobs will not be resubmitted."},
    "recovery.access_denied": {"zh": "HTTP 403 不一定是登录过期；确认当前账户拥有该任务，必要时用 charmm-gui-cli login 登录正确账户，再恢复原任务。", "en": "HTTP 403 does not necessarily mean the session expired. Confirm the current account owns the job; if needed, use charmm-gui-cli login with the correct account, then resume the original run."},
    "recovery.transport_error": {"zh": "检查网络、代理及 TLS 证书配置；不要关闭证书验证。此错误不自动循环重试。", "en": "Check connectivity, proxy and TLS certificate settings; do not disable certificate verification. This error is not retried automatically in a loop."},
    "recovery.archive_preparing": {"zh": "远端构建完成但官网归档仍在压缩；等待至少 120 秒或记录的重试时间，再 jobs resume NAME / build-resume 原任务目录。仅重试原任务的下载 GET，不创建新任务；尚未完成本地验收。", "en": "The remote build is complete but the website is still compressing its archive. Wait at least 120 seconds or until the recorded retry time, then use jobs resume NAME / build-resume with the original run directory. Only the original download GET is retried; no new job is created. Local validation is not complete."},
    "recovery.transient": {"zh": "只读 GET 的有限重试已结束；等待网络或服务器恢复，并遵守 Retry-After 等待时间。", "en": "Bounded retries for read-only GET requests have ended. Wait for connectivity or the server to recover and respect Retry-After."},
    "recovery.interrupted": {"zh": "本地已中断，已有任务和提交记录保留；使用 jobs resume NAME 或 build-resume 原任务目录。若提示 submission_unknown，先核对/attach，不能重发 POST。", "en": "Local execution was interrupted; existing jobs and submission records are retained. Use jobs resume NAME or build-resume with the original run directory. If submission_unknown is reported, inspect/attach first; do not replay the POST."},
    "recovery.wait_timeout": {"zh": "本地等待时间已到，远端任务可能仍在运行；稍后使用 jobs resume NAME 或 build-resume 原任务目录继续，不要重新 build。", "en": "Local waiting timed out; the remote job may still be running. Later use jobs resume NAME or build-resume with the original run directory; do not build again."},
    "login.bad_creds": {"zh": "邮箱或密码错误，请确认 CHARMM-GUI 注册邮箱", "en": "Incorrect email or password; check your registered CHARMM-GUI email address."},
    "auth.invalid_json": {"zh": "会话凭据文件包含无效 JSON。", "en": "Token file contains invalid JSON."},
    "auth.expected_string": {"zh": "需要凭据字符串，或含 token 字段的 JSON 对象。", "en": "Expected a token string or a JSON object with a token field."},
    "auth.expected_jwt": {"zh": "需要 API JWT，而不是 Cookie 或文件名。请使用 --token-file PATH。", "en": "Expected an API JWT, not a Cookie or filename. Use --token-file PATH."},
    "auth.decode_failed": {"zh": "无法解析 API JWT；请检查凭据文件。", "en": "Cannot decode API JWT payload; check the token file."},
    "auth.expiry_invalid": {"zh": "JWT exp 字段不是有限时间戳。", "en": "JWT exp claim is not a finite timestamp."},
    "auth.read_explicit": {"zh": "无法读取明确指定的凭据文件。", "en": "Cannot read the explicitly selected token file."},
    "auth.missing": {"zh": "未找到 API 凭据。请运行 login 或使用 --token-file PATH。", "en": "No API token found. Run login or use --token-file PATH."},
    "auth.read_discovered": {"zh": "无法读取发现的凭据文件；请明确指定 --token-file。", "en": "Cannot read the discovered token file; select --token-file explicitly."},
    "api.invalid_jobid": {"zh": "jobid 必须是带引号的数字字符串。", "en": "jobid must be a quoted string of digits."},
    "api.token_missing": {"zh": "缺少 API 凭据。请先运行 login。", "en": "API token missing. Run login first."},
    "api.token_invalid": {"zh": "已保存的 API 凭据无效。请运行 charmm-gui-cli login。", "en": "Saved API credential is invalid. Run charmm-gui-cli login."},
    "api.token_expired": {"zh": "API 凭据已过期。请重新运行 login。", "en": "API token has expired. Run login again."},
    "api.network": {"zh": "网络请求失败。请检查连接或代理，再恢复原任务。", "en": "Network request failed. Check connectivity/proxy and resume the existing run."},
    "api.redirect": {"zh": "API 请求被重定向，未转发凭据。请核对 API 地址。", "en": "API redirected the request; credentials were not forwarded. Check the API URL."},
    "api.auth_http": {"zh": "API 返回 HTTP {code}。", "en": "API returned HTTP {code}."},
    "api.http": {"zh": "API 返回 HTTP {code}。为保护凭据，不显示响应正文。", "en": "API returned HTTP {code}. Response body omitted to protect credentials."},
    "api.http_next": {"zh": "核对官网已有任务及 HTTP 错误；不要重新提交建模。", "en": "Inspect existing website jobs and the HTTP error; do not resubmit the model."},
    "api.not_json": {"zh": "API 未返回 JSON，可能是登录页或代理 HTML 页面。", "en": "API did not return JSON (possibly a login/proxy HTML page)."},
    "api.response_shape": {"zh": "API 响应格式异常；预期为对象。", "en": "Unexpected API response shape; expected an object."},
    "api.reported_error": {"zh": "API 报告错误。请检查登录、任务访问权限及准备阶段。", "en": "API reported an error. Check authentication, job access and preparation stage."},
    "api.no_token": {"zh": "登录未返回 API 凭据。", "en": "Login returned no API token."},
    "api.expired_login": {"zh": "登录返回了已过期的凭据。", "en": "Login returned an already expired token."},
    "api.status_jobid": {"zh": "状态 API 需要任务 ID；请使用 auth --check --jobid JOB_ID。", "en": "The live status API requires a job ID; use auth --check --jobid JOB_ID."},
    "api.not_acknowledged": {"zh": "服务器未确认提交。", "en": "Submission was not acknowledged."},
    "api.no_resubmit": {"zh": "{message} 不会自动重发。请核对官网任务；若已创建任务，请使用 attach。", "en": "{message} No automatic resubmission. Check website jobs; use attach if a job was created."},
    "api.destination_exists": {"zh": "下载目标已存在；拒绝覆盖。", "en": "Download destination already exists; refusing to overwrite it."},
    "api.archive_preparing": {"zh": "官网正在压缩此任务的归档，尚未提供可下载的模型文件；原始响应已保留。", "en": "The website is compressing this job's archive; no downloadable model is available yet. The original response is retained."},
    "api.download_interrupted": {"zh": "下载中断。恢复时仅重试下载，不会提交新任务。", "en": "Download interrupted. Resume will retry the download, not submit a new job."},
    "api.download_next": {"zh": "下载中断，原始响应已保留；使用 jobs resume NAME 或 build-resume 原任务目录重试下载，不会创建新任务。", "en": "The download was interrupted and its original response is retained. Use jobs resume NAME or build-resume with the original run directory to retry the download; no new job is created."},
    "api.response_next": {"zh": "检查网络和登录状态，再恢复原任务。", "en": "Check connectivity and login, then resume the existing run."},
    "api.access_next": {"zh": "核对既有任务和账户访问权限；不要重新提交。", "en": "Inspect the existing job and account access; do not resubmit."},
    "cli.full_json": {"zh": "--full 只能与 --format json 一起使用。", "en": "--full is only supported with --format json."},
    "cli.html_out": {"zh": "导出 HTML 需要用 --out 指定一个新文件名。", "en": "HTML export requires --out with a new filename."},
    "cli.stale_report": {"zh": "最终验收证据已过期；请恢复原任务更新报告。", "en": "Final validation evidence is stale. Resume the same job to refresh the report."},
    "cli.no_report": {"zh": "尚无最终报告；请用 jobs status 或 jobs resume 查看或恢复该任务。", "en": "No final report exists yet. Use jobs status or jobs resume for this task."},
    "cli.validation_exists": {"zh": "验收输出目录已存在；请选择新目录。", "en": "Validation output directory already exists; choose a new directory."},
    "cli.fields": {"zh": "--set 和 --file 需要 NAME=VALUE 格式。", "en": "--set and --file require NAME=VALUE."},
    "cli.ligand_absent": {"zh": "输入 PDB 中缺少指定的配体残基名。", "en": "Requested ligand residue names are absent from the input PDB."},
    "cli.preparation_exists": {"zh": "准备输出目录已存在；请选择新目录。", "en": "Preparation output directory already exists; choose a new directory."},
    "cli.session_paths": {"zh": "Token 和 Cookie 文件必须使用不同路径。", "en": "Token and Cookie files must use different paths."},
    "cli.jobid_mismatch": {"zh": "API 返回了不同的任务 ID，无法确认访问权限。", "en": "API returned a different job ID; access was not confirmed."},
    "cli.not_done": {"zh": "任务尚未完成，未尝试下载。", "en": "Job is not done; download was not attempted."},
    "cli.polling": {"zh": "轮询间隔必须至少 10 秒，max-wait 必须为正数。", "en": "Polling interval must be >= 10 and max-wait must be positive."},
    "prompt.email": {"zh": "CHARMM-GUI 注册邮箱：", "en": "CHARMM-GUI email: "},
    "prompt.password": {"zh": "CHARMM-GUI 密码：", "en": "CHARMM-GUI password: "},
    "cli.error": {"zh": "错误：{message}", "en": "Error: {message}"},
    "cli.next": {"zh": "下一步：{action}", "en": "Next: {action}"},
    "cli.filesystem": {
        "zh": "文件系统操作失败；请检查路径、权限和剩余空间。",
        "en": "Filesystem operation failed; check file paths, permissions and free space.",
    },
    "cli.interrupted": {"zh": "操作中断。", "en": "Operation interrupted. "},
}

_language = None


def _normalize(value):
    value = value.strip().split(".", 1)[0].split("@", 1)[0].replace("-", "_").lower()
    if value in ("", "c", "posix"):
        return None
    return "zh" if value.startswith("zh") else "en"


def detect_lang(explicit=None, *, environ=None):
    """Pure detection, including neutral-locale fallthrough; never changes locale."""
    if explicit is not None:
        if explicit not in ("zh", "en"):
            raise ValueError("Language must be zh or en.")
        return explicit
    env = os.environ if environ is None else environ
    for variable in ("CHARMM_GUI_CLI_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        raw = env.get(variable, "")
        for value in raw.split(":") if variable == "LANGUAGE" else (raw,):
            language = _normalize(value)
            if language is not None:
                return language
    return "en"


def set_language(explicit=None):
    """Set the process cache at CLI startup (also supports embedded invocation)."""
    global _language
    _language = detect_lang(explicit)
    return _language


def get_language():
    global _language
    if _language is None:
        _language = detect_lang()
    return _language


def t(key, **params):
    versions = MESSAGES.get(key, {})
    template = versions.get(get_language()) or versions.get("en") or key
    return template.format(**params)
