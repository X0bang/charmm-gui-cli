#!/usr/bin/env python3
"""Build allowlisted source archives and standards-compliant pure-Python wheels.

Uses the Python standard library; no build directory, cleanup, or deletion step.
"""

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zipfile


ROOT_FILES = (".gitignore", "LICENSE", "pyproject.toml", "README.md", "RESEARCH.md", "install.sh")
DOC_FILES = ("docs/INSTALL.md", "docs/ADVANCED.md", "docs/TESTING.md", "docs/USAGE.md", "docs/RECOVERY.md", "docs/BATCH.md")
SCRIPT_FILES = ("scripts/build_release.py", "scripts/install_user.py")
EXAMPLE_FILES = ("examples/batch.yaml", "examples/crtw-build.yaml", "examples/membrane-ligand.yaml",
                 "examples/crtw-beta-carotene.yaml")


def metadata(root):
    """Read our project's string/list metadata on Python 3.10 as well as 3.11+."""
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:
        # Only the release-relevant scalar/string-list tables are interpreted.
        # ast.literal_eval never executes input. Other project metadata can
        # remain in pyproject.toml; unsupported relevant syntax fails clearly.
        sections, section = {}, ""
        pending, key = "", None
        relevant = {"project", "project.scripts", "project.optional-dependencies"}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("[") and not pending:
                section = stripped.strip("[]")
                continue
            if section not in relevant:
                continue
            if not pending:
                match = re.fullmatch(r"([\w-]+)\s*=\s*(.*)", stripped)
                if not match:
                    raise ValueError("Unsupported release metadata; use Python 3.11+ for full TOML parsing.")
                key, pending = match.groups()
            else:
                pending += "\n" + stripped
            try:
                value = ast.literal_eval(pending)
            except (SyntaxError, ValueError):
                if pending.startswith("[") and not stripped.endswith("]"):
                    continue
                raise ValueError("Unsupported release metadata; use Python 3.11+ for full TOML parsing.") from None
            sections.setdefault(section, {})[key] = value
            pending = ""
        if pending:
            raise ValueError("Incomplete project metadata.")
        project = sections.get("project", {})
        project["scripts"] = sections.get("project.scripts", {})
        project["optional-dependencies"] = sections.get("project.optional-dependencies", {})
    else:
        project = tomllib.loads(text)["project"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project["name"]):
        raise ValueError("Unsafe distribution name.")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?", project["version"]):
        raise ValueError("Release version must be a simple PEP 440 release version.")
    if not project.get("scripts"):
        raise ValueError("No command entry points were defined.")
    for name, target in project["scripts"].items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or not re.fullmatch(r"[\w.]+:[\w.]+", target):
            raise ValueError("Unsafe command entry point.")
    return project


def selected_files(root):
    """Allowlist only; never recursively include user/session/runtime data."""
    names = list(ROOT_FILES + DOC_FILES + SCRIPT_FILES + EXAMPLE_FILES)
    names += [str(path.relative_to(root)) for path in sorted((root / "charmm_gui_cli").glob("*.py"))]
    names += [str(path.relative_to(root)) for path in sorted((root / "tests").glob("test_*.py"))]
    result = []
    for name in sorted(set(names)):
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Missing or unsafe release input: {name}")
        result.append(name)
    if "charmm_gui_cli/__init__.py" not in result:
        raise ValueError("Python package is missing.")
    return result


def wheel_metadata(project):
    lines = ["Metadata-Version: 2.4", f"Name: {project['name']}", f"Version: {project['version']}",
             f"Summary: {project.get('description', '')}", f"Requires-Python: {project.get('requires-python', '>=3.10')}"]
    lines.append("License-Expression: " + project["license"])
    lines += ["License-File: " + name for name in project["license-files"]]
    lines += ["Requires-Dist: " + value for value in project.get("dependencies", [])]
    for extra, requirements in project.get("optional-dependencies", {}).items():
        lines.append("Provides-Extra: " + extra)
        for requirement in requirements:
            base, separator, marker = requirement.partition(";")
            condition = f'({marker.strip()}) and extra == "{extra}"' if separator else f'extra == "{extra}"'
            lines.append(f"Requires-Dist: {base.strip()}; {condition}")
    return "\n".join(lines) + "\n\n"


def write_wheel(root, output, project, names):
    distribution = re.sub(r"[-_.]+", "_", project["name"])
    stem = f"{distribution}-{project['version']}"
    destination = output / f"{stem}-py3-none-any.whl"
    info = stem + ".dist-info"
    records = []
    # 'x' refuses replacement, including simultaneous release processes.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        def add(name, data):
            if isinstance(data, str):
                data = data.encode("utf-8")
            archive.writestr(name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            records.append((name, "sha256=" + digest, str(len(data))))
        for name in names:
            if name.startswith("charmm_gui_cli/"):
                add(name, (root / name).read_bytes())
        for name in project["license-files"]:
            if name not in names:
                raise ValueError("License file must be included in the source allowlist.")
            add(info + "/licenses/" + name, (root / name).read_bytes())
        add(info + "/METADATA", wheel_metadata(project))
        add(info + "/WHEEL", "Wheel-Version: 1.0\nGenerator: charmm-gui-cli-allowlist\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        add(info + "/entry_points.txt", "[console_scripts]\n" + "".join(f"{name} = {target}\n" for name, target in project["scripts"].items()))
        record = io.StringIO(newline="")
        writer = csv.writer(record, lineterminator="\n")
        writer.writerows(records)
        writer.writerow((info + "/RECORD", "", ""))
        archive.writestr(info + "/RECORD", record.getvalue())
    return destination


def build(root, output, *, wheel_only=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    project = metadata(root)
    names = selected_files(root)
    output.mkdir(parents=True, exist_ok=True)
    wheel = write_wheel(root, output, project, names)
    products = [wheel]
    if not wheel_only:
        prefix = f"{project['name']}-{project['version']}"
        source = output / (prefix + ".tar.gz")
        with source.open("xb") as handle:
            with tarfile.open(fileobj=handle, mode="w:gz") as archive:
                for name in names:
                    archive.add(root / name, arcname=prefix + "/" + name, recursive=False)
        products.append(source)
    checksums = output / "SHA256SUMS"
    with checksums.open("x", encoding="utf-8") as handle:
        for path in products:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            handle.write(f"{digest}  {path.name}\n")
    return {"version": project["version"], "wheel": str(wheel),
            "source": str(products[1]) if len(products) > 1 else None,
            "checksums": str(checksums), "source_files": names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True, help="New release output location; existing artifacts are never replaced")
    parser.add_argument("--wheel-only", action="store_true")
    args = parser.parse_args()
    try:
        result = build(args.root, args.out, wheel_only=args.wheel_only)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"Release build failed: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
