"""Credential-free Chinese progress labels and evidence-aware result summaries.

Quick Bilayer's Step 2 is its results page, not the second internal build task.
Neither page numbers nor output filenames establish validation or MD execution.
"""

import re
import sys
import time
import threading
import shutil
import unicodedata
from datetime import datetime
from urllib.parse import urlencode
from ..i18n import t, translate_literal


WEB_BASE = "https://www.charmm-gui.org/"
CONSOLE_LOCK = threading.RLock()


def clear_line():
    return "\r" + " " * max(0, shutil.get_terminal_size((80, 24)).columns - 1) + "\r"


def _width(text):
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _clip(text, width):
    if width <= 0:
        return ""
    if _width(text) <= width:
        return text
    result = ""
    for char in text:
        if _width(result + char) > width - 1:
            break
        result += char
    return result + "…"


def terminal_frame(label, job, elapsed, counter):
    suffix = (" | " + job if job else "") + " | " + t("progress.elapsed", elapsed=f"{elapsed // 60}:{elapsed % 60:02d}")
    width = max(0, shutil.get_terminal_size((80, 24)).columns - 1)
    body = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[counter % 10] + " " + _clip(label, width - _width(suffix) - 2) + suffix
    return clear_line() + _clip(body, width)
UI_NOTE = ("Quick Bilayer 网页只有 Step 1 / Step 2；Step 2 是建膜任务/结果页。"
           "内部 step5、step6、step7 文件编号不是网页进度，也不表示已运行平衡或生产 MD。")

_LABELS = {
    "preparing_inputs": ("inputs", "本地检查蛋白和配体，准备输入"),
    "preparing_inputs (local)": ("inputs", "本地检查蛋白和配体，准备输入"),
    "inputs_ready": ("inputs", "本地输入已准备；尚未确认远端建模完成"),
    "uploading": ("source", "上传复合物，建立结构准备源任务"),
    "selecting_components": ("source", "选择蛋白链和配体"),
    "parameterizing": ("source", "为配体生成参数并准备蛋白拓扑"),
    "preparation_running": ("source", "结构准备与配体参数化正在运行"),
    "preparation_ready": ("source", "结构准备页面就绪；仍需核对产物"),
    "prepared": ("source", "结构准备完成，可提交膜系统构建"),
    "prepared_reused": ("source", "已绑定核验过的前处理源；尚未独立建膜"),
    "submitting_bilayer": ("source", "使用源任务提交新的膜系统构建任务"),
    "bilayer_submitted": ("build", "膜系统构建已提交；尚未完成"),
    "submitted": ("build", "膜系统构建已提交，等待调度"),
    "pending": ("build", "膜系统构建正在排队"),
    "running": ("build", "膜系统构建正在运行"),
    "done": ("build", "远端报告完成；还需下载归档并进行本地验收"),
    "downloading": ("build", "正在下载最终归档；尚未完成本地验收"),
    "archive_preparing": ("build", "远端构建完成，官网正在压缩归档；等待后重试原任务下载，尚未验收"),
    "downloaded_unverified": ("archive", "归档已下载；完整性与模型检查尚未全部确认"),
    "ligands_present": ("archive", "归档初检通过、已发现配体；仍需完整模型验收"),
    "validating_final_archive (local)": ("validation", "正在本地验收拓扑、配体位姿、脂膜、离子与日志"),
    "validated": ("validation", "已有验收完成记录；通过情况以报告为准"),
    "validation_failed": ("validation", "本地验收未通过，请检查验证报告"),
    "remote_error": ("build", "远端建模失败；保留原任务，不自动重建"),
    "error": ("build", "远端报告失败；保留原任务，不自动重建"),
    "preparation_error": ("source", "结构准备失败，请检查保存的页面和日志"),
    "preparation_failed": ("source", "结构准备失败，请检查保存的页面和日志"),
    "preparation_auth_required": ("source", "官网会话需要登录；之后恢复原任务"),
    "preparation_unknown": ("source", "结构准备状态尚不能确认，请检查保存的页面"),
    "preparation_unrecognized": ("source", "结构准备状态尚不能确认，请检查保存的页面"),
    "submission_unknown": ("recovery", "提交结果不确定；须核对既有任务，不能重复提交"),
    "waiting_network": ("recovery", "等待网络或服务器限流恢复；保留原任务"),
    "paused_auth": ("recovery", "需要重新登录；保留原任务及未解决的提交记录"),
    "paused_network": ("recovery", "网络、代理或证书异常；修复后显式恢复原任务，不自动循环重试"),
    "interrupted": ("recovery", "本地已中断；原任务和提交记录已保留"),
    "paused_inspection": ("recovery", "任务已暂停，等待核对恢复信息"),
}

_INTERNAL = {
    "quick_bilayer": "膜系统构建",
    "ffconverter": "力场格式转换与模拟输入文件生成",
}
_OUTPUTS = {
    "ppm.out": "蛋白膜取向处理",
    "step2_orient.out": "蛋白膜取向处理",
    "step3_size.out": "计算膜系统尺寸",
    "step3_packing.out": "脂质排列",
    "step3_packing_head.out": "脂质排列",
    "step4_lipid.out": "脂质坐标构建",
    "step4_lipid_checkchol.out": "脂质与胆固醇结构检查",
    "step4.1_waterbox.out": "水盒构建",
    "step4.2_waterbox.out": "水盒构建",
    "step4.2_water.out": "溶剂构建",
    "step4.3_ion.out": "离子放置",
    "step5_assembly.out": "系统组装",
    "step5_input.out": "最终系统检查与输入文件准备",
}


def _identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{1,32}", value) else None


def official_links(*, source_job_id=None, build_job_id=None):
    """Construct only fixed official destinations from validated numeric IDs."""
    links = {}
    source, build = _identifier(source_job_id), _identifier(build_job_id)
    if source:
        links["source_preparation"] = WEB_BASE + "?" + urlencode({
            "doc": "input/pdbreader", "jobid": source, "project": "membrane_bilayer"}, safe="/")
    if build:
        links["build_results"] = WEB_BASE + "?" + urlencode({
            "doc": "input/membrane.quick", "jobid": build, "step": "2"}, safe="/")
    return links


def describe_progress(event, *, source_job_id=None, build_job_id=None, localized=False):
    """Translate known events without reflecting raw server messages or secrets.

    Legacy callbacks use job_id for two roles. Preparation/submission events
    identify the source; API queue/running/download events identify the build.
    Explicit source_job_id/build_job_id are preferred whenever available.
    """
    state = event.get("state")
    state = state if isinstance(state, str) else "unknown"
    running = state.startswith("running ")
    base = "running" if running else "archive_preparing" if state == "compressing" else state
    stage, label = _LABELS.get(base, ("unknown", "状态尚未识别；请查看任务记录"))
    source = _identifier(event.get("source_job_id")) or _identifier(source_job_id)
    build = _identifier(event.get("build_job_id")) or _identifier(build_job_id)
    legacy_id = _identifier(event.get("job_id"))
    if stage == "source":
        source = _identifier(event.get("source_job_id")) or legacy_id or source
    elif stage == "build":
        build = _identifier(event.get("build_job_id")) or legacy_id or build
    internal = state[8:] if running and state[8:] in _INTERNAL else None
    output = event.get("last_output_file", event.get("lastOutFile"))
    output = output if isinstance(output, str) and output in _OUTPUTS else None
    if internal:
        label = _INTERNAL[internal] + "正在运行"
    elif running and output:
        label = "膜系统构建正在运行"
    semantics = {
        "done": "remote_done_not_validated", "archive_preparing": "remote_done_archive_preparing", "downloaded_unverified": "archive_downloaded_not_validated",
        "ligands_present": "archive_precheck_passed", "validated": "validation_recorded",
        "validation_failed": "validation_failed", "remote_error": "remote_failed", "error": "remote_failed",
        "preparation_failed": "preparation_failed", "preparation_error": "preparation_failed",
        "submission_unknown": "submission_requires_inspection",
    }.get(base, "in_progress" if stage != "unknown" else "unknown")
    result = {"stage": stage, "label": label, "terminal_semantics": semantics,
              "source_job_id": source, "build_job_id": build,
              "official_links": official_links(source_job_id=source, build_job_id=build),
              "remote_done": True if base in ("done", "archive_preparing") else None, "archive_verified": base == "ligands_present",
              "validation_passed": None, "ui_note": UI_NOTE}
    if internal:
        result["internal_stage"] = internal
    if output:
        result.update(last_output_file=output, last_output_label=_OUTPUTS[output],
                      output_note="最近输出文件只说明已到达的内部环节，不单独证明当前阶段或任务完成。")
    if localized:
        for key in ("label", "ui_note", "last_output_label", "output_note"):
            if key in result:
                result[key] = translate_literal(result[key], prefix="progress.")
    return result


def format_progress(event, **context):
    detail = describe_progress(event, localized=True, **context)
    if event.get("state") in ("done", "downloading", "downloaded_unverified", "ligands_present", "inputs_ready", "validated"):
        detail["label"] = t("stage." + event["state"])
    fields = ["[charmm-gui-cli] " + detail["label"]]
    if detail["source_job_id"]:
        fields.append(t("progress.source", jobid=detail["source_job_id"]))
    if detail["build_job_id"]:
        fields.append(t("progress.build", jobid=detail["build_job_id"]))
    if detail.get("last_output_label"):
        fields.append(detail["last_output_label"])
    return " | ".join(fields)


def emit_progress(event, *, stream=None, **context):
    """Print only to stderr by default; stdout remains machine-readable JSON."""
    print(format_progress(event, **context), file=sys.stderr if stream is None else stream, flush=True)


class ProgressReporter:
    """A per-run callback retaining ID roles and emitting each official link once."""

    def __init__(self, *, source_job_id=None, build_job_id=None, stream=None):
        self.source_job_id = _identifier(source_job_id)
        self.build_job_id = _identifier(build_job_id)
        self.stream = stream
        self._seen_links = set()
        self.started = time.monotonic()
        self.last_record = self.started
        self.last_phase = None
        self.counter = 0
        self._stop = threading.Event()
        self._thread = None
        self._current_text = None
        self._current_job = None

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            stream = sys.stderr if self.stream is None else self.stream
            with CONSOLE_LOCK:
                print(clear_line(), end="", file=stream, flush=True)

    def _paint(self, stream):
        with CONSOLE_LOCK:
            elapsed = int(time.monotonic() - self.started)
            print(terminal_frame(self._current_text or "", self._current_job, elapsed, self.counter),
                  end="", file=stream, flush=True)
            self.counter += 1

    def _animate(self, stream):
        while not self._stop.wait(0.25):
            if self._current_text:
                try:
                    self._paint(stream)
                except (OSError, ValueError):
                    return

    def __call__(self, event):
        with CONSOLE_LOCK:
            self._report(event)

    def _report(self, event):
        detail = describe_progress(event, source_job_id=self.source_job_id, build_job_id=self.build_job_id)
        self.source_job_id, self.build_job_id = detail["source_job_id"], detail["build_job_id"]
        stream = sys.stderr if self.stream is None else self.stream
        phase = (detail["stage"], event.get("state"), detail.get("internal_stage"),
                 detail.get("last_output_file"), self.source_job_id, self.build_job_id)
        elapsed = int(time.monotonic() - self.started)
        text = format_progress(event, source_job_id=self.source_job_id, build_job_id=self.build_job_id)
        self._current_text = text.split(" | ", 1)[0].removeprefix("[charmm-gui-cli] ")
        self._current_job = (t("progress.build", jobid=self.build_job_id) if self.build_job_id else
                             t("progress.source", jobid=self.source_job_id) if self.source_job_id else None)
        text += " | " + t("progress.elapsed", elapsed=f"{elapsed // 60}:{elapsed % 60:02d}")
        if phase != self.last_phase or time.monotonic() - self.last_record >= 300:
            if stream.isatty() and self.last_phase is not None:
                print(clear_line(), end="", file=stream)
            print(datetime.now().strftime("[%H:%M:%S] ") + text, file=stream, flush=True)
            self.last_phase = phase
            self.last_record = time.monotonic()
        for role, url in detail["official_links"].items():
            if url not in self._seen_links:
                self._seen_links.add(url)
                title = t("progress.source_link") if role == "source_preparation" else t("progress.build_link")
                print(f"[charmm-gui-cli] {title}: {url}", file=stream, flush=True)
        if stream.isatty():
            self._paint(stream)
            if self._thread is None:
                self._thread = threading.Thread(target=self._animate, args=(stream,), daemon=True)
                self._thread.start()


def result_presentation(summary, report=None):
    """Add truthful terminal semantics without changing the durable run state."""
    report = report if isinstance(report, dict) else {}
    result = describe_progress(summary)
    validation = summary.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    passed = report.get("passed", validation.get("passed", summary.get("passed")))
    review = report.get("review_required", validation.get("review_required", summary.get("review_required", False))) is True
    warnings = report.get("review_warnings", [])
    warning_count = len(warnings) if report and isinstance(warnings, list) else summary.get("review_warning_count")
    review = review or (isinstance(warning_count, int) and warning_count > 0)
    checks = report.get("checks", {})
    checks = checks if isinstance(checks, dict) else {}
    check = checks.get("grompp", {})
    check = check if isinstance(check, dict) else {}
    compiled = check.get("passed") is True if report else summary.get("gromacs_compiled") is True
    stored_compilation = summary.get("compilation_status", "unknown")
    stored_compilation = stored_compilation if stored_compilation in ("passed", "failed", "not_run") else "unknown"
    compilation = "passed" if compiled else ("failed" if check else "not_run" if report else stored_compilation)
    if summary.get("state") == "validated" and passed is True:
        result.update(terminal_semantics="validation_passed_with_warnings" if review else "validation_passed",
                      label="自动验收通过；存在警告，需人工复核" if review else "自动验收通过",
                      archive_verified=True, validation_passed=True)
    elif passed is False or summary.get("state") == "validation_failed":
        result.update(terminal_semantics="validation_failed", label="自动验收未通过，请检查验证报告",
                      validation_passed=False)
    result.update(review_required=review, warning_count=warning_count, gromacs_compiled=compiled,
                  compilation_status=compilation,
                  compilation_label={"passed": "GROMACS 输入编译检查通过", "failed": "GROMACS 输入编译检查未通过",
                                     "not_run": "未运行 GROMACS 输入编译检查", "unknown": "未确认 GROMACS 输入编译通过"}[compilation],
                  scientific_correctness_verified=False,
                  simulation_note="结构验收与输入编译不等于已执行平衡/生产 MD，也不证明体系充分平衡或科学结论正确。")
    return result
