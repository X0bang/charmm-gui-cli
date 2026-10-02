import argparse
from getpass import getpass
import json
import os
from pathlib import Path
import sys

import yaml

from . import __version__
from .api import Client
from .artifacts import inspect_archive
from .auth import ToolError, atomic_json, atomic_write, default_token_path, load_token, resolve_cookie_path, token_info
from .config import load_config, plan, validate_config
from . import workflow


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def brief(manifest):
    return {key: manifest[key] for key in ("state", "job_id", "remote_status", "validation") if key in manifest}


def report_summary(report, name, path):
    """Keep the everyday report readable; --full retains all evidence paths."""
    acceptance = report.get("acceptance_validation", {})
    checks = acceptance.get("checks", {})
    penalty = checks.get("cgenff", {})
    minimization = checks.get("charmm_minimization", {})
    pose = report.get("bound_pose_validation", {})
    environment = report.get("environment_validation", {})
    environment_checks = environment.get("checks", {})
    concentration = environment_checks.get("concentration_direct_evidence", {})
    return {"state": report.get("status"), "passed": report.get("passed"), "name": name,
            "compilation": report.get("compilation"),
            "environment": {"passed": environment.get("passed"),
                "lipids": environment_checks.get("requested_lipids_present", {}).get("counts"),
                "water_molecules": environment_checks.get("water_present", {}).get("molecules"),
                "ions": environment_checks.get("ion_species"),
                "concentration_M": concentration.get("requested_M"),
                "concentration_verified": concentration.get("passed")},
            "bound_pose": {key: pose[key] for key in ("passed", "protein_ca_rmsd_A",
                "ligand_rmsd_after_protein_fit_A", "ligand_max_displacement_A") if key in pose},
            "acceptance": {"passed": acceptance.get("passed"), "status": acceptance.get("status"),
                "leaflets": checks.get("leaflets", {}).get("leaflets"),
                "cgenff": {key: penalty[key] for key in ("parameter_max", "charge_max", "review_required") if key in penalty},
                "charmm": {key: minimization[key] for key in ("normal_termination", "warning_counts",
                    "most_severe_warning_level", "minimization_convergence_verified", "equilibration_verified") if key in minimization},
                "warnings": acceptance.get("warnings", [])},
            "review_required": report.get("review_required", False),
            "scientific_correctness_verified": report.get("scientific_correctness_verified", False),
            "report": str(path)}


def add_build_runtime_options(command):
    command.add_argument("--cookies", type=Path, help="Advanced: override the saved website session")
    waiting = command.add_mutually_exclusive_group()
    waiting.add_argument("--wait", dest="wait", action="store_true", help="等待完成（默认）")
    waiting.add_argument("--no-wait", dest="wait", action="store_false", help="推进后返回；远端任务继续运行")
    command.set_defaults(wait=True)
    command.add_argument("--interval", type=int, default=30)
    command.add_argument("--max-wait", type=int, default=21600)
    checking = command.add_mutually_exclusive_group()
    checking.add_argument("--grompp", action="store_true", help="要求 GROMACS 编译验证")
    checking.add_argument("--no-grompp", action="store_true", help="跳过编译，仍验证模型")
    command.add_argument("--gmx", default="gmx", help="gmx 可执行文件；默认 PATH 中的 gmx")


def parser():
    from .helptext import OVERVIEW, BUILD_GUIDE
    root = argparse.ArgumentParser(prog="charmm-gui-cli", description=OVERVIEW,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--token-file", type=Path, help="Official raw JWT file or login JSON; never the token itself")
    commands = root.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Sign in once for API and website access; save the session for future commands")
    login.add_argument("--email")
    login.add_argument("--save-to", type=Path, help="Default: ~/.config/charmm-gui-cli/session.token")
    login.add_argument("--web", action="store_true", help=argparse.SUPPRESS)
    login.add_argument("--cookies", type=Path, help="Advanced: custom Cookie file (default: beside the saved token)")
    web_login = commands.add_parser("web-login", help="Log in to website HTTP forms without a browser")
    web_login.add_argument("--email")
    web_login.add_argument("--cookies", type=Path, help="Advanced: custom Cookie file (default: saved global session)")
    web_inspect = commands.add_parser("web-inspect", help="Read a website form for workflow development (HTTP only)")
    web_inspect.add_argument("--doc", required=True)
    web_inspect.add_argument("--out", type=Path, required=True)
    web_inspect.add_argument("--cookies", type=Path)
    web_upload = commands.add_parser("web-upload", help="Upload a complex PDB via HTTP without a browser")
    web_upload.add_argument("pdb", type=Path)
    web_upload.add_argument("--out", type=Path, required=True)
    web_upload.add_argument("--project", choices=("membrane_bilayer", "pdbreader"), default="membrane_bilayer")
    web_upload.add_argument("--cookies", type=Path)
    web_step = commands.add_parser("web-step", help="Submit one recorded modeling form through HTTP")
    web_step.add_argument("snapshot", type=Path)
    web_step.add_argument("--out", type=Path, required=True)
    web_step.add_argument("--set", dest="fields", action="append", default=[], metavar="NAME=VALUE")
    web_step.add_argument("--file", dest="files", action="append", default=[], metavar="NAME=PATH")
    web_step.add_argument("--cookies", type=Path)
    auth = commands.add_parser("auth", help="Inspect token format/expiry without displaying it")
    auth.add_argument("--check", action="store_true", help="Also ask the API to validate access (read only)")
    auth.add_argument("--jobid", help="Check access to a specific job; required for --check")
    check = commands.add_parser("plan", help="Validate YAML/PDB and show the request, without network access")
    check.add_argument("config", type=Path)
    prepare = commands.add_parser("prepare", help="Inspect a local complex and create a website-handoff configuration (no upload)")
    prepare.add_argument("pdb", type=Path)
    prepare.add_argument("--ligand", action="append", required=True)
    prepare.add_argument("--out", required=True, type=Path)
    inputs = commands.add_parser("inputs", help="Combine separate protein PDB and bound-pose ligand SDF without moving heavy atoms")
    inputs.add_argument("--protein", type=Path, required=True)
    inputs.add_argument("--ligand", type=Path, required=True)
    inputs.add_argument("--out", type=Path, required=True)
    inputs.add_argument("--ligand-resname", default="LIG")
    inputs.add_argument("--hydrogens", choices=("preserve", "add_missing"), default="preserve")
    split = commands.add_parser("split", help="Split an existing complex for separate protein/ligand inputs")
    split.add_argument("pdb", type=Path)
    split.add_argument("--ligand", default="LIG", help="Ligand residue name")
    split.add_argument("--out", type=Path, required=True)
    split.add_argument("--accept-conect-bond-orders", action="store_true", required=True)
    jobs = commands.add_parser("jobs", help="命名任务、状态、恢复 / Named jobs and recovery")
    job_commands = jobs.add_subparsers(dest="jobs_command")
    job_commands.add_parser("list", help="列出本地任务（jobs 默认行为）")
    job_status = job_commands.add_parser("status", aliases=("show",), help="查看某个任务及可操作的恢复提示")
    job_status.add_argument("target", help="任务名称或目录")
    job_status.add_argument("--remote-check", action="store_true", help="只读查询远端最新状态")
    job_report = job_commands.add_parser("report", help="按任务名查看最终验收结论和审阅项")
    job_report.add_argument("target", help="任务名称或目录")
    job_report.add_argument("--full", action="store_true", help="显示完整 JSON 证据报告")
    job_resume = job_commands.add_parser("resume", help="按名称或目录恢复同一任务")
    job_resume.add_argument("target", help="任务名称或目录")
    add_build_runtime_options(job_resume)
    job_attach = job_commands.add_parser("attach", help="为提交结果不确定的任务关联已存在的建膜 job ID")
    job_attach.add_argument("target", help="任务名称或目录")
    job_attach.add_argument("--jobid", required=True)
    doctor = commands.add_parser("doctor", help="检查安装、依赖和可选 GROMACS / Check installation")
    doctor.add_argument("--gmx", default="gmx")
    build = commands.add_parser("build", help="提供结构直接建模，自动管理中间文件 / Build a model",
                                epilog=BUILD_GUIDE, formatter_class=argparse.RawDescriptionHelpFormatter)
    build.add_argument("config", nargs="?", type=Path, help="高级：版本 2 YAML；通常直接提供下列结构参数")
    structures = build.add_argument_group("原始结构 / Inputs")
    structures.add_argument("--protein", type=Path, help="蛋白 PDB（与配体处于相同结合坐标系）")
    structures.add_argument("--ligand", type=Path, help="配体 SDF 或 PDB；PDB 需可靠化学定义")
    structures.add_argument("--complex", dest="complex_pdb", type=Path, help="已有结合姿势的蛋白–配体复合物 PDB")
    structures.add_argument("--ligand-resname", help="复合物中的配体残基名，默认 LIG")
    structures.add_argument("--ligand-smiles", help="配体键级/电荷模板；PDB 需有连接图且映射唯一")
    structures.add_argument("--accept-conect-bond-orders", action="store_true", help="明确接受输入 PDB 中 CONECT 重复次数作为键级")
    structures.add_argument("--hydrogens", choices=("add_missing", "preserve"), help="默认 add_missing；按化学定义补氢，不预测 pH")
    model = build.add_argument_group("建模参数 / Model settings")
    model.add_argument("--upper", help="上层脂质比例，默认 POPC=1")
    model.add_argument("--lower", help="下层脂质比例，默认 POPC=1")
    model.add_argument("--salt", choices=("KCl", "NaCl"), help="盐，默认 KCl")
    model.add_argument("--salt-concentration", type=float, help="盐浓度 mol/L，默认 0.15")
    model.add_argument("--margin", type=float, help="膜侧向边界 Å，默认 20")
    model.add_argument("--water-padding", type=float, help="水层 Z 边界 Å，默认 22.5")
    model.add_argument("--orientation", choices=("ppm", "prepared"), help="默认 ppm；prepared 要求输入已有正确膜取向")
    model.add_argument("--n-terminal", help="N 端处理，默认 NTER")
    model.add_argument("--c-terminal", help="C 端处理，默认 CTER")
    build.add_argument("--out", type=Path, help="可选的新任务目录；默认自动创建用户数据目录下的任务")
    build.add_argument("--name", help="唯一任务名；之后可 jobs status/resume NAME")
    build.add_argument("--dry-run", action="store_true", help="只做本地输入检查，不登录、不上传；之后可 build-resume")
    build_resume = commands.add_parser("build-resume", help="继续最近或指定任务，不重复提交 / Resume last build")
    build_resume.add_argument("directory", nargs="?", type=Path, help="省略则使用最近一次任务")
    for command in (build, build_resume):
        add_build_runtime_options(command)
    batch = commands.add_parser("batch", help="从 YAML 清单批量预检和建模")
    batch.add_argument("config", type=Path)
    batch.add_argument("--out", type=Path, help="可选的新批次目录")
    batch.add_argument("--dry-run", action="store_true", help="全员本地预检；不登录或上传")
    batch_resume = commands.add_parser("batch-resume", help="恢复已有批次，保留已提交任务")
    batch_resume.add_argument("directory", type=Path)
    for command in (batch, batch_resume):
        add_build_runtime_options(command)
        command.add_argument("--max-active", type=int, default=1, help="最多同时活跃的远端任务，1-4；默认 1")
        command.add_argument("--submit-interval", type=int, default=30, help="启动新任务的最小间隔秒数，默认 30")
    batch_status = commands.add_parser("batch-status", help="查看批次汇总，不提交任务")
    batch_status.add_argument("directory", type=Path)
    batch_status.add_argument("--remote-check", action="store_true")
    batch_status.add_argument("--csv", type=Path, help="另外输出 CSV 汇总文件")
    validate = commands.add_parser("validate", help="Check downloaded GROMACS topology/ligand retention, optionally run grompp")
    validate.add_argument("system", type=Path)
    validate.add_argument("--input-manifest", type=Path, required=True)
    validate.add_argument("--build-config", type=Path, help="Also compare output lipids and salt settings against a version-2 build YAML")
    validate.add_argument("--reference-pdb", type=Path, help="Also compare bound ligand pose after protein alignment against the unique step5_assembly.pdb")
    validate.add_argument("--out", type=Path, required=True, help="New report/output directory")
    validate.add_argument("--grompp", action="store_true")
    validate.add_argument("--gmx", default="gmx")
    run = commands.add_parser("run", help="Clone a prepared source job and submit a Quick Bilayer build")
    run.add_argument("config", type=Path)
    run.add_argument("--out", required=True, type=Path)
    resume = commands.add_parser("resume", help="Check/download an existing run; never submit again")
    resume.add_argument("directory", type=Path)
    for command in (run, resume):
        command.add_argument("--wait", action="store_true")
        command.add_argument("--interval", type=int, default=30)
        command.add_argument("--max-wait", type=int, default=21600, help="Maximum polling seconds")
        command.add_argument("--pdb-member", help="Explicit final PDB path inside the tar archive")
    status = commands.add_parser("status", help="Read remote state")
    status.add_argument("jobid")
    status.add_argument("--diagnostic", action="store_true", help="Show response keys/types and state without server logs")
    attach = commands.add_parser("attach", help="Record a recovered build ID after an ambiguous submission")
    attach.add_argument("directory", type=Path)
    attach.add_argument("--jobid", required=True)
    download = commands.add_parser("download", help="Download a completed job archive without extraction")
    download.add_argument("jobid")
    download.add_argument("--out", type=Path, required=True)
    inspect = commands.add_parser("inspect", help="Check expected residue names in an assembled PDB")
    inspect.add_argument("archive", type=Path)
    inspect.add_argument("--ligand", action="append", required=True, help="Repeat for multiple residue names")
    inspect.add_argument("--pdb-member")
    return root


def dispatch(args):
    if args.command == "doctor":
        from .doctor import inspect_installation
        report = inspect_installation(args.gmx)
        emit(report)
        return 0 if report["passed"] else 2
    if args.command == "jobs":
        from . import managed
        if args.jobs_command in (None, "list"):
            emit({"jobs": managed.list_runs(), "next_step": "charmm-gui-cli jobs resume NAME"})
        elif args.jobs_command in ("status", "show"):
            emit(managed.inspect_run(args.target, remote_check=args.remote_check, token_file=args.token_file))
        elif args.jobs_command == "report":
            directory = managed.resolve_run(args.target)
            result = managed.inspect_run(directory)
            if result.get("validation_status") == "stale_or_unreadable":
                raise ToolError("Final validation evidence is stale. Resume the same job to refresh the report.")
            path = directory / "results" / "validation.json"
            if not path.is_file():
                raise ToolError("No final report exists yet. Use jobs status or jobs resume for this task.")
            report = managed._read_json(path)
            if args.full:
                emit(report)
            else:
                emit(report_summary(report, result.get("name"), path))
            return 0 if report.get("passed") else 2
        elif args.jobs_command == "resume":
            from .build_command import execute
            args.directory = managed.resolve_run(args.target)
            args.command = "build-resume"
            result, code = execute(args)
            emit(result)
            return code
        elif args.jobs_command == "attach":
            directory = managed.resolve_run(args.target)
            token, _ = load_token(args.token_file)
            result = workflow.attach(Client(token), directory / "bilayer", args.jobid)
            managed.record_recovery(directory, state="submitted")
            emit({"directory": str(directory), **brief(result), "next_step": "charmm-gui-cli jobs resume " + args.target})
        return 0
    if args.command in ("batch", "batch-resume", "batch-status"):
        from . import batch as batching, managed
        if args.command == "batch-status":
            report = batching.status(args.directory, remote_check=args.remote_check, token_file=args.token_file)
            if args.csv:
                atomic_write(args.csv, batching.export_csv(report))
                report["csv"] = str(args.csv.resolve())
            emit(report)
            return 0
        if args.command == "batch":
            directory = args.out or managed.new_batch_path()
            report = batching.initialize(args.config, directory)
            if args.dry_run:
                emit(report)
                return 2 if report.get("failed", 0) else 0
        else:
            directory = args.directory
        report, code = batching.advance(directory, max_active=args.max_active,
            submit_interval=args.submit_interval, wait=args.wait, interval=args.interval,
            max_wait=args.max_wait, token_file=args.token_file, cookies=args.cookies,
            grompp=args.grompp, no_grompp=args.no_grompp, gmx=args.gmx)
        emit(report)
        return code
    if args.command == "inputs":
        from .inputs import prepare_inputs
        emit(prepare_inputs(args.protein, args.ligand, args.out, ligand_resname=args.ligand_resname,
                            hydrogen_policy=args.hydrogens))
        return 0
    if args.command == "split":
        from .inputs import split_complex
        emit(split_complex(args.pdb, args.out, ligand_resname=args.ligand, accept_conect_bond_orders=True))
        return 0
    if args.command in ("build", "build-resume"):
        from .build_command import execute
        result, code = execute(args)
        emit(result)
        return code
    if args.command == "validate":
        from .validation import extract_for_validation, inspect_environment, validate_system
        build_config = None
        if args.build_config:
            from .build_config import load_build_config
            build_config = load_build_config(args.build_config)
        if args.out.exists():
            raise ToolError("Validation output directory already exists; choose a new directory.")
        args.out.mkdir(parents=True)
        system = args.system
        if system.is_file():
            system = extract_for_validation(system, args.out / "extracted")
        report = validate_system(system, args.input_manifest, run_grompp=args.grompp,
                                 gmx=args.gmx, output_parent=args.out)
        if build_config is not None:
            coordinates = report.get("files", {}).get("coordinates")
            if not coordinates:
                environment = {"passed": False, "error": "No validated coordinate file was identified for environment checks."}
            else:
                lipids = sorted({name for leaflet in ("upper", "lower")
                                 for name in build_config["membrane"][leaflet].split("=", 1)[0].split(":")})
                try:
                    environment = inspect_environment(
                        coordinates, expected_lipids=lipids, ion_type=build_config["ions"]["type"],
                        concentration_M=build_config["ions"]["concentration_M"], system_dir=system)
                except ToolError as exc:
                    environment = {"passed": False, "error": str(exc)}
            report["environment_validation"] = environment
            report["passed"] = bool(report["passed"] and environment["passed"])
            from .acceptance import assess_acceptance
            acceptance = assess_acceptance(system, membrane=build_config["membrane"], ligand_resname=build_config["ligand_resname"])
            report.update(acceptance_validation=acceptance, review_required=acceptance.get("review_required", False),
                          review_warnings=acceptance.get("warnings", []))
            report["passed"] = bool(report["passed"] and acceptance["passed"])
        if args.reference_pdb is not None:
            from .pose_validation import validate_bound_pose
            pose = validate_bound_pose(system, args.input_manifest, args.reference_pdb)
            report["bound_pose_validation"] = pose
            report["passed"] = bool(report["passed"] and pose["passed"])
        atomic_json(args.out / "validation.json", report)
        emit(report)
        return 0 if report["passed"] else 2
    if args.command in ("web-login", "web-inspect", "web-upload", "web-step"):
        from .web import WebClient
        cookies = resolve_cookie_path(args.cookies)
        web_client = WebClient(None if args.command == "web-login" else cookies)
        if args.command == "web-login":
            result = web_client.login(args.email or input("CHARMM-GUI email: "), getpass("CHARMM-GUI password: "))
            web_client.cookie_path = cookies
            web_client.save_cookies()
        elif args.command == "web-upload":
            result = web_client.upload_structure(args.pdb, args.out, args.project)
        elif args.command == "web-step":
            try:
                fields = dict(value.split("=", 1) for value in args.fields)
                files = dict(value.split("=", 1) for value in args.files)
            except ValueError:
                raise ToolError("--set and --file require NAME=VALUE.") from None
            result = web_client.submit_snapshot(args.snapshot, args.out, fields, files)
        else:
            result = web_client.inspect(args.doc, args.out)
        emit(result)
        return 0
    if args.command == "prepare":
        config = validate_config({
            "version": 1, "source_job_id": None, "input_pdb": str(args.pdb.resolve()),
            "expected_ligands": args.ligand,
            "preparation": {"ligand_parameters_ready": False, "orientation": "ppm"},
            "membrane": {"upper": "POPC=1", "lower": "POPC=1", "margin_A": 20, "water_padding_A": 22.5},
            "ions": {"type": "NaCl", "concentration_M": 0.15},
        })
        proposal = plan(config)
        if proposal["local_pdb"]["missing_ligands"]:
            raise ToolError("Requested ligand residue names are absent from the input PDB.")
        try:
            args.out.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise ToolError("Preparation output directory already exists; choose a new directory.") from None
        atomic_write(args.out / "system.yaml", yaml.safe_dump(config, sort_keys=False))
        atomic_json(args.out / "preparation.json", proposal)
        emit({"config": str(args.out / "system.yaml"), "report": str(args.out / "preparation.json"),
              "next_step": "Prepare the complex/ligand on the website, then fill source_job_id and preparation fields.",
              "defaults": "POPC, NaCl 0.15 M and PPM are editable placeholders, not recommendations for this protein."})
        return 0
    if args.command == "login":
        from .web import WebClient
        destination = (args.save_to or args.token_file or default_token_path()).expanduser()
        cookies = resolve_cookie_path(args.cookies, str(destination))
        if destination.resolve() == cookies.resolve():
            raise ToolError("Token and Cookie files must use different paths.")
        email = args.email or input("CHARMM-GUI email: ")
        password = getpass("CHARMM-GUI password: ")
        token = Client().login(email, password)
        # Authenticate both transports before touching the saved credentials.
        # A failed website login must not leave a new JWT paired with old cookies.
        web_client = WebClient()
        web_result = web_client.login(email, password)
        web_client.cookie_path = cookies
        web_client.save_cookies()
        atomic_write(destination, token + "\n")
        result = {"logged_in": True, "api_authenticated": True, **web_result,
                  "saved_to": str(destination), "cookie_file": str(cookies), "permissions": "0600",
                  "next_step": "charmm-gui-cli build system.yaml --out runs/my-build --wait"}
        if os.environ.get("CHARMMGUI_TOKEN"):
            result["notice"] = "CHARMMGUI_TOKEN is set and overrides the saved login. Unset it to use this session by default."
        emit(result)
        return 0
    if args.command == "plan":
        proposal = plan(load_config(args.config))
        emit(proposal)
        return 0 if proposal["ready_to_submit"] else 2
    if args.command == "inspect":
        report = inspect_archive(args.archive, args.ligand, args.pdb_member)
        emit(report)
        return 0 if report["passed"] else 2
    token, source = load_token(args.token_file)
    client = Client(token)
    if args.command == "auth":
        info = {"source": source, **token_info(token), "server_checked": False}
        cookies = resolve_cookie_path(token_source=source)
        info.update(cookie_file=str(cookies), web_session_saved=cookies.is_file())
        if args.check or args.jobid:
            data = client.status(args.jobid)
            # A newly uploaded web job has status=null before computation,
            # yet a matching job record already proves API access to it.
            if "jobid" in data and data["jobid"] != args.jobid:
                raise ToolError("API returned a different job ID; access was not confirmed.")
            if "jobid" not in data:
                workflow.status_summary(data)
            info["job_access_confirmed"] = args.jobid
            info["remote_status"] = data.get("status")
            info["server_checked"] = True
        emit(info)
        return 2 if info["expired"] else 0
    if args.command == "status":
        data = client.status(args.jobid)
        if args.diagnostic:
            emit({"job_id": args.jobid, "status": data.get("status"),
                  "last_output_file": data.get("lastOutFile"), "last_input_step": data.get("lastInpStep"),
                  "archive_available": data.get("hasTarFile"),
                  "response_shape": {key: type(value).__name__ for key, value in data.items()}})
        else:
            emit({"job_id": args.jobid, **workflow.status_summary(data)})
    elif args.command == "download":
        if workflow.status_summary(client.status(args.jobid))["status"] != "done":
            raise ToolError("Job is not done; download was not attempted.")
        emit({"archive": str(client.download(args.jobid, args.out))})
    elif args.command == "attach":
        emit(brief(workflow.attach(client, args.directory, args.jobid)))
    elif args.command in ("run", "resume"):
        if args.interval < 10 or args.max_wait <= 0:
            raise ToolError("Polling interval must be >= 10 and max-wait must be positive.")
        directory = args.out if args.command == "run" else args.directory
        if args.command == "run":
            result = workflow.start(client, load_config(args.config), directory)
            if not args.wait:
                emit(brief(result))
                return 0
        emit(brief(workflow.resume(client, directory, wait=args.wait, interval=args.interval,
                                   max_wait=args.max_wait, pdb_member=args.pdb_member)))
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return dispatch(args)
    except ToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if getattr(exc, "next_step", None):
            print(f"Next: {exc.next_step}", file=sys.stderr)
        return 2
    except OSError:
        print("Error: filesystem operation failed; check file paths, permissions and free space.", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("Interrupted. For an existing run, use resume; if submission was interrupted, check website jobs first.", file=sys.stderr)
        return 130
