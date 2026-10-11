"""Offline, escaped result presentation; never submits or repairs a model."""

import base64
import hashlib
import html
import json
from pathlib import Path
import tarfile

from .auth import ToolError
from .progress import official_links


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_images(directory, report):
    """Read existing previews only; reject stale, changed or unrelated files."""
    directory = Path(directory).resolve()
    cache = directory / "results/preview.json"
    if not cache.is_file():
        return [], "尚无模型预览；可运行 charmm-gui-cli preview NAME。"
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        identity = saved["identity"]
        if not report.get("fingerprint") or identity["validation_fingerprint"] != report["fingerprint"]:
            raise ValueError("stale preview")
        source = Path(saved["source"])
        model = Path(saved["model"])
        roots = (directory / "results/checks", directory / "results/previews")
        if (source.is_symlink() or model.is_symlink() or
                not source.resolve().is_relative_to(roots[0]) or
                not model.resolve().is_relative_to(roots[1])):
            raise ValueError("unrelated model")
        expected = saved["source_sha256"]
        if identity["source_sha256"] != expected or _hash(source) != expected or _hash(model) != expected:
            raise ValueError("changed model")
        if saved["scope"] not in ("final_simulation_input", "pre_input_assembly"):
            raise ValueError("unexpected preview scope")
        # Bind the extracted coordinate file to the actual downloaded member,
        # not merely to a user-editable preview manifest.
        suffix = "/gromacs/step5_input.pdb" if saved["scope"] == "final_simulation_input" else "/step5_assembly.pdb"
        with tarfile.open(directory / "bilayer/charmm-gui.tgz", "r:gz") as archive:
            members = [m for m in archive.getmembers() if m.isfile() and m.name.endswith(suffix)]
            if len(members) != 1:
                raise ValueError("ambiguous archive model")
            digest = hashlib.sha256()
            with archive.extractfile(members[0]) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError("archive model mismatch")
        images = []
        for entry in saved["images"]:
            path = Path(entry["file"])
            if (entry["view"] not in ("side", "top", "ligand") or path.is_symlink() or
                    not path.resolve().is_relative_to(roots[1]) or path.stat().st_size > 20 * 1024 * 1024):
                raise ValueError("unsafe preview image")
            data = path.read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("changed preview image")
            images.append({"view": entry["view"], "file": str(path), "data": data})
        return images, ("最终模拟输入坐标预览" if saved["scope"] == "final_simulation_input" else
                        "组装阶段坐标预览，不是最终模拟输入")
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError):
        return [], "已有预览过期、损坏或与当前归档不一致；未载入图片，请重新运行 preview。"


def display_result(summary, context, manifest, report):
    """A fixed-schema display model, excluding credentials and raw server data."""
    config = manifest.get("config", {})
    return {**summary, "directory": context.get("directory"),
            "source_job_id": context.get("source_job_id"), "build_job_id": context.get("build_job_id"),
            "parameters": {"membrane": config.get("membrane"), "ions": config.get("ions"),
                           "orientation": config.get("preparation", {}).get("orientation")},
            "compilation_status": context.get("compilation_status", "unknown"),
            "atom_counts": report.get("checks", {}).get("total_atom_count"),
            "residue_counts": report.get("coordinate_residue_counts"),
            "warnings": report.get("review_warnings", []),
            "archive": context.get("archive"),
            "official_links": official_links(source_job_id=context.get("source_job_id"),
                                             build_job_id=context.get("build_job_id")),
            "simulation_note": "结构验收和 GROMACS 输入编译不代表已执行平衡或生产 MD，也不证明充分最小化。"}


def _value(value):
    if value is None:
        return "未确认"
    if value is True:
        return "通过"
    if value is False:
        return "未通过"
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)


def render_text(result):
    passed = result.get("passed")
    state = "通过，需人工复核" if passed is True and result.get("review_required") else _value(passed)
    compilation = {"passed": "通过", "failed": "未通过", "not_run": "未执行", "unknown": "未确认"}
    parameters = result.get("parameters", {})
    membrane, ions = parameters.get("membrane") or {}, parameters.get("ions") or {}
    environment = result.get("environment") or {}
    leaflets = result.get("acceptance", {}).get("leaflets") or {}
    atoms = result.get("atom_counts") or {}
    pose = result.get("bound_pose") or {}
    lines = ["CHARMM-GUI 建模结果", f"任务：{result.get('name') or result.get('directory')}",
             f"自动验收：{state}", f"GROMACS 输入编译：{compilation.get(result.get('compilation_status'), '未确认')}",
             f"源任务：{_value(result.get('source_job_id'))}；建膜任务：{_value(result.get('build_job_id'))}",
             "", "建模参数",
             f"  上叶：{_value(membrane.get('upper'))}；下叶：{_value(membrane.get('lower'))}",
             f"  盐：{_value(ions.get('type'))} {_value(ions.get('concentration_M'))} M；取向：{_value(parameters.get('orientation'))}",
             f"  膜边界：{_value(membrane.get('margin_A'))} Å；水层边界：{_value(membrane.get('water_padding_A'))} Å",
             "", "最终体系",
             f"  原子：坐标 {_value(atoms.get('coordinates'))} / 拓扑 {_value(atoms.get('topology'))}",
             f"  脂质：{_value(environment.get('lipids'))}；水分子：{_value(environment.get('water_molecules'))}",
             f"  上叶脂质：{_value(leaflets.get('upper', {}).get('lipid_counts'))}；下叶脂质：{_value(leaflets.get('lower', {}).get('lipid_counts'))}",
             "  离子：" + _value(environment.get("ions")),
             f"  盐浓度直接证据：{_value(environment.get('concentration_verified'))}",
             "", "结合位姿（同一蛋白拟合后）",
             f"  配体重原子 RMSD：{_value(pose.get('ligand_rmsd_after_protein_fit_A'))} Å",
             f"  配体最大位移：{_value(pose.get('ligand_max_displacement_A'))} Å"]
    if result.get("components"):
        lines.append("各组分：" + _value(result["components"]))
    lines.extend(["", "需要复核的警告："])
    warnings = result.get("warnings", [])
    lines.extend("- " + _value(item) for item in warnings)
    if not warnings:
        lines.append("未记录审查警告；这不等于科学质量已充分验证。")
    lines.extend(["", "归档：" + _value(result.get("archive")), "完整报告：" + _value(result.get("report")),
                  result["simulation_note"]])
    return "\n".join(lines) + "\n"


def render_html(result, images=(), preview_note=""):
    escape = lambda value: html.escape(str(value), quote=True)
    labels = {"side": "侧视图", "top": "俯视图", "ligand": "配体邻域"}
    figures = "".join('<figure><figcaption>' + labels[item["view"]] + '</figcaption><img alt="' +
                      labels[item["view"]] + '" src="data:image/png;base64,' +
                      base64.b64encode(item["data"]).decode("ascii") + '"></figure>' for item in images)
    links = "".join('<li><a rel="noreferrer" href="' + escape(url) + '">' + escape(role) + '</a></li>'
                    for role, url in result.get("official_links", {}).items())
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; '
            'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            '<title>CHARMM-GUI 建模结果</title><style>body{font-family:system-ui,sans-serif;'
            'max-width:1100px;margin:2rem auto;padding:0 1rem;color:#20303d}pre{white-space:pre-wrap;'
            'overflow-wrap:anywhere;background:#f4f7fa;padding:1rem}img{width:100%;height:auto}'
            'figure{margin:1rem 0}figcaption{font-weight:600}a{color:#156781}</style>'
            '<h1>CHARMM-GUI 建模结果</h1><pre>' + escape(render_text(result)) + '</pre>'
            '<h2>坐标预览</h2><p>' + escape(preview_note) + '</p>' + figures +
            '<h2>官方任务页面</h2><ul>' + links + '</ul></html>')


def write_new(path, content):
    """Explicit export only, exclusively create; never overwrite an old report."""
    path = Path(path).expanduser()
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        raise ToolError("Cannot create report; choose a new filename in an existing writable directory.") from exc
    return str(path.resolve())
