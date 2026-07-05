"""Non-destructive edit stack: ops, validation, folding, serialization, clipboard.

The stack is the unit of copy/paste and the source of truth for every render.
Source JPEGs are never touched. See PLAN.md "Edit model".
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

STACK_VERSION = 1

# Snapseed-style tune params. "exposure" is shown as Brightness in the UI;
# "ambiance" is Snapseed's Ambiance/Atmosphere: +1 opens shadows and boosts
# color ("colorful and happy"), -1 goes contrasty and desaturated. "hue" has
# no slider anymore but stays valid for existing stacks.
TUNE_KEYS = ("exposure", "contrast", "saturation", "ambiance",
             "highlights", "shadows", "temperature", "tint", "hue")
OP_TYPES = ("rotate", "crop", "tune")


class StackError(ValueError):
    pass


@dataclass
class Op:
    op: str
    params: dict
    enabled: bool = True

    def to_dict(self) -> dict:
        d = {"op": self.op, "params": self.params}
        if not self.enabled:
            d["enabled"] = False
        return d

    @staticmethod
    def from_dict(d: dict) -> "Op":
        op = Op(d["op"], dict(d.get("params", {})), bool(d.get("enabled", True)))
        validate_op(op)
        return op

    def summary(self) -> str:
        if self.op == "rotate":
            d = float(self.params["degrees"]) % 360
            if d > 180:
                d -= 360
            return f"Rotate {d:g}°"
        if self.op == "crop":
            x, y, w, h = self.params["rect"]
            return f"Crop {w * 100:.0f}×{h * 100:.0f}%"
        parts = [f"{k[:3]} {round(v * 100):+d}" for k, v in self.params.items() if v]
        return "Tune " + (", ".join(parts) if parts else "(neutral)")


def validate_op(op: Op) -> None:
    if op.op == "rotate":
        deg = op.params.get("degrees")
        if not isinstance(deg, (int, float)) or not -360 <= deg <= 360:
            raise StackError(f"rotate degrees must be in [-360, 360], got {deg!r}")
    elif op.op == "crop":
        rect = op.params.get("rect")
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                or not all(isinstance(v, (int, float)) for v in rect)):
            raise StackError(f"crop rect must be [x, y, w, h], got {rect!r}")
        x, y, w, h = rect
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1
                and x + w <= 1.0001 and y + h <= 1.0001):
            raise StackError(f"crop rect out of bounds: {rect!r}")
    elif op.op == "tune":
        for k, v in op.params.items():
            if k not in TUNE_KEYS:
                raise StackError(f"unknown tune param {k!r}")
            if not isinstance(v, (int, float)) or not -1 <= v <= 1:
                raise StackError(f"tune param {k}={v!r} outside [-1, 1]")
    else:
        raise StackError(f"unknown op {op.op!r}")


@dataclass
class FoldedTune:
    """All enabled tune ops folded to one set of render inputs: additive
    params sum, contrast / saturation fold as (1 + p) factors — so the
    preview shader is a single pass regardless of stack depth."""
    exposure: float = 0.0          # sums, in [-1, 1] units (render maps to EV)
    temperature: float = 0.0       # sums
    tint: float = 0.0              # sums
    hue: float = 0.0               # sums
    ambiance: float = 0.0          # sums
    highlights: float = 0.0        # sums
    shadows: float = 0.0           # sums
    contrast_factor: float = 1.0   # multiplies
    saturation_factor: float = 1.0 # multiplies

    def is_identity(self) -> bool:
        return (self.exposure == 0 and self.temperature == 0 and self.tint == 0
                and self.hue == 0
                and self.ambiance == 0 and self.highlights == 0
                and self.shadows == 0
                and self.contrast_factor == 1.0 and self.saturation_factor == 1.0)


@dataclass
class Geometry:
    """Net geometric transform: rotate source by cw_degrees (exact 90°
    steps), then by `fine` degrees CW in [-45, 45] with an automatic crop
    to the largest aspect-preserving inscribed rect (render.apply_geometry
    and the viewer UV chain both implement it), then crop rect (normalized
    [x, y, w, h]) in that visible frame."""
    cw_degrees: int = 0
    rect: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    fine: float = 0.0

    def is_identity(self) -> bool:
        return (self.cw_degrees % 360 == 0 and self.fine == 0.0
                and self.rect == (0.0, 0.0, 1.0, 1.0))


def _rotate_rect(rect, cw_degrees):
    """Transform a normalized rect through a 90°-multiple CW rotation of its frame."""
    x, y, w, h = rect
    d = cw_degrees % 360
    if d == 90:
        return (1.0 - (y + h), x, h, w)
    if d == 180:
        return (1.0 - (x + w), 1.0 - (y + h), w, h)
    if d == 270:
        return (y, 1.0 - (x + w), h, w)
    return rect


class EditStack:
    def __init__(self, ops: list[Op] | None = None):
        self.ops: list[Op] = ops or []

    # -- serialization ------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps({"version": STACK_VERSION,
                           "stack": [op.to_dict() for op in self.ops]})

    @staticmethod
    def from_json(text: str | None) -> "EditStack":
        if not text:
            return EditStack()
        try:
            doc = json.loads(text)
            if doc.get("version") != STACK_VERSION:
                raise StackError(f"unsupported stack version {doc.get('version')!r}")
            return EditStack([Op.from_dict(d) for d in doc.get("stack", [])])
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise StackError(f"malformed stack JSON: {e}") from e

    def clone(self) -> "EditStack":
        return EditStack(copy.deepcopy(self.ops))

    # -- editing ------------------------------------------------------------

    def append(self, op: Op) -> None:
        validate_op(op)
        self.ops.append(op)

    def add_rotation(self, cw_degrees: float) -> None:
        """Append a rotate, merging with a trailing rotate op."""
        if self.ops and self.ops[-1].op == "rotate" and self.ops[-1].enabled:
            total = (self.ops[-1].params["degrees"] + cw_degrees) % 360
            if abs(total) < 1e-9:
                self.ops.pop()
            else:
                self.ops[-1].params["degrees"] = total
        else:
            self.append(Op("rotate", {"degrees": cw_degrees % 360}))

    def total_rotation(self) -> float:
        """Folded rotation, normalized to (-180, 180] — the slider value."""
        t = sum(float(op.params["degrees"]) for op in self.enabled_ops()
                if op.op == "rotate") % 360
        return t - 360 if t > 180 else t

    def set_rotation(self, degrees: float) -> None:
        """Set the *total* rotation (the slider path): the trailing rotate
        op is adjusted so the fold lands on `degrees`; 0 removes it."""
        tail = (self.ops[-1] if self.ops and self.ops[-1].op == "rotate"
                and self.ops[-1].enabled else None)
        others = self.total_rotation() - (float(tail.params["degrees"])
                                          if tail else 0.0)
        v = (degrees - others) % 360
        if v > 180:
            v -= 360
        if abs(v) < 1e-9:
            if tail is not None:
                self.ops.pop()
        elif tail is not None:
            tail.params["degrees"] = v
        else:
            self.append(Op("rotate", {"degrees": v}))

    def last_tune(self) -> Op | None:
        for op in reversed(self.ops):
            if op.op == "tune" and op.enabled:
                return op
        return None

    def is_empty(self) -> bool:
        return not any(op.enabled for op in self.ops)

    def has_edits(self) -> bool:
        return not self.is_empty() and not (
            self.folded_tune().is_identity() and self.geometry().is_identity())

    def enabled_ops(self) -> list[Op]:
        return [op for op in self.ops if op.enabled]

    def only_rotations(self) -> bool:
        """True if every enabled op is a rotate (or a no-op tune) — the
        lossless-export special case."""
        for op in self.enabled_ops():
            if op.op == "crop":
                return False
            if op.op == "tune" and any(op.params.get(k) for k in TUNE_KEYS):
                return False
        return True

    # -- folding ------------------------------------------------------------

    def folded_tune(self) -> FoldedTune:
        f = FoldedTune()
        for op in self.enabled_ops():
            if op.op != "tune":
                continue
            p = op.params
            f.exposure += p.get("exposure", 0.0)
            f.temperature += p.get("temperature", 0.0)
            f.tint += p.get("tint", 0.0)
            f.hue += p.get("hue", 0.0)
            f.ambiance += p.get("ambiance", 0.0)
            f.highlights += p.get("highlights", 0.0)
            f.shadows += p.get("shadows", 0.0)
            f.contrast_factor *= 1.0 + p.get("contrast", 0.0)
            f.saturation_factor *= 1.0 + p.get("saturation", 0.0)
        return f

    def geometry(self) -> Geometry:
        """Fold to Geometry: total rotation decomposes into the nearest 90°
        multiple (exact, lossless-able) + a fine residual in [-45, 45].
        Crop rects transform through each rotate op's 90° part only — the
        fine part re-frames under the crop, like other editors."""
        total = 0.0
        rect = (0.0, 0.0, 1.0, 1.0)
        for op in self.enabled_ops():
            if op.op == "rotate":
                d = float(op.params["degrees"])
                total += d
                rect = _rotate_rect(rect, round(d / 90.0) * 90)
            elif op.op == "crop":
                cx, cy, cw, ch = op.params["rect"]
                x, y, w, h = rect
                rect = (x + cx * w, y + cy * h, cw * w, ch * h)
        theta = total % 360
        if theta > 180:
            theta -= 360
        k90 = round(theta / 90.0) * 90
        fine = theta - k90
        if abs(fine) < 1e-9:
            fine = 0.0
        return Geometry(int(k90) % 360, rect, fine)


# --------------------------------------------------------------------------
# Edit clipboard: holds a copied stack (optionally filtered by op type).
# --------------------------------------------------------------------------

class EditClipboard:
    def __init__(self):
        self._ops: list[Op] | None = None

    def copy(self, stack: EditStack, op_filter: set[str] | None = None) -> int:
        ops = [copy.deepcopy(op) for op in stack.ops
               if op_filter is None or op.op in op_filter]
        self._ops = ops
        return len(ops)

    def is_empty(self) -> bool:
        return not self._ops

    def paste_replace(self, _target: EditStack) -> EditStack:
        return EditStack(copy.deepcopy(self._ops or []))

    def paste_append(self, target: EditStack) -> EditStack:
        merged = target.clone()
        merged.ops.extend(copy.deepcopy(self._ops or []))
        return merged


# --------------------------------------------------------------------------
# Per-image undo history: snapshots of serialized stacks. Undo/redo is just
# a cursor move; bulk pastes push one snapshot per target image.
# --------------------------------------------------------------------------

class StackHistory:
    def __init__(self, limit: int = 100):
        self._hist: dict[int, tuple[list[str], int]] = {}  # id -> (snapshots, cursor)
        self._limit = limit

    def record(self, file_id: int, stack: EditStack) -> None:
        snaps, cur = self._hist.get(file_id, ([EditStack().to_json()], 0))
        new = stack.to_json()
        if snaps[cur] == new:
            return
        snaps = snaps[:cur + 1] + [new]
        if len(snaps) > self._limit:
            snaps = snaps[-self._limit:]
        self._hist[file_id] = (snaps, len(snaps) - 1)

    def seed(self, file_id: int, stack: EditStack) -> None:
        """Ensure a baseline snapshot exists without truncating redo state."""
        if file_id not in self._hist:
            self._hist[file_id] = ([stack.to_json()], 0)

    def undo(self, file_id: int) -> EditStack | None:
        snaps, cur = self._hist.get(file_id, (None, 0))
        if not snaps or cur == 0:
            return None
        self._hist[file_id] = (snaps, cur - 1)
        return EditStack.from_json(snaps[cur - 1])

    def redo(self, file_id: int) -> EditStack | None:
        snaps, cur = self._hist.get(file_id, (None, 0))
        if not snaps or cur >= len(snaps) - 1:
            return None
        self._hist[file_id] = (snaps, cur + 1)
        return EditStack.from_json(snaps[cur + 1])
