from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
import http.client
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from urllib.parse import quote, urlsplit

from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = "camera_system_secret_2120"

DB_PATH = "cameras.db"
SVG_ROOT = os.getenv("CAMERA_SVG_ROOT", "C:/Users/dania/Documents/School_Project")
DEFAULT_BUILDINGS = [1, 2, 3]
DEFAULT_FLOORS = [1, 2, 3]
MEDIAMTX_WEBRTC_BASE = os.getenv("MEDIAMTX_WEBRTC_BASE", "http://127.0.0.1:8889").rstrip("/")
MEDIAMTX_HLS_BASE = os.getenv("MEDIAMTX_HLS_BASE", "http://127.0.0.1:8888").rstrip("/")

LEGACY_USERS = {
    "admin": {"password": "school2120", "role": "admin"},
    "security": {"password": "camera_pass", "role": "viewer"},
}


def connect_db(row_factory=False):
    conn = sqlite3.connect(DB_PATH)
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = connect_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cameras (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            building    INTEGER NOT NULL DEFAULT 1,
            floor       INTEGER NOT NULL,
            svg_x       REAL NOT NULL,
            svg_y       REAL NOT NULL,
            rotation    REAL NOT NULL DEFAULT 0,
            ip          TEXT NOT NULL DEFAULT '',
            port        INTEGER NOT NULL DEFAULT 80,
            status      TEXT DEFAULT 'offline',
            description TEXT DEFAULT '',
            location    TEXT DEFAULT '',
            stream_type TEXT NOT NULL DEFAULT 'none',
            rtsp_url    TEXT DEFAULT '',
            stream_path TEXT DEFAULT '',
            extra_data  TEXT DEFAULT '{}'
        )
    """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            username      TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'viewer'
        )
    """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_buildings (
            username TEXT NOT NULL,
            building INTEGER NOT NULL,
            PRIMARY KEY (username, building),
            FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
        )
    """
    )

    cur.execute("PRAGMA table_info(cameras)")
    columns = {row[1] for row in cur.fetchall()}
    camera_migrations = {
        "building": "ALTER TABLE cameras ADD COLUMN building INTEGER NOT NULL DEFAULT 1",
        "rotation": "ALTER TABLE cameras ADD COLUMN rotation REAL NOT NULL DEFAULT 0",
        "ip": "ALTER TABLE cameras ADD COLUMN ip TEXT NOT NULL DEFAULT ''",
        "port": "ALTER TABLE cameras ADD COLUMN port INTEGER NOT NULL DEFAULT 80",
        "stream_type": "ALTER TABLE cameras ADD COLUMN stream_type TEXT NOT NULL DEFAULT 'none'",
        "rtsp_url": "ALTER TABLE cameras ADD COLUMN rtsp_url TEXT DEFAULT ''",
        "stream_path": "ALTER TABLE cameras ADD COLUMN stream_path TEXT DEFAULT ''",
    }
    for column, sql in camera_migrations.items():
        if column not in columns:
            cur.execute(sql)

    for username, payload in LEGACY_USERS.items():
        cur.execute("SELECT username FROM users WHERE username=?", (username,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, generate_password_hash(payload["password"]), payload["role"]),
            )

    cur.execute("SELECT username FROM users WHERE role <> 'admin'")
    non_admin_users = [row[0] for row in cur.fetchall()]
    for username in non_admin_users:
        cur.execute("SELECT COUNT(*) FROM user_buildings WHERE username=?", (username,))
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT OR IGNORE INTO user_buildings (username, building) VALUES (?, ?)",
                [(username, building) for building in DEFAULT_BUILDINGS],
            )

    conn.commit()
    conn.close()


def generate_camera_id(cur):
    cur.execute("SELECT id FROM cameras WHERE id LIKE 'CAM-%'")
    max_number = 0

    for (cam_id,) in cur.fetchall():
        try:
            number = int(cam_id.split("-")[-1])
            if number > max_number:
                max_number = number
        except (ValueError, AttributeError, IndexError):
            continue

    return f"CAM-{max_number + 1:05d}"


def check_camera_http(ip, port, timeout=1.5):
    conn = None
    try:
        conn = http.client.HTTPConnection(ip, port=port, timeout=timeout)
        conn.request("GET", "/")
        response = conn.getresponse()
        return response.status == 200
    except Exception:
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def parse_extra_data(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def encode_extra_data(value):
    return json.dumps(parse_extra_data(value), ensure_ascii=False)


def sanitize_path(value):
    path = (value or "").strip().strip("/")
    if not path:
        return ""

    parsed = urlsplit(path)
    if parsed.scheme and parsed.path:
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if not parsed.netloc and len(parts) > 1 and ":" in parts[0]:
            parts = parts[1:]
        path = "/".join(parts)

    return "/".join(part for part in path.split("/") if part)


def build_stream_urls(stream_path):
    clean_path = sanitize_path(stream_path)
    if not clean_path:
        return {"stream_embed_url": "", "stream_hls_url": ""}

    encoded_path = quote(clean_path, safe="/")
    return {
        "stream_embed_url": f"{MEDIAMTX_WEBRTC_BASE}/{encoded_path}?controls=true&muted=true&autoplay=true&playsInline=true",
        "stream_hls_url": f"{MEDIAMTX_HLS_BASE}/{encoded_path}/index.m3u8",
    }


def serialize_camera(row):
    data = dict(row)
    data["extra_data"] = parse_extra_data(data.get("extra_data"))
    data["rotation"] = float(data.get("rotation") or 0)
    urls = build_stream_urls(data.get("stream_path"))
    data.update(urls)
    data["has_stream"] = bool(data.get("stream_type") == "rtsp" and data.get("rtsp_url") and data.get("stream_path"))
    return data


def get_available_buildings(conn=None):
    close_conn = False
    if conn is None:
        conn = connect_db()
        close_conn = True

    buildings = set(DEFAULT_BUILDINGS)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT building FROM cameras")
    buildings.update(row[0] for row in cur.fetchall() if row[0] is not None)
    cur.execute("SELECT DISTINCT building FROM user_buildings")
    buildings.update(row[0] for row in cur.fetchall() if row[0] is not None)

    if close_conn:
        conn.close()

    return sorted(buildings)


def get_user(username):
    conn = connect_db(row_factory=True)
    cur = conn.cursor()
    cur.execute("SELECT username, password_hash, role FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None

    user = dict(row)
    if user["role"] == "admin":
        user["allowed_buildings"] = get_available_buildings(conn)
    else:
        cur.execute("SELECT building FROM user_buildings WHERE username=? ORDER BY building", (username,))
        user["allowed_buildings"] = [building for (building,) in cur.fetchall()]

    conn.close()
    return user


def get_current_user():
    username = session.get("username")
    if not username:
        return None
    return get_user(username)


def normalize_buildings(values):
    allowed = set(get_available_buildings())
    result = []
    for value in values or []:
        try:
            building = int(value)
        except (TypeError, ValueError):
            continue
        if building in allowed and building not in result:
            result.append(building)
    return result


def serialize_user(row):
    user = dict(row)
    if user["role"] == "admin":
        user["allowed_buildings"] = get_available_buildings()
    else:
        conn = connect_db()
        cur = conn.cursor()
        cur.execute("SELECT building FROM user_buildings WHERE username=? ORDER BY building", (user["username"],))
        user["allowed_buildings"] = [building for (building,) in cur.fetchall()]
        conn.close()
    return user


def user_can_access_building(building):
    user = get_current_user()
    if not user:
        return False
    if user["role"] == "admin":
        return True
    return building in user["allowed_buildings"]


def get_svg_candidates(building, floor):
    return [
        os.path.join(SVG_ROOT, f"Building{building}", f"Floor{floor}_G.svg"),
        os.path.join(SVG_ROOT, f"building_{building}", f"floor_{floor}.svg"),
        os.path.join(SVG_ROOT, f"Building{building}_Floor{floor}_G.svg"),
        os.path.join(SVG_ROOT, f"Corp{building}_Floor{floor}_G.svg"),
        os.path.join(SVG_ROOT, f"B{building}_F{floor}.svg"),
        os.path.join(SVG_ROOT, f"Floor{floor}_G.svg"),
    ]


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.is_json:
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or user["role"] != "admin":
            if request.is_json:
                return jsonify({"error": "forbidden"}), 403
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)

    return decorated


@app.route("/")
def index():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))


@app.route("/login")
def login_page():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    return render_template(
        "dashboard.html",
        username=user["username"],
        is_admin=user["role"] == "admin",
    )


@app.route("/admin")
@admin_required
def admin_page():
    user = get_current_user()
    return render_template("admin.html", username=user["username"])


@app.route("/api/session", methods=["GET"])
@login_required
def api_session():
    user = get_current_user()
    return jsonify(
        {
            "username": user["username"],
            "role": user["role"],
            "allowed_buildings": user["allowed_buildings"],
            "all_buildings": get_available_buildings(),
            "floors": DEFAULT_FLOORS,
        }
    )


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    user = get_user(username)

    if user and check_password_hash(user["password_hash"], password):
        session["logged_in"] = True
        session["username"] = user["username"]
        session["role"] = user["role"]
        return jsonify({"ok": True, "role": user["role"]})

    return jsonify({"ok": False, "error": "Неверный логин или пароль"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/users", methods=["GET"])
@admin_required
def get_users():
    conn = connect_db(row_factory=True)
    cur = conn.cursor()
    cur.execute("SELECT username, role FROM users ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, username")
    rows = [serialize_user(row) for row in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/users", methods=["POST"])
@admin_required
def create_user():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "viewer")
    allowed_buildings = normalize_buildings(data.get("allowed_buildings", []))

    if not username or not password:
        return jsonify({"error": "Укажите логин и пароль"}), 400
    if role not in {"admin", "viewer"}:
        return jsonify({"error": "Некорректная роль"}), 400
    if role != "admin" and not allowed_buildings:
        return jsonify({"error": "Выберите хотя бы один корпус для пользователя"}), 400

    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE username=?", (username,))
    if cur.fetchone():
        conn.close()
        return jsonify({"error": "Пользователь уже существует"}), 409

    cur.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), role),
    )
    if role != "admin":
        cur.executemany(
            "INSERT INTO user_buildings (username, building) VALUES (?, ?)",
            [(username, building) for building in allowed_buildings],
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/users/<username>", methods=["PATCH"])
@admin_required
def update_user(username):
    data = request.get_json() or {}
    role = data.get("role")
    password = data.get("password", "")
    allowed_buildings = normalize_buildings(data.get("allowed_buildings", []))

    conn = connect_db(row_factory=True)
    cur = conn.cursor()
    cur.execute("SELECT username, role FROM users WHERE username=?", (username,))
    existing = cur.fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "Пользователь не найден"}), 404

    updates = []
    values = []
    new_role = role or existing["role"]

    if new_role not in {"admin", "viewer"}:
        conn.close()
        return jsonify({"error": "Некорректная роль"}), 400

    if username == session.get("username") and new_role != "admin":
        conn.close()
        return jsonify({"error": "Нельзя снять права администратора у своей учётной записи"}), 400

    if role:
        updates.append("role=?")
        values.append(new_role)
    if password:
        updates.append("password_hash=?")
        values.append(generate_password_hash(password))

    if updates:
        values.append(username)
        cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE username=?", values)

    cur.execute("DELETE FROM user_buildings WHERE username=?", (username,))
    if new_role != "admin":
        if not allowed_buildings:
            conn.close()
            return jsonify({"error": "Выберите хотя бы один корпус для пользователя"}), 400
        cur.executemany(
            "INSERT INTO user_buildings (username, building) VALUES (?, ?)",
            [(username, building) for building in allowed_buildings],
        )

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/users/<username>", methods=["DELETE"])
@admin_required
def delete_user(username):
    if username == session.get("username"):
        return jsonify({"error": "Нельзя удалить текущую учётную запись"}), 400

    conn = connect_db()
    cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Пользователь не найден"}), 404

    cur.execute("DELETE FROM user_buildings WHERE username=?", (username,))
    cur.execute("DELETE FROM users WHERE username=?", (username,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/cameras", methods=["GET"])
@login_required
def get_cameras():
    user = get_current_user()
    requested_building = request.args.get("building", type=int)
    floor = request.args.get("floor", type=int)

    if requested_building and not user_can_access_building(requested_building):
        return jsonify({"error": "forbidden"}), 403

    conn = connect_db(row_factory=True)
    cur = conn.cursor()

    clauses = []
    params = []

    if user["role"] != "admin":
        placeholders = ",".join("?" for _ in user["allowed_buildings"]) or "NULL"
        clauses.append(f"building IN ({placeholders})")
        params.extend(user["allowed_buildings"])
    elif requested_building:
        clauses.append("building=?")
        params.append(requested_building)

    if requested_building and user["role"] != "admin":
        clauses.append("building=?")
        params.append(requested_building)

    if floor:
        clauses.append("floor=?")
        params.append(floor)

    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur.execute(f"SELECT * FROM cameras {where_clause} ORDER BY building, floor, name", params)
    rows = [serialize_camera(row) for row in cur.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/cameras", methods=["POST"])
@admin_required
def add_camera():
    data = request.get_json() or {}
    required = ["name", "building", "floor", "svg_x", "svg_y"]
    if not all(k in data and str(data.get(k)).strip() != "" for k in required):
        return jsonify({"error": "Не хватает обязательных полей (name, building, floor, svg_x, svg_y)"}), 400

    try:
        building = int(data.get("building"))
        floor = int(data.get("floor"))
        port = int(data.get("port", 80))
        rotation = float(data.get("rotation", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Корпус, этаж, порт и поворот должны быть числами"}), 400

    if building not in get_available_buildings():
        return jsonify({"error": "Некорректный корпус"}), 400
    if floor not in DEFAULT_FLOORS:
        return jsonify({"error": "Некорректный этаж"}), 400
    if not (1 <= port <= 65535):
        return jsonify({"error": "Порт должен быть в диапазоне 1..65535"}), 400

    stream_type = data.get("stream_type", "none")
    if stream_type not in {"none", "rtsp"}:
        return jsonify({"error": "Некорректный тип потока"}), 400

    rtsp_url = (data.get("rtsp_url") or "").strip()
    stream_path = sanitize_path(data.get("stream_path"))
    if stream_type == "rtsp" and (not rtsp_url or not stream_path):
        return jsonify({"error": "Для RTSP укажите RTSP URL и путь MediaMTX"}), 400

    conn = connect_db()
    cur = conn.cursor()
    cam_id = generate_camera_id(cur)

    try:
        cur.execute(
            """
            INSERT INTO cameras (
                id, name, building, floor, svg_x, svg_y, rotation, ip, port, status,
                description, location, stream_type, rtsp_url, stream_path, extra_data
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                cam_id,
                data["name"].strip(),
                building,
                floor,
                float(data["svg_x"]),
                float(data["svg_y"]),
                rotation % 360,
                (data.get("ip") or "").strip(),
                port,
                data.get("status", "offline"),
                (data.get("description") or "").strip(),
                (data.get("location") or "").strip(),
                stream_type,
                rtsp_url,
                stream_path,
                encode_extra_data(data.get("extra_data")),
            ),
        )
        conn.commit()
        return jsonify({"ok": True, "id": cam_id})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Не удалось сгенерировать уникальный ID"}), 409
    finally:
        conn.close()


@app.route("/api/cameras/<cam_id>", methods=["PATCH"])
@admin_required
def update_camera(cam_id):
    data = request.get_json() or {}
    allowed = {
        "name",
        "building",
        "floor",
        "svg_x",
        "svg_y",
        "rotation",
        "ip",
        "port",
        "status",
        "description",
        "location",
        "stream_type",
        "rtsp_url",
        "stream_path",
        "extra_data",
    }
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return jsonify({"error": "Нет полей для обновления"}), 400

    numeric_fields = {"building": int, "floor": int, "port": int, "rotation": float, "svg_x": float, "svg_y": float}
    for field, caster in numeric_fields.items():
        if field in fields:
            try:
                fields[field] = caster(fields[field])
            except (TypeError, ValueError):
                return jsonify({"error": f"Поле {field} имеет некорректное значение"}), 400

    if "building" in fields and fields["building"] not in get_available_buildings():
        return jsonify({"error": "Некорректный корпус"}), 400
    if "floor" in fields and fields["floor"] not in DEFAULT_FLOORS:
        return jsonify({"error": "Некорректный этаж"}), 400
    if "port" in fields and not (1 <= fields["port"] <= 65535):
        return jsonify({"error": "Порт должен быть в диапазоне 1..65535"}), 400
    if "rotation" in fields:
        fields["rotation"] = fields["rotation"] % 360
    if "stream_type" in fields and fields["stream_type"] not in {"none", "rtsp"}:
        return jsonify({"error": "Некорректный тип потока"}), 400
    if "stream_path" in fields:
        fields["stream_path"] = sanitize_path(fields["stream_path"])
    if "extra_data" in fields:
        fields["extra_data"] = encode_extra_data(fields["extra_data"])

    stream_type = fields.get("stream_type")
    rtsp_url = (fields.get("rtsp_url") or "").strip() if "rtsp_url" in fields else None
    stream_path = fields.get("stream_path") if "stream_path" in fields else None
    if stream_type == "rtsp":
        if rtsp_url is not None and not rtsp_url:
            return jsonify({"error": "Для RTSP укажите RTSP URL"}), 400
        if stream_path is not None and not stream_path:
            return jsonify({"error": "Для RTSP укажите путь MediaMTX"}), 400

    if "rtsp_url" in fields:
        fields["rtsp_url"] = (fields["rtsp_url"] or "").strip()
    if "ip" in fields:
        fields["ip"] = (fields["ip"] or "").strip()
    if "description" in fields:
        fields["description"] = (fields["description"] or "").strip()
    if "location" in fields:
        fields["location"] = (fields["location"] or "").strip()
    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()

    set_clause = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [cam_id]
    conn = connect_db()
    cur = conn.cursor()
    cur.execute(f"UPDATE cameras SET {set_clause} WHERE id=?", values)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/cameras/<cam_id>", methods=["DELETE"])
@admin_required
def delete_camera(cam_id):
    conn = connect_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM cameras WHERE id=?", (cam_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/cameras/<cam_id>/toggle", methods=["POST"])
@admin_required
def toggle_status(cam_id):
    conn = connect_db(row_factory=True)
    cur = conn.cursor()
    cur.execute("SELECT status FROM cameras WHERE id=?", (cam_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Камера не найдена"}), 404
    new_status = "offline" if row["status"] == "online" else "online"
    cur.execute("UPDATE cameras SET status=? WHERE id=?", (new_status, cam_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": new_status})


@app.route("/api/cameras/check-statuses", methods=["POST"])
@login_required
def check_all_statuses():
    conn = connect_db(row_factory=True)
    cur = conn.cursor()
    cur.execute("SELECT id, ip, port FROM cameras")
    cams = [dict(row) for row in cur.fetchall()]

    if not cams:
        conn.close()
        return jsonify({"ok": True, "checked": 0, "online": 0, "offline": 0})

    workers = min(100, max(10, len(cams)))

    def probe(cam):
        ip = (cam.get("ip") or "").strip()
        port = cam.get("port") or 80
        is_online = bool(ip) and check_camera_http(ip, int(port))
        return cam["id"], "online" if is_online else "offline"

    with ThreadPoolExecutor(max_workers=workers) as executor:
        statuses = list(executor.map(probe, cams))

    cur.executemany("UPDATE cameras SET status=? WHERE id=?", [(status, cam_id) for cam_id, status in statuses])
    conn.commit()
    conn.close()

    online_count = sum(1 for _, status in statuses if status == "online")
    offline_count = len(statuses) - online_count

    return jsonify(
        {
            "ok": True,
            "checked": len(statuses),
            "online": online_count,
            "offline": offline_count,
        }
    )


@app.route("/api/svg/<int:building>/<int:floor>")
@login_required
def get_svg(building, floor):
    if floor not in DEFAULT_FLOORS:
        return jsonify({"error": "Некорректный этаж"}), 400
    if not user_can_access_building(building):
        return jsonify({"error": "forbidden"}), 403

    for svg_path in get_svg_candidates(building, floor):
        if os.path.exists(svg_path):
            with open(svg_path, encoding="utf-8") as file:
                return Response(file.read(), mimetype="image/svg+xml")

    return Response(
        f"""
        <svg viewBox="0 0 800 500" xmlns="http://www.w3.org/2000/svg">
          <rect width="800" height="500" fill="#0d1322"/>
          <text x="400" y="250" text-anchor="middle" fill="#4a6080" font-size="16" font-family="monospace">
            SVG не найден для корпуса {building}, этаж {floor}
          </text>
        </svg>
        """.strip(),
        mimetype="image/svg+xml",
    )


init_db()


if __name__ == "__main__":
    app.run(debug=True, port=5001)
