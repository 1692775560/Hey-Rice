"""
Galbot G1 控制台后端
运行: python server.py
访问: http://localhost:7860

真机模式下前端所有操作会通过此后端转发给 galbot_sdk。
"""
import time, json, threading, os, sys, base64, io, math, struct
import xml.etree.ElementTree as ET
from flask import Flask, jsonify, request, send_from_directory, Response

app = Flask(__name__, static_folder="static")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_PCD_BYTES = int(os.environ.get("GALBOT_MAX_PCD_BYTES", str(128 * 1024 * 1024)))
MAX_PCD_POINTS = int(os.environ.get("GALBOT_MAX_PCD_POINTS", "20000000"))


# ── SDK 导入（真机模式使用，模拟模式可缺省）────────────────────
SDK_OK = False
try:
    import galbot_sdk.g1 as gm
    from galbot_sdk.g1 import (
        GalbotRobot, GalbotMotion, GalbotNavigation,
        SensorType, JointGroup, ControlStatus,
        Trajectory, TrajectoryPoint, JointCommand,
    )
    SDK_OK = True
    print("✅ galbot_sdk 加载成功")
except ImportError:
    print("⚠  galbot_sdk 未找到 — 以模拟模式运行（前端模拟按钮仍可用）")

# ── 单例 ──────────────────────────────────────────────────────
robot  = None
motion = None
nav    = None
_lock  = threading.Lock()
_sensor_lock = threading.Lock()
_motion_lock = threading.Lock()
_active_urdf_chains = {}

def get_instances():
    global robot, motion, nav
    if robot is None and SDK_OK:
        robot  = GalbotRobot.get_instance()
        motion = GalbotMotion.get_instance()
        nav    = GalbotNavigation.get_instance()
        enable = {SensorType.HEAD_LEFT_CAMERA,
                  SensorType.LEFT_ARM_CAMERA,
                  SensorType.LEFT_ARM_DEPTH_CAMERA,
                  SensorType.BASE_LIDAR,
                  SensorType.TORSO_IMU}
        robot.init(enable)
        motion.init()
        nav.init()
        time.sleep(2)
    return robot, motion, nav

def status_name(s):
    if not SDK_OK: return "SIMULATED"
    return {
        ControlStatus.SUCCESS: "SUCCESS",
        ControlStatus.TIMEOUT: "TIMEOUT",
        ControlStatus.FAULT:   "FAULT",
    }.get(s, str(s))

def read_rgb(sensor):
    with _sensor_lock:
        r, _, _ = get_instances()
        return r.get_rgb_data(sensor)

def read_depth(sensor):
    with _sensor_lock:
        r, _, _ = get_instances()
        return r.get_depth_data(sensor)

def read_lidar(sensor):
    with _sensor_lock:
        r, _, _ = get_instances()
        return r.get_lidar_data(sensor)

def read_imu(sensor):
    with _sensor_lock:
        r, _, _ = get_instances()
        return r.get_imu_data(sensor)

def _pose_list(pose):
    if pose is None:
        return None
    try:
        return [float(x) for x in list(pose)]
    except Exception:
        return None

def _normalise_lidar_points(raw, limit=900):
    if raw is None:
        return []
    if isinstance(raw, dict):
        data = raw.get("data")
        point_step = int(raw.get("point_step") or raw.get("pointStep") or 0)
        fields = raw.get("fields") or []
        if isinstance(data, (bytes, bytearray, memoryview)) and point_step > 0 and fields:
            field_map = {}
            for field in fields:
                if isinstance(field, dict):
                    name = field.get("name")
                    offset = field.get("offset")
                    datatype = int(field.get("datatype", 7))
                else:
                    name = getattr(field, "name", None)
                    offset = getattr(field, "offset", None)
                    datatype = int(getattr(field, "datatype", 7))
                if name in {"x", "y", "z"} and offset is not None:
                    field_map[name] = (int(offset), datatype)
            if "x" in field_map and "y" in field_map:
                blob = bytes(data)
                count = min(
                    len(blob) // point_step,
                    int(raw.get("width", 0)) * max(1, int(raw.get("height", 1))) or len(blob) // point_step,
                )
                stride = max(1, count // max(1, limit))

                def unpack_field(base, spec):
                    offset, datatype = spec
                    fmt = "<d" if datatype == 8 else "<f"
                    return struct.unpack_from(fmt, blob, base + offset)[0]

                points = []
                for index in range(0, count, stride):
                    base = index * point_step
                    try:
                        x = float(unpack_field(base, field_map["x"]))
                        y = float(unpack_field(base, field_map["y"]))
                        z = float(unpack_field(base, field_map.get("z", (0, 7)))) if "z" in field_map else 0.0
                        if math.isfinite(x) and math.isfinite(y) and math.isfinite(z):
                            points.append([round(x, 3), round(y, 3), round(z, 3)])
                    except (struct.error, ValueError, TypeError):
                        continue
                return points
        for key in ("points", "xyz", "data"):
            if key in raw:
                raw = raw[key]
                break
    if hasattr(raw, "points"):
        raw = raw.points
    points = []
    try:
        iterator = list(raw)
    except Exception:
        return []
    stride = max(1, len(iterator) // limit) if iterator else 1
    for item in iterator[::stride]:
        try:
            if isinstance(item, dict):
                x = float(item.get("x", item.get("0", 0.0)))
                y = float(item.get("y", item.get("1", 0.0)))
                z = float(item.get("z", item.get("2", 0.0)))
            else:
                vals = list(item)
                x = float(vals[0]); y = float(vals[1])
                z = float(vals[2]) if len(vals) > 2 else 0.0
            if math.isfinite(x) and math.isfinite(y):
                points.append([round(x, 3), round(y, 3), round(z, 3)])
        except Exception:
            continue
    return points

def _vector3(value):
    if value is None:
        return [0.0, 0.0, 0.0]
    if isinstance(value, dict):
        return [float(value.get(axis, 0.0)) for axis in ("x", "y", "z")]
    if all(hasattr(value, axis) for axis in ("x", "y", "z")):
        return [float(getattr(value, axis)) for axis in ("x", "y", "z")]
    try:
        values = list(value)
        return [float(values[index]) if index < len(values) else 0.0 for index in range(3)]
    except Exception:
        return [0.0, 0.0, 0.0]

def _normalise_imu(raw):
    if isinstance(raw, dict):
        get_value = raw.get
    else:
        get_value = lambda key, default=None: getattr(raw, key, default)
    return {
        "accel": _vector3(get_value("accel")),
        "gyro": _vector3(get_value("gyro")),
        "magnet": _vector3(get_value("magnet")),
        "timestamp_ns": int(get_value("timestamp_ns", 0) or 0),
    }

def _normalise_depth(raw, limit=7200):
    if not isinstance(raw, dict):
        return None
    data = raw.get("data")
    width = int(raw.get("width") or 0)
    height = int(raw.get("height") or 0)
    scale = float(raw.get("depth_scale") or 1000.0)
    if not isinstance(data, (bytes, bytearray, memoryview)) or width <= 0 or height <= 0:
        return None
    blob = bytes(data)
    stride = max(1, int(math.ceil(math.sqrt(width * height / max(1, limit)))))
    values = []
    valid = []
    for y in range(0, height, stride):
        row = []
        for x in range(0, width, stride):
            offset = (y * width + x) * 2
            try:
                millimeters = struct.unpack_from("<H", blob, offset)[0]
            except struct.error:
                millimeters = 0
            meters = round(millimeters / scale, 3) if millimeters else 0.0
            row.append(meters)
            if meters > 0:
                valid.append(meters)
        values.append(row)
    return {
        "width": len(values[0]) if values else 0,
        "height": len(values),
        "source_width": width,
        "source_height": height,
        "depth_scale": scale,
        "min_m": round(min(valid), 3) if valid else None,
        "max_m": round(max(valid), 3) if valid else None,
        "values": values,
    }

def _rgb_response(raw):
    if isinstance(raw, dict):
        data = raw.get("data")
        image_format = str(raw.get("format") or "rgb8")
        if isinstance(data, (bytes, bytearray, memoryview)):
            mime = "image/png" if bytes(data[:8]).startswith(b"\x89PNG") else "image/jpeg"
            response = Response(bytes(data), mimetype=mime)
            response.headers["X-Galbot-Image-Format"] = image_format
            response.headers["Cache-Control"] = "no-store"
            return response
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return Response(bytes(raw), mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
    raise ValueError("相机未返回可解码的压缩图像")

def _nav_failure_reason(target_pose, current_pose=None, reachable=False, err=None):
    causes = []
    suggestions = []
    pose = _pose_list(target_pose) or [0, 0, 0, 0, 0, 0, 1]
    cur = _pose_list(current_pose)
    if err:
        causes.append(f"SDK 检查异常: {err}")
        suggestions.append("确认导航模块已初始化，并查看机器人端导航日志。")
    if cur is None:
        causes.append("当前位姿为空，机器人可能尚未完成定位。")
        suggestions.append("先执行重定位，确认 is_localized() 返回 True。")
    if len(pose) >= 2:
        x, y = pose[0], pose[1]
        if abs(x) > 3.0 or abs(y) > 3.0:
            causes.append("目标点超出当前前端局部地图显示范围。")
            suggestions.append("在目标位姿里把 X/Y 调回雷达边界内，或先探索扩大地图。")
    if not reachable and not causes:
        causes.extend([
            "导航规划器返回不可达。",
            "可能存在障碍物占据、局部地图未知区域、坐标系转换错误或代价地图未更新。",
        ])
        suggestions.extend([
            "用自主导航页的目标位姿编辑 X/Y/Yaw 后重新检查。",
            "启动扫描建图或重定位后再发送导航。",
        ])
    return {
        "target_pose": pose,
        "current_pose": cur,
        "causes": causes,
        "suggestions": suggestions,
        "hint": "可先重定位，再用雷达扫描边界确认目标点位于已知可行区域内。",
    }

def _location_map_path():
    candidates = [
        os.environ.get("GALBOT_LOCATION_MAP"),
        os.path.join(APP_DIR, "..", "..", "..", "config", "location_map.json"),
        os.path.join(APP_DIR, "..", "..", "userdata", "galbot_tree", "config", "location_map.json"),
        os.path.join(APP_DIR, "..", "..", "userdata", "galbot_tree", "galbot_g1", "config", "location_map.json"),
        "/home/galbot/vla_client/vla_tree/config/location_map.json",
    ]
    for item in candidates:
        if not item:
            continue
        path = os.path.abspath(item)
        if os.path.exists(path):
            return path
    return os.path.abspath(candidates[-1])

def _location_percentages(items):
    poses = [v.get("pose", [0, 0]) for v in items.values() if isinstance(v, dict)]
    xs = [float(p[0]) for p in poses if len(p) > 1]
    ys = [float(p[1]) for p in poses if len(p) > 1]
    xs.append(0.0); ys.append(0.0)
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    pad_x = max(0.5, (max_x - min_x) * 0.12)
    pad_y = max(0.5, (max_y - min_y) * 0.12)
    min_x -= pad_x; max_x += pad_x
    min_y -= pad_y; max_y += pad_y
    span_x = max(0.1, max_x - min_x)
    span_y = max(0.1, max_y - min_y)
    def conv(pose):
        x = float(pose[0]) if len(pose) > 0 else 0.0
        y = float(pose[1]) if len(pose) > 1 else 0.0
        return round((x - min_x) / span_x * 100, 1), round((1.0 - (y - min_y) / span_y) * 100, 1)
    return conv

def _pcd_default_paths():
    return [
        os.environ.get("GALBOT_GLOBAL_MAP"),
        "/var/maps/cur/global_cloud_cleaned.pcd",
        os.path.join(APP_DIR, "..", "..", "..", "global_cloud_cleaned.pcd"),
        os.path.join(APP_DIR, "..", "..", "global_cloud_cleaned.pcd"),
        os.path.join(APP_DIR, "..", "..", "..", "..", "global_cloud_cleaned.pcd"),
        "/Users/tree/Desktop/baai/global_cloud_cleaned.pcd",
        "/home/galbot/vla_client/vla_tree/global_cloud_cleaned.pcd",
    ]

def _find_default_pcd_path():
    for item in _pcd_default_paths():
        if item and os.path.exists(os.path.abspath(item)):
            return os.path.abspath(item)
    return None

def _urdf_default_paths():
    return [
        os.environ.get("GALBOT_URDF"),
        os.path.join(APP_DIR, "..", "..", "..", "galbot_one_golf_description", "urdf", "galbot_g1_v2_2_1.urdf"),
        os.path.join(APP_DIR, "..", "..", "..", "galbot_one_golf_description", "urdf", "galbot_g1.urdf"),
        "/Users/tree/Desktop/baai/galbot_one_golf_description/urdf/galbot_g1_v2_2_1.urdf",
        "/home/galbot/galbot_one_golf_description/urdf/galbot_g1_v2_2_1.urdf",
    ]

def _find_default_urdf_path():
    for item in _urdf_default_paths():
        if item and os.path.exists(os.path.abspath(item)):
            return os.path.abspath(item)
    return None

def _parse_urdf_bytes(blob, source="uploaded"):
    root = ET.fromstring(blob)
    chains = {name: [] for name in ("left_arm", "right_arm", "head", "leg")}
    for joint in root.findall("joint"):
        name = joint.attrib.get("name", "")
        joint_type = joint.attrib.get("type", "")
        if joint_type == "fixed":
            continue
        chain = next((prefix for prefix in chains if name.startswith(prefix + "_joint")), None)
        if not chain:
            continue
        limit = joint.find("limit")
        lower = -3.14
        upper = 3.14
        velocity = None
        effort = None
        if limit is not None:
            lower = float(limit.attrib.get("lower", lower))
            upper = float(limit.attrib.get("upper", upper))
            velocity = float(limit.attrib["velocity"]) if "velocity" in limit.attrib else None
            effort = float(limit.attrib["effort"]) if "effort" in limit.attrib else None
        origin = joint.find("origin")
        axis = joint.find("axis")
        parent = joint.find("parent")
        child = joint.find("child")
        chains[chain].append({
            "name": name,
            "type": joint_type,
            "min": round(lower, 6),
            "max": round(upper, 6),
            "default": round(min(max(0.0, lower), upper), 6),
            "velocity": velocity,
            "effort": effort,
            "origin": origin.attrib if origin is not None else {},
            "axis": axis.attrib.get("xyz", "") if axis is not None else "",
            "parent": parent.attrib.get("link", "") if parent is not None else "",
            "child": child.attrib.get("link", "") if child is not None else "",
        })
    chains = {k: v for k, v in chains.items() if v}
    if not chains:
        raise ValueError("URDF 未解析出 left_arm/right_arm/head/leg 关节链")
    return {
        "ok": True,
        "source": source,
        "robot": root.attrib.get("name", "unknown"),
        "chains": chains,
        "chain_count": len(chains),
        "joint_count": sum(len(v) for v in chains.values()),
    }

def _lzf_decompress(data, expected_size):
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        ctrl = data[i]
        i += 1
        if ctrl < 32:
            length = ctrl + 1
            out.extend(data[i:i + length])
            i += length
        else:
            length = ctrl >> 5
            ref = len(out) - ((ctrl & 0x1f) << 8) - 1
            if length == 7:
                length += data[i]
                i += 1
            ref -= data[i]
            i += 1
            length += 2
            if ref < 0:
                raise ValueError("PCD LZF 数据引用越界")
            for _ in range(length):
                out.append(out[ref])
                ref += 1
    if expected_size and len(out) != expected_size:
        raise ValueError(f"PCD 解压长度不匹配: {len(out)} != {expected_size}")
    return bytes(out)

def _pcd_unpack(fmt_type, size, raw, offset):
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
    return None

def _parse_pcd_bytes(blob, source="uploaded", limit=7000):
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
        if not parts:
            continue
        meta[parts[0].upper()] = parts[1:]

    fields = meta.get("FIELDS") or []
    sizes = [int(x) for x in meta.get("SIZE", [])]
    types = meta.get("TYPE") or []
    counts = [int(x) for x in meta.get("COUNT", ["1"] * len(fields))]
    points_n = int((meta.get("POINTS") or meta.get("WIDTH") or ["0"])[0])
    data_mode = (meta.get("DATA") or [""])[0].lower()
    if not fields or len(fields) != len(sizes) or len(fields) != len(types):
        raise ValueError("PCD FIELDS/SIZE/TYPE header 不完整")
    if not points_n:
        raise ValueError("PCD POINTS 为空")
    if points_n > MAX_PCD_POINTS:
        raise ValueError(f"PCD 点数超过限制: {points_n} > {MAX_PCD_POINTS}")
    for name in ("x", "y"):
        if name not in fields:
            raise ValueError(f"PCD 缺少 {name} 字段")

    z_idx = fields.index("z") if "z" in fields else None
    x_idx, y_idx = fields.index("x"), fields.index("y")
    field_widths = [sizes[i] * counts[i] for i in range(len(fields))]
    aos_offsets = []
    offset = 0
    for width in field_widths:
        aos_offsets.append(offset)
        offset += width
    point_step = offset
    payload = blob[header_end:]

    if data_mode == "binary_compressed":
        if len(payload) < 8:
            raise ValueError("PCD binary_compressed payload 过短")
        compressed_size, raw_size = struct.unpack_from("<II", payload, 0)
        if compressed_size > len(payload) - 8:
            raise ValueError("PCD 压缩数据长度无效")
        if raw_size > MAX_PCD_BYTES:
            raise ValueError(f"PCD 解压数据超过限制: {raw_size} bytes")
        raw = _lzf_decompress(payload[8:8 + compressed_size], raw_size)
        soa_offsets = []
        offset = 0
        for width in field_widths:
            soa_offsets.append(offset)
            offset += width * points_n
        layout = "binary_compressed_soa"
        def value_at(i, idx):
            return _pcd_unpack(types[idx], sizes[idx], raw, soa_offsets[idx] + i * field_widths[idx])
    elif data_mode == "binary":
        raw = payload
        layout = "binary_aos"
        def value_at(i, idx):
            return _pcd_unpack(types[idx], sizes[idx], raw, i * point_step + aos_offsets[idx])
    elif data_mode == "ascii":
        rows = payload.decode("utf-8", errors="ignore").splitlines()
        stride = max(1, points_n // max(1, limit))
        points = []
        bounds = {"min_x": None, "max_x": None, "min_y": None, "max_y": None, "min_z": None, "max_z": None}
        kept = total = 0
        for i, row in enumerate(rows):
            vals = row.split()
            if len(vals) < len(fields):
                continue
            try:
                x = float(vals[x_idx]); y = float(vals[y_idx]); z = float(vals[z_idx]) if z_idx is not None else 0.0
            except Exception:
                continue
            if not math.isfinite(x) or not math.isfinite(y) or not math.isfinite(z):
                continue
            total += 1
            if bounds["min_x"] is None:
                bounds = {"min_x": x, "max_x": x, "min_y": y, "max_y": y, "min_z": z, "max_z": z}
            else:
                bounds["min_x"] = min(bounds["min_x"], x); bounds["max_x"] = max(bounds["max_x"], x)
                bounds["min_y"] = min(bounds["min_y"], y); bounds["max_y"] = max(bounds["max_y"], y)
                bounds["min_z"] = min(bounds["min_z"], z); bounds["max_z"] = max(bounds["max_z"], z)
            if i % stride == 0:
                points.append([round(x, 3), round(y, 3), round(z, 3)]); kept += 1
        return {"ok": True, "source": source, "data_mode": "ascii", "fields": fields, "total_points": total, "sampled_points": kept, "bounds": bounds, "points": points}
    else:
        raise ValueError(f"暂不支持 PCD DATA {data_mode}")

    stride = max(1, points_n // max(1, limit))
    points = []
    bounds = {"min_x": None, "max_x": None, "min_y": None, "max_y": None, "min_z": None, "max_z": None}
    total = kept = 0
    for i in range(points_n):
        try:
            x = float(value_at(i, x_idx)); y = float(value_at(i, y_idx)); z = float(value_at(i, z_idx)) if z_idx is not None else 0.0
        except Exception:
            continue
        if not math.isfinite(x) or not math.isfinite(y) or not math.isfinite(z):
            continue
        total += 1
        if bounds["min_x"] is None:
            bounds = {"min_x": x, "max_x": x, "min_y": y, "max_y": y, "min_z": z, "max_z": z}
        else:
            bounds["min_x"] = min(bounds["min_x"], x); bounds["max_x"] = max(bounds["max_x"], x)
            bounds["min_y"] = min(bounds["min_y"], y); bounds["max_y"] = max(bounds["max_y"], y)
            bounds["min_z"] = min(bounds["min_z"], z); bounds["max_z"] = max(bounds["max_z"], z)
        if i % stride == 0:
            points.append([round(x, 3), round(y, 3), round(z, 3)]); kept += 1
    if not points:
        raise ValueError("PCD 未解析出有效 XY 点")
    return {"ok": True, "source": source, "data_mode": layout, "fields": fields, "total_points": total, "sampled_points": kept, "bounds": {k: round(v, 3) if v is not None else None for k, v in bounds.items()}, "points": points}

# ── 静态页面 ──────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

# ── 系统 ──────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    return jsonify({"sdk": SDK_OK, "robot_running": robot is not None})

@app.route("/api/robot/urdf/import", methods=["POST"])
def api_robot_urdf_import():
    global _active_urdf_chains
    try:
        source = None
        blob = None
        if "file" in request.files:
            f = request.files["file"]
            source = f.filename or "uploaded.urdf"
            blob = f.read()
        else:
            data = request.get_json(silent=True) or {}
            path = data.get("path") or _find_default_urdf_path()
            if not path:
                return jsonify({"ok": False, "msg": "未找到默认 URDF，请选择文件上传或设置 GALBOT_URDF。"})
            source = os.path.abspath(path)
            with open(source, "rb") as f:
                blob = f.read()
        result = _parse_urdf_bytes(blob, source=source)
        _active_urdf_chains = result["chains"]
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/estop", methods=["POST"])
def api_estop():
    try:
        r, _, n = get_instances()
        if n: n.stop_navigation()
        if r: r.request_shutdown(); r.wait_for_shutdown(); r.destroy()
        global robot, motion, nav
        robot = motion = nav = None
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    return jsonify({"ok": True})

# ── 关节控制 ──────────────────────────────────────────────────
@app.route("/api/joint/set", methods=["POST"])
def api_joint_set():
    data = request.get_json()
    chain     = data.get("chain", "left_arm")
    positions = data.get("positions", [])
    max_speed = float(data.get("max_speed", 0.1))
    if not SDK_OK:
        return jsonify({"ok": True, "status": "SIMULATED"})
    try:
        r, _, _ = get_instances()
        status = r.set_joint_positions(positions, [chain], [], True, max_speed, 20.0)
        return jsonify({"ok": status == ControlStatus.SUCCESS, "status": status_name(status)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/joint/get")
def api_joint_get():
    chain = request.args.get("chain", "left_arm")
    if not SDK_OK:
        return jsonify({"ok": True, "positions": [0.0]*7})
    try:
        r, _, _ = get_instances()
        pos = r.get_joint_positions([chain], [])
        return jsonify({"ok": True, "positions": list(pos)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/joint/urdf_sync", methods=["POST"])
def api_joint_urdf_sync():
    data = request.get_json(silent=True) or {}
    chain = str(data.get("chain") or "")
    joints = _active_urdf_chains.get(chain)
    if chain not in {"left_arm", "right_arm"} or not joints:
        return jsonify({"ok": False, "msg": "请先导入包含对应机械臂的 URDF"}), 400
    try:
        positions = [float(value) for value in data.get("positions", [])]
        if len(joints) > 10 or len(positions) != len(joints):
            raise ValueError(f"关节数量不匹配: expected={len(joints)}, actual={len(positions)}")
        for index, (value, joint) in enumerate(zip(positions, joints), start=1):
            if (
                not math.isfinite(value)
                or abs(value) > 3.2
                or not float(joint["min"]) <= value <= float(joint["max"])
            ):
                raise ValueError(f"J{index} 超出 URDF 限位")
        follow = data.get("follow") is True
        speed = min(0.05 if follow else 0.12, max(0.01, float(data.get("max_speed", 0.05))))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400
    if not SDK_OK:
        return jsonify({"ok": True, "status": "SIMULATED", "positions": positions})
    try:
        with _motion_lock:
            r, _, _ = get_instances()
            current = list(r.get_joint_positions([chain], []))
            if len(current) == len(positions):
                largest_delta = max(abs(target - actual) for target, actual in zip(positions, current))
                if largest_delta > 0.25:
                    return jsonify({
                        "ok": False,
                        "msg": f"单次关节变化 {largest_delta:.3f} rad 超过 0.25 rad，请分步拖动",
                    }), 409
            status = r.set_joint_positions(
                positions, [chain], [], not follow, speed, 20.0
            )
        return jsonify({
            "ok": status == ControlStatus.SUCCESS,
            "status": status_name(status),
            "positions": positions,
            "max_speed": speed,
            "follow": follow,
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500

# ── 末端位姿 ──────────────────────────────────────────────────
@app.route("/api/motion/ik", methods=["POST"])
def api_ik():
    data  = request.get_json()
    chain = data.get("chain", "left_arm")
    pose  = data.get("pose", [0]*7)
    if not SDK_OK:
        return jsonify({"ok": True, "joints": {chain: [0.0]*7}})
    try:
        _, m, _ = get_instances()
        status, joints = m.inverse_kinematics(
            target_pose=pose, chain_names=[chain],
            target_frame="EndEffector", reference_frame="base_link",
            enable_collision_check=True)
        ok = status == gm.MotionStatus.SUCCESS
        return jsonify({"ok": ok, "joints": joints if ok else {}})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/motion/ee", methods=["POST"])
def api_ee_set():
    data  = request.get_json()
    chain = data.get("chain", "left_arm")
    pose  = data.get("pose", [0]*7)
    if not SDK_OK:
        return jsonify({"ok": True})
    try:
        _, m, _ = get_instances()
        status = m.set_end_effector_pose(
            target_pose=pose, end_effector_frame=chain,
            reference_frame="base_link", enable_collision_check=True,
            is_blocking=True, timeout=5.0, params=gm.Parameter())
        return jsonify({"ok": status == gm.MotionStatus.SUCCESS, "status": str(status)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/motion/ee_get")
def api_ee_get():
    chain = request.args.get("chain", "left_arm")
    if not SDK_OK:
        return jsonify({"ok": True, "pose": [0.127,0.234,0.736,0.022,0.013,0.034,0.999]})
    try:
        _, m, _ = get_instances()
        status, pose = m.get_end_effector_pose_on_chain(
            chain_name=chain, frame_id="EndEffector", reference_frame="base_link")
        return jsonify({"ok": status == gm.MotionStatus.SUCCESS, "pose": list(pose)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ── 夹爪 ──────────────────────────────────────────────────────
@app.route("/api/gripper/set", methods=["POST"])
def api_gripper():
    data    = request.get_json()
    side    = data.get("side", "l")
    width   = float(data.get("width", 0.08))
    speed   = float(data.get("speed", 0.05))
    force   = float(data.get("force", 10))
    blocking= bool(data.get("blocking", True))
    if not SDK_OK:
        return jsonify({"ok": True})
    try:
        r, _, _ = get_instances()
        grp = JointGroup.LEFT_GRIPPER if side == "l" else JointGroup.RIGHT_GRIPPER
        r.set_gripper_command(grp, width, speed, force, blocking)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ── 导航 ──────────────────────────────────────────────────────
@app.route("/api/nav/goto", methods=["POST"])
def api_nav_goto():
    data = request.get_json(silent=True) or {}
    pose = data.get("pose", [0,0,0,0,0,0,1])
    if not SDK_OK:
        return jsonify({"ok": True, "reachable": True})
    try:
        _, _, n = get_instances()
        cur = n.get_current_pose()
        try:
            reachable = bool(n.check_path_reachability(pose, cur))
        except Exception as check_err:
            reason = _nav_failure_reason(pose, cur, reachable=False, err=str(check_err))
            return jsonify({"ok": False, "reachable": False, "msg": "路径检查失败", "reason": reason})
        if not reachable:
            reason = _nav_failure_reason(pose, cur, reachable=False)
            return jsonify({"ok": False, "reachable": False, "msg": "路径不可达", "reason": reason})
        def _nav():
            for _ in range(3):
                n.navigate_to_goal(pose, enable_collision_check=True, is_blocking=True, timeout=30)
                time.sleep(0.5)
                if n.check_goal_arrival(): break
        threading.Thread(target=_nav, daemon=True).start()
        return jsonify({"ok": True, "reachable": True, "current_pose": _pose_list(cur)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "reason": _nav_failure_reason(pose, err=str(e))})

@app.route("/api/nav/check", methods=["POST"])
def api_nav_check():
    data = request.get_json(silent=True) or {}
    pose = data.get("pose", [0,0,0,0,0,0,1])
    if not SDK_OK:
        return jsonify({"ok": True, "reachable": True, "reason": None})
    try:
        _, _, n = get_instances()
        cur = n.get_current_pose()
        try:
            reachable = bool(n.check_path_reachability(pose, cur))
        except Exception as check_err:
            reason = _nav_failure_reason(pose, cur, reachable=False, err=str(check_err))
            return jsonify({"ok": False, "reachable": False, "msg": "路径检查失败", "reason": reason})
        reason = None if reachable else _nav_failure_reason(pose, cur, reachable=False)
        return jsonify({"ok": True, "reachable": reachable, "reason": reason, "current_pose": _pose_list(cur)})
    except Exception as e:
        return jsonify({"ok": False, "reachable": False, "msg": str(e), "reason": _nav_failure_reason(pose, err=str(e))})

@app.route("/api/nav/stop", methods=["POST"])
def api_nav_stop():
    if SDK_OK:
        try:
            _, _, n = get_instances()
            n.stop_navigation()
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})
    return jsonify({"ok": True})

@app.route("/api/nav/locations")
def api_nav_locations():
    config_path = _location_map_path()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        to_percent = _location_percentages(data)
        locations = []
        for key, item in data.items():
            pose = item.get("pose", [0, 0, 0, 0, 0, 0, 1])
            px, py = to_percent(pose)
            locations.append({
                "key": key,
                "name": item.get("name", key),
                "id": item.get("id"),
                "pose": pose,
                "px": px,
                "py": py,
            })
        return jsonify({"ok": True, "locations": locations, "source": config_path})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "source": config_path, "locations": []})


@app.route("/api/nav/map/import", methods=["POST"])
def api_nav_map_import():
    try:
        source = None
        blob = None
        if "file" in request.files:
            f = request.files["file"]
            source = f.filename or "uploaded.pcd"
            blob = f.read()
        else:
            data = request.get_json(silent=True) or {}
            path = data.get("path") or _find_default_pcd_path()
            if not path:
                return jsonify({"ok": False, "msg": "未找到默认 PCD，请选择文件上传或设置 GALBOT_GLOBAL_MAP。"})
            source = os.path.abspath(path)
            with open(source, "rb") as f:
                blob = f.read()
        result = _parse_pcd_bytes(blob, source=source)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/nav/map")
def api_nav_map():
    if not SDK_OK:
        return jsonify({"ok": True, "pose": [0,0,0,0,0,0,1], "points": [], "source": "SIMULATED"})
    try:
        r, _, n = get_instances()
        pose = None
        points = []
        if n:
            try:
                pose = _pose_list(n.get_current_pose())
            except Exception:
                pose = None
        if r:
            try:
                points = _normalise_lidar_points(r.get_lidar_data(SensorType.BASE_LIDAR))
            except Exception as lidar_err:
                return jsonify({"ok": False, "msg": f"LiDAR 读取失败: {lidar_err}", "pose": pose, "points": []})
        return jsonify({"ok": True, "pose": pose, "points": points, "source": "BASE_LIDAR"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e), "points": []})

@app.route("/api/nav/explore", methods=["POST"])
def api_nav_explore():
    if not SDK_OK:
        return jsonify({"ok": True, "status": "SIMULATED"})
    try:
        _, _, n = get_instances()
        for method in ("start_exploration", "start_mapping", "start_slam"):
            if hasattr(n, method):
                getattr(n, method)()
                return jsonify({"ok": True, "method": method})
        return jsonify({"ok": False, "msg": "当前 SDK 未暴露自动探索接口，前端将只进行雷达扫描建图。"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/nav/explore/stop", methods=["POST"])
def api_nav_explore_stop():
    if not SDK_OK:
        return jsonify({"ok": True, "status": "SIMULATED"})
    try:
        _, _, n = get_instances()
        for method in ("stop_exploration", "stop_mapping", "stop_slam", "stop_navigation"):
            if hasattr(n, method):
                getattr(n, method)()
                return jsonify({"ok": True, "method": method})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/nav/relocalize", methods=["POST"])
def api_relocalize():
    data = request.get_json(silent=True) or {}
    pose = data.get("pose", [0,0,0,0,0,0,1])
    if not SDK_OK:
        return jsonify({"ok": True, "localized": True})
    try:
        _, _, n = get_instances()
        for _ in range(10):
            n.relocalize(pose); time.sleep(1)
            if n.is_localized():
                return jsonify({"ok": True, "localized": True})
        return jsonify({"ok": False, "localized": False})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ── 传感器 ────────────────────────────────────────────────────
@app.route("/api/sensor/start", methods=["POST"])
def api_sensor_start():
    if not SDK_OK:
        return jsonify({"ok": True})
    try:
        r, _, _ = get_instances()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/sensor/rgb")
def api_sensor_rgb():
    if not SDK_OK:
        svg = """<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360' viewBox='0 0 640 360'>
        <rect width='640' height='360' fill='#0f1720'/>
        <rect x='24' y='24' width='592' height='312' rx='16' fill='#17202b' stroke='#2f3b47'/>
        <text x='52' y='92' fill='#9fb2c7' font-size='28' font-family='monospace'>LEFT_ARM_CAMERA</text>
        <text x='52' y='140' fill='#6f8598' font-size='18' font-family='monospace'>模拟图像流未连接</text>
        <text x='52' y='184' fill='#6f8598' font-size='16' font-family='monospace'>启动后端图像接口后可替换为真实帧</text>
        <circle cx='520' cy='180' r='60' fill='none' stroke='#3fb950' stroke-width='3' opacity='.6'/>
        <circle cx='520' cy='180' r='110' fill='none' stroke='#3fb950' stroke-width='2' opacity='.3'/>
        </svg>"""
        return Response(svg, mimetype='image/svg+xml')
    try:
        rgb = read_rgb(SensorType.LEFT_ARM_CAMERA)
        return _rgb_response(rgb)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/sensor/depth")
def api_sensor_depth():
    if not SDK_OK:
        return jsonify({"ok": True, "depth": None, "source": "SIMULATED"})
    try:
        depth = _normalise_depth(read_depth(SensorType.LEFT_ARM_DEPTH_CAMERA))
        if depth is None:
            return jsonify({"ok": False, "msg": "深度相机未返回有效 16UC1 数据"})
        return jsonify({"ok": True, "depth": depth, "source": "LEFT_ARM_DEPTH_CAMERA"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/sensor/lidar")
def api_sensor_lidar():
    if not SDK_OK:
        return jsonify({"ok": True, "points": []})
    try:
        lidar = read_lidar(SensorType.BASE_LIDAR)
        points = _normalise_lidar_points(lidar, limit=2400)
        return jsonify({"ok": True, "points": points, "count": len(points), "source": "BASE_LIDAR"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/sensor/imu")
def api_sensor_imu():
    if not SDK_OK:
        return jsonify({"ok": True, "data": {"accel_z": 9.8, "gyro_x": 0.001}})
    try:
        imu = read_imu(SensorType.TORSO_IMU)
        return jsonify({"ok": True, "data": _normalise_imu(imu), "source": "TORSO_IMU"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ── 启动 ──────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"\n🤖 Galbot G1 控制台")
    print(f"   SDK 状态: {'✅ 已加载' if SDK_OK else '⚠  未找到 (模拟模式)'}")
    print(f"   访问地址: http://localhost:{port}")
    print(f"   SSH 转发: ssh -L {port}:localhost:{port} galbot@<robot_ip>\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
