"""Headless coordinate-driven molecular previews, not a geometry certificate.

Only supplied coordinates are projected. Protein is a C-alpha trace, lipids are
headgroups/coarse tail traces, and selected components are heavy-atom spheres.
No membrane planes, atom positions, chemical bonds, or missing atoms are invented.
"""

from charmm_gui_cli.store.layout import full_archive_path

from collections import Counter
from statistics import mean
import hashlib
import json
import math
from pathlib import Path
import shutil

from charmm_gui_cli.store.auth import ToolError, atomic_json


WATER = {"TIP3", "TIP4", "HOH", "WAT", "SOL"}
IONS = {"POT", "SOD", "CLA", "NA", "CL", "K", "CA", "MG", "ZN"}
PROTEIN = set("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL HSD HSE HSP CYX CYN ASH GLH LYN".split())
TAIL_NAMES = {f"C{chain}{position}" for chain in (2, 3) for position in (2, 6, 10, 14, 18, 22)}
ELEMENT_COLORS = {"C": (224, 133, 55), "N": (79, 121, 190), "O": (193, 77, 83),
                  "S": (199, 176, 54), "P": (162, 108, 164), "F": (106, 160, 109),
                  "Cl": (106, 160, 109), "Br": (150, 87, 60)}


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_scene(path, ligand_resnames=("LIG",)):
    try:
        return _read_scene(path, ligand_resnames)
    except UnicodeError:
        raise ToolError("Preview requires readable text PDB/PSF files.") from None


def _read_scene(path, ligand_resnames=("LIG",)):
    """Read standard/CHARMM PDB; large-system serial overflow is not an identity."""
    path = Path(path).expanduser().resolve()
    selected = set(ligand_resnames)
    protein, components, membranes, hidden = [], [], {}, Counter()
    bonds, model_count, count, index_names = [], 0, 0, {}
    with path.open(errors="strict") as handle:
        for line in handle:
            if line.startswith("MODEL "):
                model_count += 1
                if model_count > 1:
                    raise ToolError("Preview requires one PDB model; select it explicitly.")
            if line.startswith("CONECT"):
                try:
                    values = [int(line[i:i + 5]) for i in range(6, len(line), 5) if line[i:i + 5].strip()]
                    bonds.extend((values[0], v) for v in values[1:])
                except (ValueError, IndexError):
                    raise ToolError("Preview encountered malformed CONECT records.") from None
            if not line.startswith(("ATOM  ", "HETATM")):
                continue
            count += 1
            if line[16:17].strip():
                raise ToolError("Resolve alternate locations before rendering a preview.")
            try:
                xyz = tuple(float(line[i:i + 8]) for i in (30, 38, 46))
                if not all(math.isfinite(v) for v in xyz):
                    raise ValueError
            except ValueError:
                raise ToolError("Preview requires finite PDB coordinates.") from None
            name, residue = line[12:16].strip(), line[17:21].strip()
            segment, chain = line[72:76].strip(), line[21:22].strip()
            element = line[76:78].strip().title()
            identity = (segment, chain, line[22:27].strip(), residue)
            atom = {"index": count, "name": name, "resname": residue, "identity": identity,
                    "element": element, "xyz": xyz}
            if residue in WATER:
                hidden["water_atoms"] += 1
                continue
            if residue in IONS and residue not in selected and segment != "MEMB":
                hidden["ion_atoms"] += 1
                continue
            if element in ("H", "D", "T") or (not element and name.startswith("H")):
                hidden["hydrogen_atoms"] += 1
                continue
            if segment == "MEMB":
                if name in ("P", "O3") or name in TAIL_NAMES:
                    membranes.setdefault(identity, []).append(atom)
            elif residue in selected or residue not in PROTEIN:
                components.append(atom)
                try:
                    index_names[int(line[6:11])] = atom
                except ValueError:
                    pass  # PSF order, not overflowing serial text, handles final models.
            else:
                protein.append(atom)
    if not count or not (protein or components or membranes):
        raise ToolError("Preview found no visible molecular coordinates.")
    by_index = {a["index"]: a for a in components}
    edges = {tuple(sorted((index_names[a]["index"], index_names[b]["index"])))
             for a, b in bonds if a in index_names and b in index_names and a != b}
    psf = path.with_suffix(".psf")
    if not edges and psf.is_file():
        edges = _psf_edges(psf, by_index, count)
    return {"source": str(path), "source_sha256": _digest(path), "total_atoms": count,
            "protein": protein, "components": components, "membranes": membranes,
            "component_bonds": sorted(edges), "hidden": dict(hidden)}


def _psf_edges(path, selected, coordinate_count):
    try:
        return _read_psf_edges(path, selected, coordinate_count)
    except (ValueError, StopIteration, IndexError):
        raise ToolError("Malformed PSF atom/bond table during preview.") from None


def _read_psf_edges(path, selected, coordinate_count):
    edges = set()
    with Path(path).open() as handle:
        for line in handle:
            if "!NATOM" in line:
                count = int(line.split()[0])
                if count != coordinate_count:
                    raise ToolError("Preview PSF/PDB atom counts differ; cannot map bonds safely.")
                for index in range(1, count + 1):
                    fields = next(handle).split()
                    if index in selected and (len(fields) < 6 or fields[3:5] != [selected[index]["resname"], selected[index]["name"]]):
                        raise ToolError("Preview PSF/PDB component atom order differs.")
                break
        for line in handle:
            if "!NBOND" in line:
                remaining = int(line.split()[0]) * 2
                pending = None
                while remaining:
                    line = next(handle)
                    values = [int(v) for v in line.split()]
                    if len(values) > remaining:
                        raise ToolError("Malformed PSF bond count during preview.")
                    for value in values:
                        if pending is None:
                            pending = value
                        else:
                            if value in selected and pending in selected:
                                edges.add(tuple(sorted((pending, value))))
                            pending = None
                    remaining -= len(values)
                break
    return edges


def _dependencies():
    try:
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
    except ImportError:
        raise ToolError("PNG previews need NumPy and Pillow; install charmm-gui-cli[chem].",
                        category="local_dependency") from None
    return np, Image, ImageDraw, ImageFont, PngImagePlugin


def _font(ImageFont, size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def _primitives(scene, close=False):
    primitives = []
    components = scene["components"]
    center = tuple(sum(a["xyz"][i] for a in components) / len(components) for i in range(3)) if components else None
    protein = scene["protein"]
    groups = {}
    for atom in protein:
        if close and center and math.dist(atom["xyz"], center) > 24:
            continue
        if atom["name"] == "CA":
            groups.setdefault(atom["identity"][:2], []).append(atom)
        if close:
            primitives.append(("sphere", atom["xyz"], None, .28, (185, 202, 208)))
    colors = ((66, 130, 155), (117, 107, 163), (75, 145, 133), (159, 116, 146))
    for index, atoms in enumerate(groups.values()):
        color = colors[index % len(colors)]
        trace_radius = .42 if close else 1.05
        for atom in atoms:
            primitives.append(("sphere", atom["xyz"], None, trace_radius, color))
        for a, b in zip(atoms, atoms[1:]):
            if 2.8 <= math.dist(a["xyz"], b["xyz"]) <= 4.6:
                primitives.append(("line", a["xyz"], b["xyz"], trace_radius, color))
    if not close:
        head_z = [a["xyz"][2] for atoms in scene["membranes"].values() for a in atoms if a["name"] in ("P", "O3")]
        visual_midplane = mean(head_z) if head_z else 0
        for atoms in scene["membranes"].values():
            by_name = {a["name"]: a for a in atoms}
            head = by_name.get("P") or by_name.get("O3")
            if head:
                color = (185, 159, 97) if head["xyz"][2] >= visual_midplane else (156, 139, 180)
                primitives.append(("sphere", head["xyz"], None, 1.1, color))
                for chain in (2, 3):
                    tail = [by_name[f"C{chain}{i}"] for i in (2, 6, 10, 14, 18, 22) if f"C{chain}{i}" in by_name]
                    for a, b in zip([head] + tail, tail):
                        if math.dist(a["xyz"], b["xyz"]) < 9:
                            primitives.append(("line", a["xyz"], b["xyz"], .13, (210, 207, 191)))
    for atom in components:
        color = ELEMENT_COLORS.get(atom["element"], (185, 124, 70))
        primitives.append(("sphere", atom["xyz"], None, .8 if atom["element"] == "C" else .9, color))
    by_index = {a["index"]: a for a in components}
    for a, b in scene["component_bonds"]:
        primitives.append(("line", by_index[a]["xyz"], by_index[b]["xyz"], .25, (156, 113, 70)))
    return primitives


def _render(scene, output, view, width):
    np, Image, ImageDraw, ImageFont, PngImagePlugin = _dependencies()
    height = int(width * .72)
    scale_factor = 2
    canvas = Image.new("RGBA", (width * scale_factor, height * scale_factor), (249, 250, 252, 255))
    draw = ImageDraw.Draw(canvas)
    angle = math.radians(30)
    depth = np.asarray((math.sin(angle), math.cos(angle), .12) if view != "top" else (0., 0., 1.))
    depth /= np.linalg.norm(depth)
    right = np.asarray((depth[1], -depth[0], 0.)) if view != "top" else np.asarray((1., 0., 0.))
    right /= np.linalg.norm(right)
    up = np.cross(right, depth)
    if view == "top":
        up = -up
    matrix = np.column_stack((right, up, depth))
    primitives = _primitives(scene, close=view == "ligand")
    if not primitives:
        raise ToolError("Preview needs protein C-alpha atoms, visible components or membrane headgroups.")
    points = np.asarray([p[i] for p in primitives for i in (1, 2) if p[i] is not None]) @ matrix
    low, high = points.min(axis=0), points.max(axis=0)
    spans = np.maximum(high - low, 8)
    factor = min(width * .85 / spans[0], height * .70 / spans[1]) * scale_factor
    midpoint = (low + high) / 2
    origin = np.asarray((width * .5, height * .50)) * scale_factor
    def project(point):
        q = np.asarray(point) @ matrix
        xy = origin + (q[:2] - midpoint[:2]) * factor * np.asarray((1, -1))
        return tuple(xy), q[2]
    projected = []
    for kind, a, b, radius, color in primitives:
        xy, z = project(a)
        end, other_z = project(b) if b is not None else (None, z)
        projected.append(((z + other_z) / 2, kind, xy, end, max(radius * factor, 1), color))
    sprites = {}
    for _, kind, a, b, radius, color in sorted(projected, key=lambda p: p[0]):
        if kind == "line":
            draw.line((a, b), fill=color + (255,), width=max(1, round(radius * 2)))
            continue
        radius = max(1, round(radius))
        key = radius, color
        if key not in sprites:
            size = 2 * radius + 2
            grid_y, grid_x = np.mgrid[-radius:radius + 2, -radius:radius + 2]
            norm = (grid_x / radius) ** 2 + (grid_y / radius) ** 2
            mask = norm <= 1
            light = np.clip(.65 + .35 * np.sqrt(np.maximum(0, 1 - norm)) - .12 * grid_x / radius - .12 * grid_y / radius, .4, 1.12)
            pixels = np.zeros((size, size, 4), dtype=np.uint8)
            pixels[:, :, :3] = np.clip(np.asarray(color)[None, None, :] * light[:, :, None], 0, 255)
            pixels[:, :, 3] = mask * 255
            sprites[key] = Image.fromarray(pixels)
        sprite = sprites[key]
        canvas.alpha_composite(sprite, (round(a[0]) - radius, round(a[1]) - radius))
    draw = ImageDraw.Draw(canvas)
    title = "Membrane system" if scene["membranes"] else "Input structure"
    label = {"side": title + " - side view", "top": title + " - top view", "ligand": "Component neighborhood"}[view]
    draw.text((24 * scale_factor, 16 * scale_factor), label, font=_font(ImageFont, min(25, width // 28) * scale_factor), fill=(49, 62, 75))
    legend = "Protein: C-alpha trace | Components: heavy atoms | Lipids: headgroups / coarse tail traces"
    if width < 1000:
        for index, line in enumerate(("Protein: C-alpha | Components: heavy atoms", "Lipids: heads / coarse tails; water/ions hidden", "Actual coordinates; not equilibrium proof.")):
            draw.text((16 * scale_factor, (height - 52 + index * 15) * scale_factor), line,
                      font=_font(ImageFont, min(12, width // 36) * scale_factor), fill=(75, 83, 93))
    else:
        draw.text((24 * scale_factor, (height - 42) * scale_factor), legend, font=_font(ImageFont, 13 * scale_factor), fill=(75, 83, 93))
        draw.text((24 * scale_factor, (height - 23) * scale_factor), "Actual coordinates; water/ions hidden. Preview does not certify geometry or equilibration.",
                  font=_font(ImageFont, 12 * scale_factor), fill=(103, 110, 119))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("SourceSHA256", scene["source_sha256"])
    metadata.add_text("Representation", "Coordinate-driven orthographic projection; C-alpha trace and selected atoms")
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    canvas.convert("RGB").resize((width, height), resampling).save(output, pnginfo=metadata)
    return {"file": str(output), "view": view, "width": width, "height": height,
            "sha256": _digest(output), "primitives": len(primitives)}


def render_model(pdb, out, *, ligand_resnames=("LIG",), width=1400, views=("side", "top", "ligand"), scope="coordinate_file", copy_model=True):
    if type(copy_model) is not bool:
        raise ToolError(key="preview.copy_mode_invalid", next_step="charmm-gui-cli preview -h")
    if type(width) is not int or not 400 <= width <= 4096:
        raise ToolError("Preview width must be an integer from 400 to 4096 pixels.")
    if not views or len(set(views)) != len(views) or any(v not in ("side", "top", "ligand") for v in views):
        raise ToolError("Preview views must be unique choices from side, top, ligand.")
    _dependencies()
    scene = read_scene(pdb, ligand_resnames)
    scene["scope"] = scope
    destination = Path(out).expanduser().resolve()
    try:
        destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError:
        raise ToolError("Preview output already exists; choose a new directory. Nothing was overwritten.") from None
    model = destination / "model.pdb" if copy_model else Path(scene["source"])
    if copy_model:
        shutil.copyfile(scene["source"], model)
    images = [_render(scene, destination / (view + ".png"), view, width) for view in views
              if view != "ligand" or scene["components"]]
    result = {"status": "rendered", "source": scene["source"], "source_sha256": scene["source_sha256"],
              "model": str(model), "images": images, "model_copied": copy_model,
              "total_atoms": scene["total_atoms"], "hidden_atoms": scene["hidden"],
              "visible_components": dict(Counter(a["resname"] for a in scene["components"])),
              "coordinate_transform": "camera projection only; model.pdb unchanged" if copy_model else "camera projection only; source coordinates unchanged",
              "scope": scope,
              "scientific_correctness_verified": False}
    atomic_json(destination / "preview.json", result)
    return result


def new_preview_path(kind="previews"):
    from datetime import datetime, timezone
    import os
    import uuid
    if kind == "checks":
        return Path.cwd() / "charmm-gui-checks" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:10])
    return Path.cwd() / "charmm-gui-previews" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:10])
