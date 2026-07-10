#!/usr/bin/env python3
"""
Galbot VLA Tree control platform.

Run:
  python server.py
Open:
  http://localhost:7860
"""

from __future__ import annotations

import json
import hmac
import hashlib
import math
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from collections import deque
from typing import Any, Optional

from flask import Flask, jsonify, request, send_file, send_from_directory, Response

# Galbot robot images install SDK modules outside the active conda environment.
for _sdk_path in (
    "/data/galbot/lib",
    "/data/galbot/lib/python3/site-packages",
    "/data/galbot/lib/python3.8.10",
    "/data/galbot/lib/python3.8.10/site-packages",
):
    if _sdk_path not in sys.path:
        sys.path.insert(0, _sdk_path)

import legacy_backend as _legacy_backend
try:
    from skill_runtime import list_skills as _list_arm_skills
    from skill_runtime import load_skill as _load_arm_skill
    from skill_runtime import trajectory as _arm_skill_trajectory
    SKILL_RUNTIME_OK = True
    SKILL_RUNTIME_ERROR = ""
except ImportError as exc:
    SKILL_RUNTIME_OK = False
    SKILL_RUNTIME_ERROR = str(exc)

    def _list_arm_skills(_root):
        return []

    def _load_arm_skill(_root, _name):
        raise RuntimeError(f"skill_runtime 不可用: {SKILL_RUNTIME_ERROR}")

    def _arm_skill_trajectory(_manifest, _model, _steps):
        raise RuntimeError(f"skill_runtime 不可用: {SKILL_RUNTIME_ERROR}")


APP_DIR = Path(__file__).resolve().parent
VLA_TREE_DIR = APP_DIR.parent
BAAI_DIR = VLA_TREE_DIR.parent
CONFIG_DIR = VLA_TREE_DIR / "config"
LOCATION_MAP_PATH = Path(os.environ.get("GALBOT_LOCATION_MAP", CONFIG_DIR / "location_map.json"))
DEFAULT_PCD_PATH = Path(os.environ.get("GALBOT_GLOBAL_MAP", "/var/maps/cur/global_cloud_cleaned.pcd"))
MAIN_CAMERA_SNAPSHOT = os.environ.get("GALBOT_MAIN_CAMERA_SNAPSHOT", "")
ROBOT_ID = os.environ.get("GALBOT_ROBOT_ID", f"robot-{uuid.uuid4().hex[:6]}")
ROBOT_NAME = os.environ.get("GALBOT_ROBOT_NAME", ROBOT_ID)
ROBOT_CAPABILITIES = [
    item.strip()
    for item in os.environ.get(
        "GALBOT_CAPABILITIES", "navigation,exploration,camera,manipulation"
    ).split(",")
    if item.strip()
]
AGENT_TOKEN = os.environ.get("GALBOT_AGENT_TOKEN", "")
CONTROL_TOKEN = os.environ.get("GALBOT_CONTROL_TOKEN", "")
SIMULATION_MODE = os.environ.get("GALBOT_SIMULATION", "0") == "1"
COORDINATOR_URL = os.environ.get("GALBOT_COORDINATOR_URL", "").rstrip("/")
MAX_PCD_BYTES = int(os.environ.get("GALBOT_MAX_PCD_BYTES", str(128 * 1024 * 1024)))
MAX_PCD_POINTS = int(os.environ.get("GALBOT_MAX_PCD_POINTS", "20000000"))
SKILL_DIR = Path(os.environ.get("GALBOT_SKILL_DIR", APP_DIR / "skills"))
MAPPING_SERVER_BIN = Path(os.environ.get("GALBOT_MAPPING_SERVER", "/data/galbot/bin/mapping_server"))
ENGINE_TOOLS_BIN = Path(os.environ.get("GALBOT_ENGINE_TOOLS", "/data/galbot/bin/engine_tools"))
MAPPING_SAVE_PATH = Path(os.environ.get("GALBOT_MAPPING_SAVE_PATH", "/var/maps/room1102"))
MAP_MIN_HEIGHT = float(os.environ.get("GALBOT_MAP_MIN_HEIGHT", "-0.10"))
MAP_MAX_HEIGHT = float(os.environ.get("GALBOT_MAP_MAX_HEIGHT", "0.50"))
STOPPABLE_PROCESSES = {
    item.strip()
    for item in os.environ.get(
        "GALBOT_STOPPABLE_PROCESSES", "mapping_server,engine_tools"
    ).split(",")
    if item.strip()
}

sys.path.insert(0, str(VLA_TREE_DIR))
sys.path.insert(0, str(BAAI_DIR))

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_PCD_BYTES

NAV_OK = False
NAV_IMPORT_ERROR = ""
NavigationResult = None
_nav = None
_nav_lock = threading.Lock()
_location_lock = threading.Lock()
_fleet_lock = threading.Lock()
_fleet_robots: dict[str, dict[str, Any]] = {}
_fleet_messages: list[dict[str, Any]] = []
_fleet_tasks: list[dict[str, Any]] = []
_agent_lock = threading.Lock()
_agent_task: dict[str, Any] | None = None
_agent_task_history: list[dict[str, Any]] = []
_agent_messages: list[dict[str, Any]] = []
_skill_lock = threading.RLock()
_skill_state: dict[str, Any] | None = None
_agent_started_at = time.time()
_mapping_lock = threading.RLock()
_mapping_process: subprocess.Popen | None = None
_mapping_reader: threading.Thread | None = None
_mapping_state: dict[str, Any] = {
    "status": "idle",
    "pid": None,
    "started_at": None,
    "keyframes": 0,
    "time_delay": None,
    "pose": None,
    "last_error": None,
}
_mapping_logs: deque[str] = deque(maxlen=160)
_teleop_generation = 0
_nav_nudge_lock = threading.Lock()
_nav_nudge_active = False
_cpu_sample_lock = threading.Lock()
_cpu_sample: tuple[int, int] | None = None


class SpatialOccupancyMap:
    """Incremental voxel cloud + 2D occupancy/frontier map."""

    def __init__(self, resolution: float = 0.20, voxel: float = 0.08):
        self.resolution = resolution
        self.voxel = voxel
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with getattr(self, "lock", threading.RLock()):
            self.voxels: dict[tuple[int, int, int], list[float]] = {}
            self.free: set[tuple[int, int]] = set()
            self.occupied: set[tuple[int, int]] = set()
            self.occupancy_score: dict[tuple[int, int], int] = {}
            self.scans = 0
            self.updated_at = None
            self.pose = None
            self.pose_resets = 0
            self.erase_masks: list[tuple[float, float, float]] = []

    @staticmethod
    def _bresenham(start, end):
        x0, y0 = start
        x1, y1 = end
        dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
        dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
        error = dx + dy
        cells = []
        while True:
            cells.append((x0, y0))
            if x0 == x1 and y0 == y1:
                break
            twice = 2 * error
            if twice >= dy:
                error += dy
                x0 += sx
            if twice <= dx:
                error += dx
                y0 += sy
        return cells

    @staticmethod
    def _hull(points):
        points = sorted(set(points))
        if len(points) <= 2:
            return points

        def cross(origin, a, b):
            return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])

        lower = []
        for point in points:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
                lower.pop()
            lower.append(point)
        upper = []
        for point in reversed(points):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
                upper.pop()
            upper.append(point)
        return lower[:-1] + upper[:-1]

    def _cell(self, x: float, y: float):
        return (math.floor(x / self.resolution), math.floor(y / self.resolution))

    def _world(self, cell):
        return [
            round((cell[0] + 0.5) * self.resolution, 3),
            round((cell[1] + 0.5) * self.resolution, 3),
        ]

    def _edge_segments(self, cells, limit=18000):
        """Return the exposed cell edges instead of an inaccurate convex hull."""
        segments = []
        resolution = self.resolution
        for x, y in cells:
            x0, y0 = x * resolution, y * resolution
            x1, y1 = x0 + resolution, y0 + resolution
            for neighbor, start, end in (
                ((x - 1, y), (x0, y0), (x0, y1)),
                ((x + 1, y), (x1, y0), (x1, y1)),
                ((x, y - 1), (x0, y0), (x1, y0)),
                ((x, y + 1), (x0, y1), (x1, y1)),
            ):
                if neighbor not in cells:
                    segments.append([
                        [round(start[0], 3), round(start[1], 3)],
                        [round(end[0], 3), round(end[1], 3)],
                    ])
        if len(segments) <= limit:
            return segments
        stride = max(1, math.ceil(len(segments) / limit))
        return segments[::stride]

    @staticmethod
    def _stable_components(cells, minimum=2):
        remaining = set(cells)
        stable = set()
        while remaining:
            seed = remaining.pop()
            component = {seed}
            queue = [seed]
            while queue:
                x, y = queue.pop()
                for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.add(neighbor)
                        queue.append(neighbor)
            if len(component) >= minimum:
                stable.update(component)
        return stable

    @staticmethod
    def _smooth_reachable(cells, blocked):
        cells = set(cells)
        candidates = set()
        for x, y in cells:
            candidates.update(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
        for cell in candidates - cells - blocked:
            x, y = cell
            neighbors = ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            if sum(neighbor in cells for neighbor in neighbors) >= 3:
                cells.add(cell)
        return cells

    def erase(self, circles):
        """Remove occupied cells and point samples inside user-selected circles."""
        valid = []
        for circle in circles[:200]:
            try:
                x = float(circle["x"])
                y = float(circle["y"])
                radius = min(3.0, max(0.05, float(circle["radius"])))
            except (KeyError, TypeError, ValueError):
                continue
            if all(math.isfinite(value) for value in (x, y, radius)):
                valid.append((x, y, radius, radius * radius))
        if not valid:
            return {"cells": 0, "points": 0}

        def inside(x, y):
            return any((x - cx) ** 2 + (y - cy) ** 2 <= radius_sq
                       for cx, cy, _, radius_sq in valid)

        with self.lock:
            self.erase_masks.extend((x, y, radius_sq) for x, y, _, radius_sq in valid)
            self.erase_masks = self.erase_masks[-600:]
            removed_cells = {
                cell for cell in self.occupied
                if inside(*self._world(cell))
            }
            self.occupied.difference_update(removed_cells)
            self.free.update(removed_cells)
            for cell in removed_cells:
                self.occupancy_score.pop(cell, None)
            voxel_keys = [
                key for key, point in self.voxels.items()
                if inside(point[0], point[1])
            ]
            for key in voxel_keys:
                del self.voxels[key]
            self.updated_at = time.time()
        return {"cells": len(removed_cells), "points": len(voxel_keys)}

    def add_scan(self, local_points, pose):
        if not pose or len(pose) < 7:
            return
        px, py, pz = float(pose[0]), float(pose[1]), float(pose[2])
        yaw = _quat_to_yaw(pose)
        cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
        valid = []
        z_values = []
        with self.lock:
            erase_masks = list(self.erase_masks)
        for point in local_points:
            try:
                lx, ly = float(point[0]), float(point[1])
                lz = float(point[2]) if len(point) > 2 else 0.0
            except (TypeError, ValueError):
                continue
            distance = math.hypot(lx, ly)
            if not (0.15 <= distance <= 25.0) or not all(math.isfinite(value) for value in (lx, ly, lz)):
                continue
            wx = px + cos_yaw * lx - sin_yaw * ly
            wy = py + sin_yaw * lx + cos_yaw * ly
            wz = pz + lz
            if not MAP_MIN_HEIGHT <= lz <= MAP_MAX_HEIGHT:
                continue
            if any((wx - cx) ** 2 + (wy - cy) ** 2 <= radius_sq
                   for cx, cy, radius_sq in erase_masks):
                continue
            valid.append((wx, wy, wz, distance))
            z_values.append(wz)
        if not valid:
            return
        is_3d = max(z_values) - min(z_values) > 0.15
        stride = max(1, len(valid) // 420)
        origin = self._cell(px, py)
        with self.lock:
            if (
                self.pose
                and self.updated_at
                and time.time() - self.updated_at < 3.0
                and math.hypot(px - self.pose[0], py - self.pose[1]) > 2.0
            ):
                self.voxels.clear()
                self.free.clear()
                self.occupied.clear()
                self.occupancy_score.clear()
                self.pose_resets += 1
            self.pose = [float(value) for value in pose]
            free_observed = set()
            hit_observed = set()
            scan_voxels: dict[tuple[int, int], list[tuple[tuple[int, int, int], list[float]]]] = {}
            for index, (wx, wy, wz, _) in enumerate(valid):
                voxel_key = (
                    round(wx / self.voxel),
                    round(wy / self.voxel),
                    round(wz / self.voxel),
                )
                if index % stride:
                    continue
                endpoint = self._cell(wx, wy)
                ray = self._bresenham(origin, endpoint)
                free_observed.update(ray[:-1])
                obstacle = (not is_3d) or (pz + 0.12 <= wz <= pz + 1.80)
                if obstacle:
                    hit_observed.add(endpoint)
                    scan_voxels.setdefault(endpoint, []).append((voxel_key, [wx, wy, wz]))
                else:
                    free_observed.add(endpoint)
            free_observed.difference_update(hit_observed)
            cleared_cells = set()
            for cell in free_observed:
                score = max(-6, self.occupancy_score.get(cell, 0) - 2)
                self.occupancy_score[cell] = score
                self.free.add(cell)
                if score <= 0:
                    if cell in self.occupied:
                        cleared_cells.add(cell)
                    self.occupied.discard(cell)
            for cell in hit_observed:
                score = min(6, self.occupancy_score.get(cell, 0) + 2)
                self.occupancy_score[cell] = score
                self.free.discard(cell)
                if score >= 4:
                    self.occupied.add(cell)
                    for voxel_key, point in scan_voxels.get(cell, ()):
                        self.voxels[voxel_key] = point
            if cleared_cells:
                self.voxels = {
                    key: point for key, point in self.voxels.items()
                    if self._cell(point[0], point[1]) not in cleared_cells
                }
            if len(self.occupancy_score) > 180000:
                self.occupancy_score = {
                    cell: score for cell, score in self.occupancy_score.items()
                    if score != 0 or cell in self.free or cell in self.occupied
                }
            # Bound memory for long sessions while retaining the newest spatial coverage.
            if len(self.voxels) > 180000:
                self.voxels = dict(list(self.voxels.items())[-150000:])
            self.scans += 1
            self.updated_at = time.time()

    def snapshot(self):
        with self.lock:
            free = set(self.free)
            occupied = set(self.occupied)
            voxels = list(self.voxels.values())
            pose = list(self.pose) if self.pose else None
            scans = self.scans
            updated_at = self.updated_at
            pose_resets = self.pose_resets

        reachable = set()
        path_steps: dict[tuple[int, int], int] = {}
        if pose:
            start = self._cell(pose[0], pose[1])
            queue = deque([(start, 0)])
            while queue and len(reachable) < 120000:
                cell, steps = queue.popleft()
                if cell in reachable or cell not in free:
                    continue
                reachable.add(cell)
                path_steps[cell] = steps
                x, y = cell
                queue.extend((
                    ((x + 1, y), steps + 1),
                    ((x - 1, y), steps + 1),
                    ((x, y + 1), steps + 1),
                    ((x, y - 1), steps + 1),
                ))
        if not reachable:
            reachable = free

        inflated = set(occupied)
        for x, y in list(occupied):
            inflated.update(
                (x + dx, y + dy)
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
            )
        frontier_cells = {
            cell
            for cell in reachable
            if cell not in inflated
            and any(
                neighbor not in free and neighbor not in occupied
                for neighbor in (
                    (cell[0] + 1, cell[1]),
                    (cell[0] - 1, cell[1]),
                    (cell[0], cell[1] + 1),
                    (cell[0], cell[1] - 1),
                )
            )
        }
        clusters = []
        remaining = set(frontier_cells)
        while remaining:
            seed = remaining.pop()
            component = [seed]
            queue = deque([seed])
            while queue:
                x, y = queue.popleft()
                for neighbor in (
                    (x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1),
                    (x + 1, y + 1), (x - 1, y - 1), (x + 1, y - 1), (x - 1, y + 1),
                ):
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.append(neighbor)
                        queue.append(neighbor)
            if len(component) >= 3:
                robot_yaw = _quat_to_yaw(pose) if pose else 0.0

                def candidate_metrics(cell):
                    unknown_gain = 0
                    for dx in range(-5, 6):
                        for dy in range(-5, 6):
                            neighbor = (cell[0] + dx, cell[1] + dy)
                            if neighbor not in free and neighbor not in occupied:
                                unknown_gain += 1
                    clearance_cells = 9
                    for radius in range(1, 9):
                        if any(
                            (cell[0] + dx, cell[1] + dy) in occupied
                            for dx in range(-radius, radius + 1)
                            for dy in (-radius, radius)
                        ) or any(
                            (cell[0] + dx, cell[1] + dy) in occupied
                            for dy in range(-radius + 1, radius)
                            for dx in (-radius, radius)
                        ):
                            clearance_cells = radius
                            break
                    path_cost = path_steps.get(cell, 100000) * self.resolution
                    if pose:
                        world = self._world(cell)
                        target_heading = math.atan2(world[1] - pose[1], world[0] - pose[0])
                        heading_cost = abs(math.atan2(
                            math.sin(target_heading - robot_yaw),
                            math.cos(target_heading - robot_yaw),
                        ))
                    else:
                        heading_cost = 0.0
                    clearance = clearance_cells * self.resolution
                    utility = (
                        2.4 * math.sqrt(max(1, unknown_gain))
                        + 0.8 * min(clearance, 2.0)
                        + 0.25 * math.sqrt(len(component))
                        - 0.65 * path_cost
                        - 0.25 * heading_cost
                    )
                    return utility, unknown_gain, clearance, path_cost, heading_cost

                viable = [
                    cell
                    for cell in component
                    if path_steps.get(cell, 0) * self.resolution >= 0.6
                ]
                target_cell = max(viable or component, key=lambda cell: candidate_metrics(cell)[0])
                utility, information_gain, clearance, path_cost, heading_cost = candidate_metrics(target_cell)
                world = self._world(target_cell)
                clusters.append({
                    "x": world[0],
                    "y": world[1],
                    "cells": len(component),
                    "distance": round(path_cost, 3) if path_cost < 10000 else None,
                    "information_gain": information_gain,
                    "clearance": round(clearance, 3),
                    "heading_cost": round(heading_cost, 3),
                    "utility": round(utility, 3),
                })
        clusters.sort(key=lambda item: -item["utility"])
        stable_occupied = self._stable_components(occupied, minimum=2)
        display_reachable = self._smooth_reachable(reachable, stable_occupied)
        hull = self._hull([tuple(self._world(cell)) for cell in display_reachable])
        frame_segments = self._edge_segments(display_reachable)
        obstacle_segments = self._edge_segments(stable_occupied)
        point_stride = max(1, len(voxels) // 14000)
        free_stride = max(1, len(reachable) // 14000)
        occupied_stride = max(1, len(occupied) // 7000)
        return {
            "resolution": self.resolution,
            "height_range": [MAP_MIN_HEIGHT, MAP_MAX_HEIGHT],
            "scans": scans,
            "updated_at": updated_at,
            "pose_resets": pose_resets,
            "pose": pose,
            "points": [[round(v, 3) for v in point] for point in voxels[::point_stride]],
            "free_cells": [self._world(cell) for cell in list(reachable)[::free_stride]],
            "occupied_cells": [self._world(cell) for cell in list(stable_occupied)[::occupied_stride]],
            "frontiers": clusters[:40],
            "boundary": hull,
            "frame_segments": frame_segments,
            "obstacle_segments": obstacle_segments,
            "counts": {
                "voxels": len(voxels),
                "free": len(reachable),
                "occupied": len(occupied),
                "stable_occupied": len(stable_occupied),
                "frontier": len(frontier_cells),
                "frontier_clusters": len(clusters),
            },
        }


_spatial_map = SpatialOccupancyMap()

try:
    from navigation_interface import get_navigation_interface, navigate_to_pose, stop_navigation
    from navigation_interface import NavigationResult as _NavigationResult

    NavigationResult = _NavigationResult
    NAV_OK = True
except Exception as exc:  # noqa: BLE001 - platform should still run without robot deps.
    NAV_IMPORT_ERROR = str(exc)


def _json_error(msg: str, **extra: Any):
    payload = {"ok": False, "msg": msg}
    payload.update(extra)
    return jsonify(payload)


def _bearer_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.headers.get("X-Galbot-Token", "")


@app.before_request
def require_control_auth():
    path = request.path
    if path.startswith(("/api/agent/", "/api/fleet/")):
        expected = AGENT_TOKEN
    elif path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        expected = CONTROL_TOKEN
    else:
        return None
    if expected and not hmac.compare_digest(_bearer_token(), expected):
        return jsonify({"ok": False, "msg": "未授权", "code": "UNAUTHORIZED"}), 401
    return None


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _get_nav():
    global _nav
    if not NAV_OK:
        return None
    with _nav_lock:
        if _nav is None:
            _nav = get_navigation_interface()
        return _nav


def _pose_list(value):
    if value is None:
        return None
    try:
        return [float(x) for x in list(value)]
    except Exception:
        return None


def _yaw_to_quat(yaw: float):
    return [0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)]


def _quat_to_yaw(pose):
    if not pose or len(pose) < 7:
        return 0.0
    qz, qw = float(pose[5]), float(pose[6])
    return math.atan2(2.0 * qw * qz, 1.0 - 2.0 * qz * qz)


def _load_locations():
    with LOCATION_MAP_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("location_map.json 顶层结构必须是对象")
    return data


def _normalize_robot_url(ip_or_url: str) -> str:
    text = (ip_or_url or "").strip()
    if not text:
        raise ValueError("机器人 IP/URL 不能为空")
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    if ":" not in text.rsplit("/", 1)[0].replace("http://", "").replace("https://", ""):
        text = text.rstrip("/") + ":7861"
    return text.rstrip("/")


def _http_json(url: str, payload: Optional[dict[str, Any]] = None, timeout: float = 1.5):
    data = None
    headers = {"Accept": "application/json"}
    if AGENT_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_TOKEN}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user supplied robot URLs are intended.
        body = resp.read()
    return json.loads(body.decode("utf-8")) if body else {}


def _http_bytes(url: str, timeout: float = 15.0):
    headers = {"Accept": "application/octet-stream"}
    if AGENT_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_TOKEN}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read(MAX_PCD_BYTES + 1)


def _append_fleet_message(kind: str, source: str, target: str, text: str, payload: Optional[dict[str, Any]] = None):
    item = {
        "time": time.time(),
        "kind": kind,
        "source": source,
        "target": target,
        "text": text,
        "payload": payload or {},
    }
    with _fleet_lock:
        _fleet_messages.append(item)
        del _fleet_messages[:-200]
    return item


def _agent_task_snapshot():
    with _agent_lock:
        return dict(_agent_task) if _agent_task else None


def _skill_snapshot():
    with _skill_lock:
        return dict(_skill_state) if _skill_state else None


def _run_arm_skill(name: str, manifest: dict[str, Any], model: dict[str, Any], steps: int):
    global _skill_state
    try:
        frames = _arm_skill_trajectory(manifest, model, steps)
        with _skill_lock:
            _skill_state.update(status="running", total_steps=len(frames), started_at=time.time())
        if SIMULATION_MODE:
            for index, _ in enumerate(frames, start=1):
                time.sleep(0.005)
                with _skill_lock:
                    _skill_state.update(step=index, progress=round(index / len(frames) * 100))
        else:
            if not manifest.get("validated"):
                raise RuntimeError("技能尚未通过真机验证")
            if (manifest.get("safety") or {}).get("requires_collision_check"):
                raise RuntimeError("技能要求碰撞检查；请离线验证轨迹并更新 manifest 后再部署")
            robot, _, _ = _legacy_backend.get_instances()
            if robot is None:
                raise RuntimeError("Galbot SDK/Robot 未就绪")
            for index, frame in enumerate(frames, start=1):
                status = robot.set_joint_positions(
                    frame,
                    list(manifest.get("joint_groups") or ["left_arm", "right_arm"]),
                    [],
                    True,
                    float(manifest.get("max_speed", 0.1)),
                    20.0,
                )
                if _legacy_backend.status_name(status) != "SUCCESS":
                    raise RuntimeError(f"关节轨迹第 {index} 帧失败: {_legacy_backend.status_name(status)}")
                with _skill_lock:
                    _skill_state.update(step=index, progress=round(index / len(frames) * 100))
        with _skill_lock:
            _skill_state.update(status="completed", progress=100, finished_at=time.time())
    except Exception as exc:  # noqa: BLE001
        with _skill_lock:
            if _skill_state and _skill_state.get("name") == name:
                _skill_state.update(status="failed", error=str(exc), finished_at=time.time())


def _mapping_snapshot():
    with _mapping_lock:
        process = _mapping_process
        running = bool(process and process.poll() is None)
        state = dict(_mapping_state)
        state["running"] = running or state["status"] == "simulated"
        state["logs"] = list(_mapping_logs)[-30:]
        if state.get("started_at"):
            state["elapsed"] = round(time.time() - state["started_at"], 1)
        return state


def _parse_mapping_line(line: str):
    with _mapping_lock:
        _mapping_logs.append(line)
        keyframe = re.search(r"keyframe\s*(?:num|number)?\s*[:=]?\s*(\d+)", line, re.I)
        if keyframe:
            _mapping_state["keyframes"] = int(keyframe.group(1))
        delay = re.search(r"time\s*delay\s*[:=]?\s*(-?\d+(?:\.\d+)?)", line, re.I)
        if delay:
            _mapping_state["time_delay"] = float(delay.group(1))
        pose_match = re.search(
            r"(?:current\s+robot\s+)?pose[^-+\d]*"
            r"([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s+"
            r"([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s+"
            r"([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)\s+"
            r"([-+]?\d+(?:\.\d+)?)",
            line,
            re.I,
        )
        if pose_match:
            _mapping_state["pose"] = [float(value) for value in pose_match.groups()]


def _read_mapping_output(process: subprocess.Popen):
    global _mapping_process
    try:
        if process.stdout:
            for raw in iter(process.stdout.readline, ""):
                line = raw.strip()
                if line:
                    _parse_mapping_line(line)
    finally:
        code = process.poll()
        with _mapping_lock:
            if _mapping_process is process:
                _mapping_state["status"] = "stopped" if code in (0, -signal.SIGINT) else "failed"
                _mapping_state["exit_code"] = code
                _mapping_process = None


def _start_mapping():
    global _mapping_process, _mapping_reader
    with _mapping_lock:
        if _mapping_process and _mapping_process.poll() is None:
            return _mapping_snapshot()
        _mapping_logs.clear()
        _spatial_map.reset()
        _mapping_state.update(
            status="starting",
            pid=None,
            started_at=time.time(),
            keyframes=0,
            time_delay=None,
            pose=None,
            last_error=None,
        )
        if not MAPPING_SERVER_BIN.exists():
            _mapping_state.update(status="simulated", last_error=f"建图程序不存在: {MAPPING_SERVER_BIN}")
            return _mapping_snapshot()
        try:
            _mapping_process = subprocess.Popen(
                [str(MAPPING_SERVER_BIN)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            _mapping_state.update(status="running", pid=_mapping_process.pid)
            _mapping_reader = threading.Thread(
                target=_read_mapping_output,
                args=(_mapping_process,),
                daemon=True,
                name="mapping-output",
            )
            _mapping_reader.start()
            return _mapping_snapshot()
        except Exception as exc:
            _mapping_state.update(status="failed", last_error=str(exc))
            _mapping_process = None
            raise


def _stop_mapping():
    global _mapping_process
    with _mapping_lock:
        process = _mapping_process
        if not process:
            if _mapping_state["status"] == "simulated":
                _mapping_state["status"] = "stopped"
            return _mapping_snapshot()
        _mapping_state["status"] = "stopping"
    try:
        os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    finally:
        with _mapping_lock:
            if _mapping_process is process:
                _mapping_process = None
            _mapping_state["status"] = "stopped"
    return _mapping_snapshot()


def _newest_map_directory():
    if MAPPING_SAVE_PATH.exists():
        return MAPPING_SAVE_PATH
    root = MAPPING_SAVE_PATH.parent
    candidates = [item for item in root.iterdir() if item.is_dir()] if root.exists() else []
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


def _saved_pcd(map_dir: Path | None):
    if not map_dir:
        return None
    for name in ("global_cloud_cleaned.pcd", "global_cloud.pcd"):
        path = map_dir / name
        if path.exists():
            return path
    return next(map_dir.rglob("*.pcd"), None)


def _run_agent_task(task: dict[str, Any]):
    global _agent_task
    with _agent_lock:
        if _agent_task is None or _agent_task.get("id") != task["id"]:
            return
        _agent_task["status"] = "running"
        _agent_task["started_at"] = time.time()
        _agent_task["progress"] = 10

    try:
        assignment = task.get("assignment") or {}
        goal = assignment.get("goal") or task.get("goal") or {}
        pose = goal.get("pose") if isinstance(goal, dict) else None
        if isinstance(pose, list) and len(pose) == 7 and NAV_OK:
            nav = _get_nav()
            result = navigate_to_pose(
                x=pose[0], y=pose[1], z=pose[2],
                qx=pose[3], qy=pose[4], qz=pose[5], qw=pose[6],
                timeout=float(task.get("timeout", 60.0)),
                nav_interface=nav,
            )
            success_values = {True}
            if NavigationResult is not None:
                success_values.add(NavigationResult.SUCCESS)
                success_values.add(getattr(NavigationResult, "ARRIVED", NavigationResult.SUCCESS))
            if result not in success_values:
                raise RuntimeError(f"导航任务返回: {result}")
        elif SIMULATION_MODE:
            time.sleep(0.25)
        else:
            raise RuntimeError("任务没有可执行的导航目标，或导航模块尚未就绪")

        with _agent_lock:
            if _agent_task and _agent_task.get("id") == task["id"] and _agent_task.get("status") != "cancelled":
                _agent_task.update(status="completed", progress=100, finished_at=time.time())
                _agent_task_history.append(dict(_agent_task))
                del _agent_task_history[:-50]
    except Exception as exc:  # noqa: BLE001
        with _agent_lock:
            if (
                _agent_task
                and _agent_task.get("id") == task["id"]
                and _agent_task.get("status") != "cancelled"
            ):
                _agent_task.update(status="failed", error=str(exc), finished_at=time.time())
                _agent_task_history.append(dict(_agent_task))
                del _agent_task_history[:-50]


def _lzf_decompress(data: bytes, expected_size: int) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        ctrl = data[i]
        i += 1
        if ctrl < 32:
            length = ctrl + 1
            out.extend(data[i : i + length])
            i += length
            continue
        length = ctrl >> 5
        ref = len(out) - ((ctrl & 0x1F) << 8) - 1
        if length == 7:
            length += data[i]
            i += 1
        ref -= data[i]
        i += 1
        length += 2
        if ref < 0:
            raise ValueError("PCD LZF 引用越界")
        for _ in range(length):
            out.append(out[ref])
            ref += 1
    if expected_size and len(out) != expected_size:
        raise ValueError(f"PCD 解压长度不匹配: {len(out)} != {expected_size}")
    return bytes(out)


def _unpack_value(fmt_type: str, size: int, raw: bytes, offset: int):
    if fmt_type == "F" and size == 4:
        return struct.unpack_from("<f", raw, offset)[0]
    if fmt_type == "F" and size == 8:
        return struct.unpack_from("<d", raw, offset)[0]
    if fmt_type == "U" and size == 1:
        return struct.unpack_from("<B", raw, offset)[0]
    if fmt_type == "U" and size == 2:
        return struct.unpack_from("<H", raw, offset)[0]
    if fmt_type == "U" and size == 4:
        return struct.unpack_from("<I", raw, offset)[0]
    if fmt_type == "I" and size == 1:
        return struct.unpack_from("<b", raw, offset)[0]
    if fmt_type == "I" and size == 2:
        return struct.unpack_from("<h", raw, offset)[0]
    if fmt_type == "I" and size == 4:
        return struct.unpack_from("<i", raw, offset)[0]
    raise ValueError(f"不支持字段类型 TYPE={fmt_type} SIZE={size}")


def parse_pcd(blob: bytes, source: str, limit: int = 5000):
    header_end = None
    header_lines = []
    pos = 0
    for line in blob.splitlines(keepends=True):
        text = line.decode("ascii", errors="ignore").strip()
        header_lines.append(text)
        pos += len(line)
        if text.upper().startswith("DATA "):
            header_end = pos
            break
    if header_end is None:
        raise ValueError("未找到 PCD DATA header")

    meta = {}
    for line in header_lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts:
            meta[parts[0].upper()] = parts[1:]

    fields = meta.get("FIELDS", [])
    sizes = [int(x) for x in meta.get("SIZE", [])]
    types = meta.get("TYPE", [])
    counts = [int(x) for x in meta.get("COUNT", ["1"] * len(fields))]
    points_n = int((meta.get("POINTS") or meta.get("WIDTH") or ["0"])[0])
    data_mode = (meta.get("DATA") or [""])[0].lower()
    if not points_n:
        raise ValueError("PCD POINTS 为空")
    if points_n > MAX_PCD_POINTS:
        raise ValueError(f"PCD 点数超过限制: {points_n} > {MAX_PCD_POINTS}")
    if "x" not in fields or "y" not in fields:
        raise ValueError("PCD 必须包含 x/y 字段")
    if len(fields) != len(sizes) or len(fields) != len(types):
        raise ValueError("PCD FIELDS/SIZE/TYPE header 不完整")

    x_idx, y_idx = fields.index("x"), fields.index("y")
    z_idx = fields.index("z") if "z" in fields else None
    field_widths = [sizes[i] * counts[i] for i in range(len(fields))]
    payload = blob[header_end:]
    stride = max(1, points_n // max(1, limit))
    points = []
    bounds = None

    def keep_point(x, y, z, idx):
        nonlocal bounds
        if not math.isfinite(x) or not math.isfinite(y) or not math.isfinite(z):
            return
        if bounds is None:
            bounds = [x, x, y, y, z, z]
        else:
            bounds[0] = min(bounds[0], x)
            bounds[1] = max(bounds[1], x)
            bounds[2] = min(bounds[2], y)
            bounds[3] = max(bounds[3], y)
            bounds[4] = min(bounds[4], z)
            bounds[5] = max(bounds[5], z)
        if idx % stride == 0:
            points.append([round(x, 3), round(y, 3), round(z, 3)])

    if data_mode == "ascii":
        rows = payload.decode("utf-8", errors="ignore").splitlines()
        for i, row in enumerate(rows):
            vals = row.split()
            if len(vals) < len(fields):
                continue
            x = float(vals[x_idx])
            y = float(vals[y_idx])
            z = float(vals[z_idx]) if z_idx is not None else 0.0
            keep_point(x, y, z, i)
        layout = "ascii"
    else:
        aos_offsets = []
        offset = 0
        for width in field_widths:
            aos_offsets.append(offset)
            offset += width
        point_step = offset
        if data_mode == "binary_compressed":
            if len(payload) < 8:
                raise ValueError("PCD binary_compressed payload 过短")
            compressed_size, raw_size = struct.unpack_from("<II", payload, 0)
            if compressed_size > len(payload) - 8:
                raise ValueError("PCD 压缩数据长度无效")
            if raw_size > MAX_PCD_BYTES:
                raise ValueError(f"PCD 解压数据超过限制: {raw_size} bytes")
            raw = _lzf_decompress(payload[8 : 8 + compressed_size], raw_size)
            soa_offsets = []
            offset = 0
            for width in field_widths:
                soa_offsets.append(offset)
                offset += width * points_n

            def value_at(i, idx):
                return _unpack_value(types[idx], sizes[idx], raw, soa_offsets[idx] + i * field_widths[idx])

            layout = "binary_compressed_soa"
        elif data_mode == "binary":
            raw = payload

            def value_at(i, idx):
                return _unpack_value(types[idx], sizes[idx], raw, i * point_step + aos_offsets[idx])

            layout = "binary_aos"
        else:
            raise ValueError(f"暂不支持 PCD DATA {data_mode}")

        for i in range(points_n):
            x = float(value_at(i, x_idx))
            y = float(value_at(i, y_idx))
            z = float(value_at(i, z_idx)) if z_idx is not None else 0.0
            keep_point(x, y, z, i)

    if not points or bounds is None:
        raise ValueError("PCD 未解析出有效点")
    return {
        "ok": True,
        "source": source,
        "data_mode": layout,
        "total_points": points_n,
        "sampled_points": len(points),
        "bounds": {
            "min_x": round(bounds[0], 3),
            "max_x": round(bounds[1], 3),
            "min_y": round(bounds[2], 3),
            "max_y": round(bounds[3], 3),
            "min_z": round(bounds[4], 3),
            "max_z": round(bounds[5], 3),
        },
        "points": points,
    }


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/status")
def api_status():
    nav = None
    pose = None
    if NAV_OK:
        try:
            nav = _get_nav()
            pose = _pose_list(getattr(nav, "current_pose", None))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": True, "nav_import": True, "nav_ready": False, "msg": str(exc)})
    return jsonify({
        "ok": True,
        "sdk": bool(getattr(_legacy_backend, "SDK_OK", False)),
        "robot_running": bool(getattr(_legacy_backend, "robot", None)),
        "nav_import": NAV_OK,
        "nav_ready": nav is not None,
        "nav_error": NAV_IMPORT_ERROR,
        "pose": pose,
        "robot_id": ROBOT_ID,
        "robot_name": ROBOT_NAME,
        "capabilities": ROBOT_CAPABILITIES,
        "agent_uptime": round(time.time() - _agent_started_at, 1),
        "current_task": _agent_task_snapshot(),
        "simulation": SIMULATION_MODE,
        "current_skill": _skill_snapshot(),
        "auth_enabled": bool(AGENT_TOKEN),
        "vla_tree": str(VLA_TREE_DIR),
        "location_map": str(LOCATION_MAP_PATH),
        "default_pcd": str(DEFAULT_PCD_PATH),
    })


def _system_cpu_percent():
    global _cpu_sample
    try:
        values = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
    except (OSError, ValueError, IndexError):
        return None
    with _cpu_sample_lock:
        previous = _cpu_sample
        _cpu_sample = (total, idle)
    if not previous or total <= previous[0]:
        return None
    return round(100.0 * (1.0 - (idle - previous[1]) / (total - previous[0])), 1)


def _system_memory():
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
        used = total - available
        return {
            "total_mb": round(total / 1024, 1),
            "used_mb": round(used / 1024, 1),
            "percent": round(used / total * 100, 1) if total else 0.0,
        }
    except (OSError, ValueError, KeyError):
        return None


def _process_rows(limit=16):
    rows = []
    own_uid = os.getuid()
    page_kb = os.sysconf("SC_PAGE_SIZE") / 1024
    for directory in Path("/proc").glob("[0-9]*"):
        if not directory.name.isdigit():
            continue
        try:
            pid = int(directory.name)
            stat_text = (directory / "stat").read_text()
            closing = stat_text.rfind(")")
            stat_fields = stat_text[closing + 2:].split()
            ppid = int(stat_fields[1])
            rss_mb = int(stat_fields[21]) * page_kb / 1024
            uid = (directory / "status").stat().st_uid
            command = (directory / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            if not command:
                command = stat_text[stat_text.find("(") + 1:closing]
            tokens = command.split()
            names = {Path(token).name for token in tokens[:3]}
            stoppable_name = next((name for name in names if name in STOPPABLE_PROCESSES), None)
            can_stop = uid == own_uid and pid != os.getpid() and stoppable_name is not None
            rows.append({
                "pid": pid,
                "ppid": ppid,
                "rss_mb": round(rss_mb, 1),
                "command": command[:180],
                "name": stoppable_name or (Path(tokens[0]).name if tokens else "unknown"),
                "can_stop": can_stop,
            })
        except (OSError, ValueError, IndexError):
            continue
    rows.sort(key=lambda item: item["rss_mb"], reverse=True)
    return rows[:limit] if limit else rows


@app.route("/api/system/resources")
def api_system_resources():
    try:
        load = os.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    return jsonify({
        "ok": True,
        "cpu_percent": _system_cpu_percent(),
        "memory": _system_memory(),
        "load": [round(value, 2) for value in load],
        "processes": _process_rows(),
        "stoppable": sorted(STOPPABLE_PROCESSES),
    })


@app.route("/api/system/processes/<int:pid>/stop", methods=["POST"])
def api_system_process_stop(pid: int):
    process = next((item for item in _process_rows(limit=None) if item["pid"] == pid), None)
    if not process or not process["can_stop"]:
        return _json_error("该进程不在安全停止白名单中", code="PROCESS_NOT_STOPPABLE"), 403
    try:
        os.kill(pid, signal.SIGTERM)
        return jsonify({"ok": True, "pid": pid, "signal": "SIGTERM", "process": process})
    except ProcessLookupError:
        return _json_error("进程已退出", code="PROCESS_NOT_FOUND"), 404
    except PermissionError:
        return _json_error("没有停止该进程的权限", code="PROCESS_PERMISSION_DENIED"), 403


@app.route("/api/nav/locations", methods=["GET", "POST"])
def api_nav_locations():
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            name = str(payload.get("name") or "").strip()
            pose = _pose_list(payload.get("pose"))
            if not name or len(name) > 40:
                return _json_error("点位名称不能为空且不能超过 40 个字符"), 400
            if not pose or len(pose) != 7 or not all(math.isfinite(value) for value in pose):
                return _json_error("pose 必须是 7 个有限数值"), 400
            if abs(pose[0]) > 1000 or abs(pose[1]) > 1000:
                return _json_error("点位坐标超出允许范围"), 400
            with _location_lock:
                data = _load_locations()
                existing_ids = [
                    int(item.get("id"))
                    for item in data.values()
                    if isinstance(item, dict) and str(item.get("id", "")).isdigit()
                ]
                location_id = max(existing_ids, default=0) + 1
                key = f"location_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"
                item = {
                    "id": location_id,
                    "name": name,
                    "pose": [round(value, 9) for value in pose],
                }
                data[key] = item
                LOCATION_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
                temporary = LOCATION_MAP_PATH.with_suffix(LOCATION_MAP_PATH.suffix + ".tmp")
                temporary.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, LOCATION_MAP_PATH)
            return jsonify({
                "ok": True,
                "source": str(LOCATION_MAP_PATH),
                "location": {
                    "key": key,
                    **item,
                    "yaw": _quat_to_yaw(item["pose"]),
                },
            }), 201

        data = _load_locations()
        locations = []
        for key, item in data.items():
            pose = item.get("pose", [0, 0, 0, 0, 0, 0, 1])
            locations.append({
                "key": key,
                "id": item.get("id"),
                "name": item.get("name", key),
                "pose": pose,
                "yaw": _quat_to_yaw(pose),
            })
        locations.sort(key=lambda x: (x["id"] is None, x["id"] or 0, x["key"]))
        return jsonify({"ok": True, "source": str(LOCATION_MAP_PATH), "locations": locations})
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), source=str(LOCATION_MAP_PATH), locations=[])


@app.route("/api/nav/locations/<key>", methods=["DELETE"])
def api_nav_location_delete(key: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", key):
        return _json_error("点位 key 格式无效"), 400
    try:
        with _location_lock:
            data = _load_locations()
            removed = data.pop(key, None)
            if removed is None:
                return _json_error("点位不存在"), 404
            temporary = LOCATION_MAP_PATH.with_suffix(LOCATION_MAP_PATH.suffix + ".tmp")
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, LOCATION_MAP_PATH)
        return jsonify({"ok": True, "key": key, "removed": removed})
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc))


@app.route("/api/nav/map/import", methods=["POST"])
def api_nav_map_import():
    try:
        if "file" in request.files:
            f = request.files["file"]
            source = f.filename or "uploaded.pcd"
            blob = f.read()
        else:
            data = request.get_json(silent=True) or {}
            path = Path(data.get("path") or DEFAULT_PCD_PATH).expanduser()
            if not path.exists():
                return _json_error(f"PCD 文件不存在: {path}", source=str(path))
            source = str(path.resolve())
            blob = path.read_bytes()
        return jsonify(parse_pcd(blob, source))
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc))


@app.route("/api/nav/current_pose")
def api_nav_current_pose():
    if not NAV_OK:
        return jsonify({"ok": True, "pose": [0, 0, 0, 0, 0, 0, 1], "source": "SIMULATED"})
    try:
        nav = _get_nav()
        pose = None
        if hasattr(nav, "get_current_pose"):
            pose = _pose_list(nav.get_current_pose())
        if pose is None:
            pose = _pose_list(getattr(nav, "current_pose", None))
        if pose is None and hasattr(nav, "GetCurrentPose"):
            pose = _pose_list(nav.GetCurrentPose())
        return jsonify({"ok": True, "pose": pose, "source": "navigation_interface"})
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), pose=None)


@app.route("/api/camera/main")
def api_camera_main():
    snapshot = Path(MAIN_CAMERA_SNAPSHOT).expanduser() if MAIN_CAMERA_SNAPSHOT else None
    if snapshot and snapshot.exists():
        return send_file(snapshot)
    if getattr(_legacy_backend, "SDK_OK", False):
        try:
            return _legacy_backend._rgb_response(
                _legacy_backend.read_rgb(_legacy_backend.SensorType.HEAD_LEFT_CAMERA)
            )
        except Exception:
            pass
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
      <rect width="960" height="540" fill="#080c11"/>
      <rect x="28" y="28" width="904" height="484" rx="10" fill="#101923" stroke="#2b3542"/>
      <path d="M72 382 C210 286 326 252 474 286 C624 320 748 294 888 190" fill="none" stroke="#42d392" stroke-width="4" opacity=".55"/>
      <path d="M72 418 C244 346 374 332 516 360 C642 384 748 354 888 274" fill="none" stroke="#3b82f6" stroke-width="3" opacity=".45"/>
      <circle cx="752" cy="162" r="54" fill="none" stroke="#f3b340" stroke-width="3" opacity=".55"/>
      <text x="56" y="82" fill="#d9e5f0" font-family="monospace" font-size="30">FRONT_HEAD_CAMERA / MAIN VIEW</text>
      <text x="56" y="126" fill="#8fa2b6" font-family="monospace" font-size="18">set GALBOT_MAIN_CAMERA_SNAPSHOT to use a live frame</text>
      <text x="56" y="474" fill="#668197" font-family="monospace" font-size="16">time {time.strftime("%H:%M:%S")}</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.route("/api/nav/goto", methods=["POST"])
def api_nav_goto():
    data = request.get_json(silent=True) or {}
    pose = data.get("pose")
    if not isinstance(pose, list) or len(pose) != 7:
        return _json_error("pose 必须是 [x,y,z,qx,qy,qz,qw]")
    if not NAV_OK:
        return jsonify({"ok": True, "status": "SIMULATED", "pose": pose})
    try:
        nav = _get_nav()

        def worker():
            navigate_to_pose(
                x=pose[0],
                y=pose[1],
                z=pose[2],
                qx=pose[3],
                qy=pose[4],
                qz=pose[5],
                qw=pose[6],
                timeout=float(data.get("timeout", 30.0)),
                nav_interface=nav,
            )

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"ok": True, "status": "SENT", "pose": pose})
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), pose=pose)


@app.route("/api/nav/stop", methods=["POST"])
def api_nav_stop():
    if not NAV_OK:
        return jsonify({"ok": True, "status": "SIMULATED"})
    try:
        nav = _get_nav()
        ok = stop_navigation(nav_interface=nav)
        return jsonify({"ok": bool(ok)})
    except TypeError:
        try:
            ok = stop_navigation(_get_nav())
            return jsonify({"ok": bool(ok)})
        except Exception as exc:  # noqa: BLE001
            return _json_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc))


@app.route("/api/nav/nudge", methods=["POST"])
def api_nav_nudge():
    """Switch to chassis pose control and move in a selected base-frame direction."""
    global _nav_nudge_active
    data = request.get_json(silent=True) or {}
    direction = str(data.get("direction") or "forward").lower()
    direction_vectors = {
        "forward": (1.0, 0.0),
        "backward": (-1.0, 0.0),
        "left": (0.0, 1.0),
        "right": (0.0, -1.0),
    }
    if direction not in direction_vectors:
        return _json_error("direction 仅支持 forward/backward/left/right", 400)
    try:
        distance = max(0.05, min(1.0, float(data.get("distance", 0.3))))
        requested_stop = data.get("stop_after")
        stop_after = (
            max(0.5, min(8.0, float(requested_stop)))
            if requested_stop is not None
            else max(1.0, min(8.0, distance / 0.15 + 0.8))
        )
    except (TypeError, ValueError):
        return _json_error("distance 和 stop_after 必须是数字", 400)
    with _nav_nudge_lock:
        if _nav_nudge_active:
            return _json_error("前进脱困正在执行，请等待自动停止", 409)
        _nav_nudge_active = True

    if not getattr(_legacy_backend, "SDK_OK", False):
        with _nav_nudge_lock:
            _nav_nudge_active = False
        return jsonify({
            "ok": True,
            "status": "SIMULATED",
            "direction": direction,
            "distance": distance,
            "stop_after": stop_after,
        })

    try:
        robot, _, nav = _legacy_backend.get_instances()
        controller_name = _legacy_backend.gm.ControllerName.CHASSIS_POSE_CTRL
        status = robot.switch_controller(controller_name)
        if status != _legacy_backend.ControlStatus.SUCCESS:
            with _nav_nudge_lock:
                _nav_nudge_active = False
            return _json_error(
                f"切换 CHASSIS_POSE_CTRL 失败: {_legacy_backend.status_name(status)}"
            )

        vector_x, vector_y = direction_vectors[direction]
        target = [
            vector_x * distance,
            vector_y * distance,
            0.0, 0.0, 0.0, 0.0, 1.0,
        ]

        def worker():
            global _nav_nudge_active
            try:
                nav.move_straight_to(target, is_blocking=False, timeout=10)
                time.sleep(stop_after)
            finally:
                try:
                    nav.stop_navigation()
                finally:
                    with _nav_nudge_lock:
                        _nav_nudge_active = False

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({
            "ok": True,
            "status": "SENT",
            "controller": "CHASSIS_POSE_CTRL",
            "direction": direction,
            "target": target,
            "stop_after": stop_after,
        })
    except Exception as exc:  # noqa: BLE001
        with _nav_nudge_lock:
            _nav_nudge_active = False
        return _json_error(str(exc))


@app.route("/api/mapping/status")
def api_mapping_status():
    return jsonify({
        "ok": True,
        "mapping": _mapping_snapshot(),
        "mapping_server": str(MAPPING_SERVER_BIN),
        "engine_tools": str(ENGINE_TOOLS_BIN),
        "save_path": str(MAPPING_SAVE_PATH),
    })


@app.route("/api/mapping/spatial")
def api_mapping_spatial():
    capture_error = None
    mapping = _mapping_snapshot()
    if mapping["running"] and getattr(_legacy_backend, "SDK_OK", False):
        try:
            _, _, nav = _legacy_backend.get_instances()
            raw = _legacy_backend.read_lidar(_legacy_backend.SensorType.BASE_LIDAR)
            points = _legacy_backend._normalise_lidar_points(raw, limit=12000)
            pose = mapping.get("pose")
            if not pose and nav is not None:
                try:
                    pose = _pose_list(nav.get_current_pose())
                except Exception:
                    pose = _pose_list(getattr(nav, "current_pose", None))
            if pose:
                _spatial_map.add_scan(points, pose)
            else:
                capture_error = "尚未收到机器人全局位姿"
        except Exception as exc:  # noqa: BLE001
            capture_error = str(exc)
    spatial = _spatial_map.snapshot()
    return jsonify({
        "ok": True,
        "source": "BASE_LIDAR_OCCUPANCY_GRID",
        "capture_error": capture_error,
        "mapping_running": mapping["running"],
        "spatial": spatial,
    })


@app.route("/api/mapping/spatial/erase", methods=["POST"])
def api_mapping_spatial_erase():
    payload = request.get_json(silent=True) or {}
    circles = payload.get("circles")
    if not isinstance(circles, list):
        return _json_error("circles 必须是数组", 400)
    erased = _spatial_map.erase(circles)
    return jsonify({
        "ok": True,
        "erased": erased,
        "spatial": _spatial_map.snapshot(),
    })


@app.route("/api/mapping/start", methods=["POST"])
def api_mapping_start():
    try:
        stop_navigation(nav_interface=_get_nav()) if NAV_OK else None
    except Exception:
        pass
    try:
        state = _start_mapping()
        return jsonify({"ok": True, "mapping": state})
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), mapping=_mapping_snapshot())


@app.route("/api/mapping/stop", methods=["POST"])
def api_mapping_stop():
    try:
        return jsonify({"ok": True, "mapping": _stop_mapping()})
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), mapping=_mapping_snapshot())


@app.route("/api/mapping/save", methods=["POST"])
def api_mapping_save():
    global _nav
    state = _mapping_snapshot()
    if not state["running"]:
        return _json_error("建图程序未运行，不能保存地图"), 409

    if not ENGINE_TOOLS_BIN.exists():
        if state["status"] != "simulated":
            return _json_error(f"地图保存工具不存在: {ENGINE_TOOLS_BIN}")
        map_dir = DEFAULT_PCD_PATH.parent if DEFAULT_PCD_PATH.exists() else None
        pcd = DEFAULT_PCD_PATH if DEFAULT_PCD_PATH.exists() else None
        _stop_mapping()
        return jsonify({
            "ok": True,
            "simulated": True,
            "map_dir": str(map_dir) if map_dir else None,
            "pcd": str(pcd) if pcd else None,
            "pose": state.get("pose") or [0, 0, 0, 0, 0, 0, 1],
        })

    try:
        result = subprocess.run(
            [str(ENGINE_TOOLS_BIN)],
            input="1\n",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            check=False,
        )
        output = result.stdout or ""
        with _mapping_lock:
            for line in output.splitlines()[-40:]:
                if line.strip():
                    _mapping_logs.append("[save] " + line.strip())
        map_dir = _newest_map_directory()
        pcd = _saved_pcd(map_dir)
        save_ok = result.returncode == 0 or bool(pcd)
        if not save_ok:
            return _json_error(
                f"地图保存失败，engine_tools exit={result.returncode}",
                output=output[-4000:],
                mapping=_mapping_snapshot(),
            )
        # Official sequence: save first, then terminate mapping.
        _stop_mapping()
        with _nav_lock:
            _nav = None
        return jsonify({
            "ok": True,
            "map_dir": str(map_dir) if map_dir else None,
            "pcd": str(pcd) if pcd else None,
            "pose": state.get("pose"),
            "engine_exit": result.returncode,
            "mapping": _mapping_snapshot(),
        })
    except subprocess.TimeoutExpired:
        return _json_error("地图保存超时，建图程序保持运行，未自动关闭")
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), mapping=_mapping_snapshot())


@app.route("/api/mapping/teleop", methods=["POST"])
def api_mapping_teleop():
    global _teleop_generation
    data = request.get_json(silent=True) or {}
    key = str(data.get("key") or "stop").lower()
    linear = max(0.05, min(0.3, float(data.get("linear", 0.2))))
    angular = max(0.1, min(0.6, float(data.get("angular", 0.4))))
    commands = {
        "w": ([linear, 0.0, 0.0], [0.0, 0.0, 0.0]),
        "s": ([-linear, 0.0, 0.0], [0.0, 0.0, 0.0]),
        "a": ([0.0, linear, 0.0], [0.0, 0.0, 0.0]),
        "d": ([0.0, -linear, 0.0], [0.0, 0.0, 0.0]),
        "q": ([0.0, 0.0, 0.0], [0.0, 0.0, angular]),
        "e": ([0.0, 0.0, 0.0], [0.0, 0.0, -angular]),
        "stop": ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]),
    }
    if key not in commands:
        return _json_error("key 仅支持 w/s/a/d/q/e/stop")
    lv, av = commands[key]
    try:
        if getattr(_legacy_backend, "SDK_OK", False):
            robot, _, _ = _legacy_backend.get_instances()
            if key == "stop":
                robot.stop_base()
            else:
                robot.set_base_velocity(lv, av, 0.12)
        _teleop_generation += 1
        generation = _teleop_generation

        def auto_stop():
            if generation != _teleop_generation:
                return
            try:
                if getattr(_legacy_backend, "SDK_OK", False):
                    robot, _, _ = _legacy_backend.get_instances()
                    robot.stop_base()
            except Exception:
                pass

        if key != "stop":
            threading.Timer(0.35, auto_stop).start()
        return jsonify({
            "ok": True,
            "status": "SENT" if getattr(_legacy_backend, "SDK_OK", False) else "SIMULATED",
            "key": key,
            "linear_velocity": lv,
            "angular_velocity": av,
        })
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc))


@app.route("/api/nav/relocalize", methods=["POST"])
def api_nav_relocalize():
    data = request.get_json(silent=True) or {}
    pose = data.get("pose") or _mapping_snapshot().get("pose") or [0, 0, 0, 0, 0, 0, 1]
    if not isinstance(pose, list) or len(pose) != 7:
        return _json_error("pose 必须是 [x,y,z,qx,qy,qz,qw]")
    if not getattr(_legacy_backend, "SDK_OK", False):
        return jsonify({"ok": True, "localized": True, "status": "SIMULATED", "pose": pose})
    try:
        _, _, nav = _legacy_backend.get_instances()
        for attempt in range(1, 11):
            nav.relocalize(pose)
            time.sleep(1)
            if nav.is_localized():
                return jsonify({"ok": True, "localized": True, "attempt": attempt, "pose": pose})
        return _json_error("重定位失败", localized=False, pose=pose)
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), localized=False, pose=pose)


@app.route("/api/agent/state")
def api_agent_state():
    pose = None
    if NAV_OK:
        try:
            pose = _pose_list(getattr(_get_nav(), "current_pose", None))
        except Exception:
            pose = None
    return jsonify({
        "ok": True,
        "protocol": "galbot-fleet-agent/v1",
        "robot_id": ROBOT_ID,
        "name": ROBOT_NAME,
        "capabilities": ROBOT_CAPABILITIES,
        "online": True,
        "nav_ready": NAV_OK,
        "sdk_ready": bool(getattr(_legacy_backend, "SDK_OK", False)),
        "pose": pose,
        "current_task": _agent_task_snapshot(),
        "simulation": SIMULATION_MODE,
        "message_count": len(_agent_messages),
        "current_skill": _skill_snapshot(),
        "uptime": round(time.time() - _agent_started_at, 1),
        "timestamp": time.time(),
    })


@app.route("/api/agent/tasks", methods=["GET", "POST"])
def api_agent_tasks():
    global _agent_task
    if request.method == "GET":
        with _agent_lock:
            return jsonify({
                "ok": True,
                "current": dict(_agent_task) if _agent_task else None,
                "history": list(_agent_task_history[-20:]),
            })

    data = request.get_json(silent=True) or {}
    task_id = str(data.get("id") or f"task-{int(time.time() * 1000)}")
    assignment = data.get("assignment") or {}
    goal = assignment.get("goal") or data.get("goal") or {}
    pose = goal.get("pose") if isinstance(goal, dict) else None
    if not SIMULATION_MODE:
        if not NAV_OK:
            return _json_error(
                f"导航模块未就绪: {NAV_IMPORT_ERROR}",
                code="NAV_NOT_READY",
            ), 503
        if not isinstance(pose, list) or len(pose) != 7:
            return _json_error(
                "真机任务必须包含 assignment.goal.pose（7 元素位姿）",
                code="INVALID_GOAL",
            ), 400
    with _agent_lock:
        if _agent_task and _agent_task.get("status") in {"accepted", "running"}:
            return _json_error(
                f"机器人正在执行任务 {_agent_task.get('id')}",
                code="ROBOT_BUSY",
                current=dict(_agent_task),
            ), 409
        _agent_task = {
            "id": task_id,
            "description": str(data.get("description") or ""),
            "type": str(data.get("type") or "cooperative"),
            "assignment": data.get("assignment") or {},
            "goal": data.get("goal") or {},
            "status": "accepted",
            "progress": 0,
            "coordinator": data.get("coordinator"),
            "created_at": time.time(),
        }
        accepted = dict(_agent_task)
    threading.Thread(target=_run_agent_task, args=(accepted,), daemon=True).start()
    return jsonify({"ok": True, "robot_id": ROBOT_ID, "task": accepted}), 202


@app.route("/api/agent/tasks/current/cancel", methods=["POST"])
def api_agent_task_cancel():
    global _agent_task
    with _agent_lock:
        if not _agent_task or _agent_task.get("status") not in {"accepted", "running"}:
            return jsonify({"ok": True, "cancelled": False, "msg": "没有正在执行的任务"})
        _agent_task.update(status="cancelled", finished_at=time.time())
        cancelled = dict(_agent_task)
        _agent_task_history.append(cancelled)
        del _agent_task_history[:-50]
    if NAV_OK:
        try:
            stop_navigation(nav_interface=_get_nav())
        except Exception:
            pass
    return jsonify({"ok": True, "cancelled": True, "task": cancelled})


@app.route("/api/agent/estop", methods=["POST"])
def api_agent_estop():
    api_agent_task_cancel()
    try:
        response = _legacy_backend.api_estop()
        return response
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc))


@app.route("/api/agent/messages", methods=["GET", "POST"])
def api_agent_messages():
    if request.method == "GET":
        with _agent_lock:
            return jsonify({"ok": True, "robot_id": ROBOT_ID, "messages": list(_agent_messages[-100:])})
    data = request.get_json(silent=True) or {}
    message_id = str(data.get("id") or f"msg-{uuid.uuid4().hex[:12]}")
    source = str(data.get("source") or "").strip()
    target = str(data.get("target") or "all").strip()
    if not source:
        return _json_error("消息 source 不能为空", code="INVALID_MESSAGE"), 400
    if target not in {"all", ROBOT_ID}:
        return jsonify({"ok": True, "accepted": False, "robot_id": ROBOT_ID})
    item = {
        "id": message_id,
        "source": source,
        "target": target,
        "kind": str(data.get("kind") or "status"),
        "text": str(data.get("text") or "")[:2000],
        "payload": data.get("payload") if isinstance(data.get("payload"), dict) else {},
        "timestamp": float(data.get("timestamp") or time.time()),
        "received_at": time.time(),
    }
    with _agent_lock:
        if not any(existing.get("id") == message_id for existing in _agent_messages):
            _agent_messages.append(item)
            del _agent_messages[:-100]
    return jsonify({"ok": True, "accepted": True, "robot_id": ROBOT_ID, "message": item}), 202


@app.route("/api/agent/messages/publish", methods=["POST"])
def api_agent_message_publish():
    if not COORDINATOR_URL:
        return _json_error("未配置 GALBOT_COORDINATOR_URL", code="COORDINATOR_NOT_CONFIGURED"), 503
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return _json_error("消息 text 不能为空", code="INVALID_MESSAGE"), 400
    payload = {
        "source": ROBOT_ID,
        "target": str(data.get("target") or "all"),
        "kind": str(data.get("kind") or "status"),
        "text": text[:2000],
        "payload": data.get("payload") if isinstance(data.get("payload"), dict) else {},
    }
    try:
        response = _http_json(COORDINATOR_URL + "/api/messages", payload, timeout=2.0)
        return jsonify({"ok": bool(response.get("ok")), "message": response.get("message")}), 202
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), code="COORDINATOR_UNREACHABLE"), 502


@app.route("/api/agent/map/sync", methods=["POST"])
def api_agent_map_sync():
    if not COORDINATOR_URL:
        return _json_error("未配置 GALBOT_COORDINATOR_URL", code="COORDINATOR_NOT_CONFIGURED"), 503
    try:
        metadata_response = _http_json(COORDINATOR_URL + "/api/maps/current", timeout=3.0)
        metadata = metadata_response.get("map") or {}
        blob = _http_bytes(COORDINATOR_URL + "/api/maps/current/file")
        if len(blob) > MAX_PCD_BYTES:
            raise ValueError("共享地图超过机器人端大小限制")
        digest = hashlib.sha256(blob).hexdigest()
        if not metadata.get("sha256") or not hmac.compare_digest(digest, str(metadata["sha256"])):
            raise ValueError("共享地图 SHA-256 校验失败")
        header = blob[:4096].decode("ascii", errors="ignore").upper()
        if "FIELDS" not in header or "DATA " not in header:
            raise ValueError("共享地图不是有效 PCD")
        target = DEFAULT_PCD_PATH.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(blob)
        os.replace(temporary, target)
        return jsonify({"ok": True, "map": metadata, "path": str(target)})
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), code="MAP_SYNC_FAILED"), 502


@app.route("/api/agent/handovers/<handover_id>/events", methods=["POST"])
def api_agent_handover_event(handover_id: str):
    if not COORDINATOR_URL:
        return _json_error("未配置 GALBOT_COORDINATOR_URL", code="COORDINATOR_NOT_CONFIGURED"), 503
    data = request.get_json(silent=True) or {}
    payload = {
        "event": str(data.get("event") or ""),
        "actor": ROBOT_ID,
        "evidence": data.get("evidence") if isinstance(data.get("evidence"), dict) else {},
    }
    if not payload["event"]:
        return _json_error("event 不能为空", code="INVALID_HANDOVER_EVENT"), 400
    try:
        response = _http_json(
            COORDINATOR_URL + f"/api/handovers/{handover_id}/events",
            payload,
            timeout=2.0,
        )
        return jsonify({"ok": bool(response.get("ok")), "handover": response.get("handover")})
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"msg": f"Coordinator HTTP {exc.code}"}
        return _json_error(detail.get("msg") or "交接事件被拒绝", code=detail.get("code")), exc.code
    except Exception as exc:  # noqa: BLE001
        return _json_error(str(exc), code="COORDINATOR_UNREACHABLE"), 502


@app.route("/api/agent/skills")
def api_agent_skills():
    skills = []
    for manifest in _list_arm_skills(SKILL_DIR):
        skills.append({
            key: manifest.get(key)
            for key in ("name", "version", "description", "type", "validated", "duration_s", "metrics", "safety")
        })
    return jsonify({
        "ok": True,
        "robot_id": ROBOT_ID,
        "runtime_available": SKILL_RUNTIME_OK,
        "runtime_error": SKILL_RUNTIME_ERROR or None,
        "skills": skills,
        "current": _skill_snapshot(),
    })


@app.route("/api/agent/skills/<skill_name>/invoke", methods=["POST"])
def api_agent_skill_invoke(skill_name: str):
    global _skill_state
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return _json_error("调用手臂技能必须显式传入 confirm=true", code="CONFIRMATION_REQUIRED"), 400
    try:
        manifest, model = _load_arm_skill(SKILL_DIR, skill_name)
        if not manifest.get("validated") and not (SIMULATION_MODE and data.get("allow_unvalidated") is True):
            return _json_error("技能尚未通过真机验证，只能在仿真中预览", code="SKILL_NOT_VALIDATED"), 409
        steps = int(data.get("steps") or manifest.get("default_steps") or 40)
        _arm_skill_trajectory(manifest, model, steps)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return _json_error(str(exc), code="INVALID_SKILL"), 400
    with _skill_lock:
        if _skill_state and _skill_state.get("status") in {"accepted", "running"}:
            return _json_error("已有手臂技能正在运行", code="SKILL_BUSY", current=dict(_skill_state)), 409
        _skill_state = {
            "id": f"skill-run-{uuid.uuid4().hex[:10]}",
            "name": skill_name,
            "status": "accepted",
            "step": 0,
            "progress": 0,
            "created_at": time.time(),
        }
        accepted = dict(_skill_state)
    threading.Thread(
        target=_run_arm_skill,
        args=(skill_name, manifest, model, steps),
        daemon=True,
        name=f"skill-{skill_name}",
    ).start()
    return jsonify({"ok": True, "run": accepted}), 202


@app.route("/api/fleet/robots", methods=["GET", "POST"])
def api_fleet_robots():
    if request.method == "GET":
        with _fleet_lock:
            robots = list(_fleet_robots.values())
        return jsonify({"ok": True, "robot_id": ROBOT_ID, "robots": robots})

    data = request.get_json(silent=True) or {}
    robots_in = data.get("robots") or []
    if not isinstance(robots_in, list):
        return _json_error("robots 必须是数组")
    updated = []
    with _fleet_lock:
        _fleet_robots.clear()
        for idx, item in enumerate(robots_in[:3], start=1):
            if not isinstance(item, dict):
                continue
            name = item.get("name") or f"robot-{idx}"
            try:
                url = _normalize_robot_url(item.get("url") or item.get("ip") or "")
            except ValueError:
                continue
            robot = {
                "id": item.get("id") or name,
                "name": name,
                "url": url,
                "role": item.get("role") or ("leader" if idx == 1 else "worker"),
                "status": "unknown",
                "last_seen": None,
            }
            _fleet_robots[robot["id"]] = robot
            updated.append(robot)
    _append_fleet_message("fleet", ROBOT_ID, "all", f"配置协同机器人 {len(updated)} 台")
    return jsonify({"ok": True, "robot_id": ROBOT_ID, "robots": updated})


@app.route("/api/fleet/ping", methods=["POST"])
def api_fleet_ping():
    results = []
    with _fleet_lock:
        robots = list(_fleet_robots.values())
    for robot in robots:
        result = {**robot}
        try:
            status = _http_json(robot["url"] + "/api/status", timeout=1.2)
            result["status"] = "online"
            result["remote_robot_id"] = status.get("robot_id")
            result["last_seen"] = time.time()
        except Exception as exc:  # noqa: BLE001
            result["status"] = "offline"
            result["error"] = str(exc)
        with _fleet_lock:
            if robot["id"] in _fleet_robots:
                _fleet_robots[robot["id"]].update({k: v for k, v in result.items() if k in ("status", "remote_robot_id", "last_seen", "error")})
        results.append(result)
    _append_fleet_message("heartbeat", ROBOT_ID, "all", "完成协同心跳检测", {"results": results})
    return jsonify({"ok": True, "robots": results})


@app.route("/api/fleet/message", methods=["GET", "POST"])
def api_fleet_message():
    if request.method == "GET":
        with _fleet_lock:
            return jsonify({"ok": True, "messages": list(_fleet_messages[-80:])})
    data = request.get_json(silent=True) or {}
    source = data.get("source") or ROBOT_ID
    target = data.get("target") or "all"
    text = data.get("text") or ""
    item = _append_fleet_message(data.get("kind") or "message", source, target, text, data.get("payload") or {})
    return jsonify({"ok": True, "message": item})


@app.route("/api/fleet/task", methods=["POST"])
def api_fleet_task():
    data = request.get_json(silent=True) or {}
    task = {
        "id": data.get("id") or f"task-{int(time.time())}",
        "time": time.time(),
        "source": data.get("source") or ROBOT_ID,
        "description": data.get("description") or "",
        "mode": data.get("mode") or "cooperative",
        "goal": data.get("goal") or {},
        "assignments": data.get("assignments") or {},
    }
    with _fleet_lock:
        _fleet_tasks.append(task)
        del _fleet_tasks[:-80]
        robots = list(_fleet_robots.values())
    deliveries = []
    if data.get("relay", True):
        for robot in robots:
            payload = {**task, "target_robot": robot["id"], "relay": False}
            try:
                resp = _http_json(robot["url"] + "/api/fleet/task", payload, timeout=1.2) if robot["url"].rstrip("/") != request.host_url.rstrip("/") else {"ok": True}
                deliveries.append({"robot": robot["id"], "ok": bool(resp.get("ok", True))})
            except urllib.error.URLError as exc:
                deliveries.append({"robot": robot["id"], "ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                deliveries.append({"robot": robot["id"], "ok": False, "error": str(exc)})
    _append_fleet_message("task", task["source"], "all", f"协同任务: {task['description']}", {"task": task, "deliveries": deliveries})
    return jsonify({"ok": True, "task": task, "deliveries": deliveries})


# Restore the original manipulator, gripper, sensor and VLA-facing API surface.
# Navigation routes owned by this server stay authoritative.
_LEGACY_ROUTES = {
    "/api/robot/urdf/import",
    "/api/estop",
    "/api/joint/set",
    "/api/joint/get",
    "/api/joint/urdf_sync",
    "/api/motion/ik",
    "/api/motion/ee",
    "/api/motion/ee_get",
    "/api/gripper/set",
    "/api/nav/check",
    "/api/nav/map",
    "/api/nav/explore",
    "/api/nav/explore/stop",
    "/api/sensor/start",
    "/api/sensor/rgb",
    "/api/sensor/depth",
    "/api/sensor/lidar",
    "/api/sensor/imu",
}

for _rule in _legacy_backend.app.url_map.iter_rules():
    if _rule.rule not in _LEGACY_ROUTES:
        continue
    app.add_url_rule(
        _rule.rule,
        endpoint=f"legacy_{_rule.endpoint}",
        view_func=_legacy_backend.app.view_functions[_rule.endpoint],
        methods=sorted(_rule.methods - {"HEAD", "OPTIONS"}),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    print("\nGalbot VLA Tree Control Platform")
    print(f"  vla_tree:     {VLA_TREE_DIR}")
    print(f"  location_map: {LOCATION_MAP_PATH}")
    print(f"  default_pcd:  {DEFAULT_PCD_PATH}")
    print(f"  nav import:   {'ok' if NAV_OK else 'simulated - ' + NAV_IMPORT_ERROR}")
    print(f"  url:          http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
