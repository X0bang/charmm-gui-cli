"""Offline comparison gates for research conversion evidence, not a converter.

Inputs must be GROMACS-preprocessed topology text. No files, subprocesses or
network are accessed, and no production modeling route uses these gates yet.
The return value is locale-independent evidence, not terminal prose.
"""

import math
import re


_TYPE = re.compile(r"[A-Za-z0-9_*+\-]+\Z")
_MAX_TEXT = 32 * 1024 * 1024
_MAX_MAPS = 128
_MAX_CELLS = 65536


def _cmap_records(text):
    if not isinstance(text, str) or len(text) > _MAX_TEXT:
        return None, {"code": "invalid_text"}
    section, pending, records = "", [], {}
    for number, raw in enumerate(text.splitlines(), 1):
        code = raw.split(";", 1)[0].strip()
        if not code:
            continue
        if code.startswith("#"):
            return None, {"code": "unresolved_preprocessor", "line": number}
        if code.startswith("["):
            if pending:
                return None, {"code": "unfinished_record", "line": number}
            match = re.fullmatch(r"\[\s*([A-Za-z0-9_]+)\s*\]", code)
            if match is None:
                return None, {"code": "invalid_section", "line": number}
            section = match[1].lower()
            continue
        if section != "cmaptypes":
            continue
        continued = code.endswith("\\")
        pending.extend(code.rstrip("\\").split())
        if len(pending) > _MAX_CELLS + 8:
            return None, {"code": "oversized_grid", "line": number}
        if continued:
            continue
        if (len(pending) < 8 or not all(_TYPE.fullmatch(v) for v in pending[:5])
                or pending[5] != "1" or not all(re.fullmatch(r"[0-9]{1,5}", v) for v in pending[6:8])):
            return None, {"code": "invalid_record", "line": number}
        dimensions = tuple(int(v) for v in pending[6:8])
        cells = dimensions[0] * dimensions[1]
        if not all(dimensions) or cells > _MAX_CELLS or len(pending) != cells + 8:
            return None, {"code": "invalid_grid_size", "line": number}
        try:
            values = tuple(float(v) for v in pending[8:])
        except (ValueError, OverflowError):
            return None, {"code": "invalid_grid_value", "line": number}
        if not all(math.isfinite(v) for v in values):
            return None, {"code": "nonfinite_grid_value", "line": number}
        key = tuple(pending[:5])
        if key in records:
            return None, {"code": "duplicate_map", "line": number}
        if len(records) >= _MAX_MAPS:
            return None, {"code": "too_many_maps", "line": number}
        records[key] = (dimensions, values)
        pending = []
    if pending:
        return None, {"code": "unfinished_record"}
    if not records:
        return None, {"code": "no_cmap_parameters"}
    return records, None


def compare_cmap_parameters(reference, candidate, *, relative_tolerance=1e-7, absolute_tolerance=1e-8):
    """Compare every full-precision CMAP cell at its original ordered position.

    Never sort grid values, transpose surfaces, reverse type keys or accept
    matching sampled energies in place of matching parameters. Error locations
    are bounded numeric/type metadata; raw topology lines are never reflected.
    This proves only CMAP equality, not topology, restraint or energy equivalence.
    """
    if (not isinstance(relative_tolerance, (int, float)) or isinstance(relative_tolerance, bool)
            or not isinstance(absolute_tolerance, (int, float)) or isinstance(absolute_tolerance, bool)
            or relative_tolerance < 0 or absolute_tolerance < 0):
        return {"passed": False, "error": {"code": "invalid_tolerance"}}
    try:
        relative_tolerance, absolute_tolerance = float(relative_tolerance), float(absolute_tolerance)
    except (ValueError, OverflowError):
        return {"passed": False, "error": {"code": "invalid_tolerance"}}
    if not math.isfinite(relative_tolerance) or not math.isfinite(absolute_tolerance):
        return {"passed": False, "error": {"code": "invalid_tolerance"}}
    a, error = _cmap_records(reference)
    if error:
        return {"passed": False, "error": {"source": "reference", **error}}
    b, error = _cmap_records(candidate)
    if error:
        return {"passed": False, "error": {"source": "candidate", **error}}
    missing, extra = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    mismatches, checked, max_absolute, examples = 0, 0, 0.0, []
    difference_overflow = False
    for key in sorted(set(a) & set(b)):
        dimensions, values = a[key]
        other_dimensions, other_values = b[key]
        if dimensions != other_dimensions:
            mismatches += 1
            if len(examples) < 8:
                examples.append({"types": list(key), "code": "grid_dimensions_differ"})
            continue
        for index, (x, y) in enumerate(zip(values, other_values)):
            checked += 1
            difference = abs(x - y)
            if math.isfinite(difference):
                max_absolute = max(max_absolute, difference)
            else:
                difference_overflow = True
            if not math.isclose(x, y, rel_tol=relative_tolerance, abs_tol=absolute_tolerance):
                mismatches += 1
                if len(examples) < 8:
                    examples.append({"types": list(key), "code": "grid_value_differs",
                                     "row": index // dimensions[1], "column": index % dimensions[1]})
    return {"passed": not (missing or extra or mismatches), "scope": "ordered_cmap_parameters_only",
            "reference_map_count": len(a), "candidate_map_count": len(b),
            "checked_cells": checked, "mismatches": mismatches,
            "max_absolute_difference": None if difference_overflow else max_absolute,
            **({"difference_overflow": True} if difference_overflow else {}),
            "missing_map_count": len(missing), "extra_map_count": len(extra),
            "missing_maps": [list(v) for v in missing[:8]], "extra_maps": [list(v) for v in extra[:8]],
            "examples": examples, "relative_tolerance": relative_tolerance,
            "absolute_tolerance": absolute_tolerance}
