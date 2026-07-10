"""Runtime for compact, phase-conditioned Galbot arm skills."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def _features(phase: float, harmonics: int):
    values = [1.0, phase, phase * phase]
    for index in range(1, harmonics + 1):
        values.extend((math.sin(math.pi * index * phase), math.cos(math.pi * index * phase)))
    return values


def list_skills(root: Path):
    skills = []
    if not root.exists():
        return skills
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["path"] = str(manifest_path.parent)
            skills.append(manifest)
        except (OSError, json.JSONDecodeError):
            continue
    return skills


def load_skill(root: Path, name: str):
    if not name or "/" in name or ".." in name:
        raise ValueError("技能名称无效")
    directory = root / name
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "galbot-arm-skill/v1" or manifest.get("name") != name:
        raise ValueError("技能 manifest 无效")
    model_path = directory / str(manifest.get("model") or "model.json")
    model_bytes = model_path.read_bytes()
    expected = str(manifest.get("model_sha256") or "")
    if not expected or hashlib.sha256(model_bytes).hexdigest() != expected:
        raise ValueError("技能模型摘要校验失败")
    model = json.loads(model_bytes)
    if model.get("format") != "galbot-phase-joint-policy/v1":
        raise ValueError("不支持的技能模型格式")
    return manifest, model


def predict(model: dict[str, Any], phase: float):
    phase = min(1.0, max(0.0, float(phase)))
    feature = _features(phase, int(model["harmonics"]))
    weights = model["weights"]
    if len(weights) != len(feature):
        raise ValueError("模型特征维度不匹配")
    outputs = len(weights[0])
    values = [sum(feature[row] * float(weights[row][column]) for row in range(len(feature))) for column in range(outputs)]
    return values


def trajectory(manifest: dict[str, Any], model: dict[str, Any], steps: int):
    steps = max(2, min(200, int(steps)))
    bounds = manifest.get("output_bounds") or []
    result = []
    for index in range(steps):
        values = predict(model, index / (steps - 1))
        if len(values) != 14 or len(bounds) != 14:
            raise ValueError("双臂技能必须输出 14 个关节并提供安全边界")
        for joint, (value, bound) in enumerate(zip(values, bounds), start=1):
            if not float(bound[0]) <= value <= float(bound[1]):
                raise ValueError(f"模型输出超出示教安全包络: joint={joint}, value={value:.4f}")
        result.append(values)
    return result
