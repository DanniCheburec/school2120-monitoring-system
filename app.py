from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3
import os
import json
from functools import wraps

app = Flask(__name__)
app.secret_key = "camera_system_secret_2120"



DB_PATH = "cameras.db"


ADMIN_CREDENTIALS = {
    "admin": "school2120",          
    "security": "camera_pass",      
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cameras (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            floor       INTEGER NOT NULL,
            svg_x       REAL NOT NULL,
            svg_y       REAL NOT NULL,
            status      TEXT DEFAULT 'online',
            description TEXT DEFAULT '',
            location    TEXT DEFAULT '',
            extra_data  TEXT DEFAULT '{}'
        )
    """)

#Декоратор авторизации 
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.is_json:
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

# Страницы
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
    return render_template("dashboard.html", username=session.get("username"))

@app.route("/admin")
@login_required
def admin_page():
    return render_template("admin.html", username=session.get("username"))


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if ADMIN_CREDENTIALS.get(username) == password:
        session["logged_in"] = True
        session["username"] = username
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Неверный логин или пароль"}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/cameras", methods=["GET"])
@login_required
def get_cameras():
    floor = request.args.get("floor", type=int)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if floor:
        cur.execute("SELECT * FROM cameras WHERE floor=? ORDER BY name", (floor,))
    else:
        cur.execute("SELECT * FROM cameras ORDER BY floor, name")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route("/api/cameras", methods=["POST"])
@login_required
def add_camera():
    data = request.get_json()
    required = ["id", "name", "floor", "svg_x", "svg_y"]
    if not all(k in data for k in required):
        return jsonify({"error": "Не хватает полей"}), 400
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO cameras (id, name, floor, svg_x, svg_y, status, description, location, extra_data)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            data["id"], data["name"], int(data["floor"]),
            float(data["svg_x"]), float(data["svg_y"]),
            data.get("status", "online"),
            data.get("description", ""),
            data.get("location", ""),
            json.dumps(data.get("extra_data", {}))
        ))
        conn.commit(); conn.close()
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"error": "ID уже существует"}), 409

@app.route("/api/cameras/<cam_id>", methods=["PATCH"])
@login_required
def update_camera(cam_id):
    data = request.get_json()
    allowed = ["name", "floor", "svg_x", "svg_y", "status", "description", "location", "extra_data"]
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return jsonify({"error": "Нет полей для обновления"}), 400
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [cam_id]
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"UPDATE cameras SET {set_clause} WHERE id=?", values)
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/cameras/<cam_id>", methods=["DELETE"])
@login_required
def delete_camera(cam_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM cameras WHERE id=?", (cam_id,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/cameras/<cam_id>/toggle", methods=["POST"])
@login_required
def toggle_status(cam_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT status FROM cameras WHERE id=?", (cam_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Камера не найдена"}), 404
    new_status = "offline" if row["status"] == "online" else "online"
    cur.execute("UPDATE cameras SET status=? WHERE id=?", (new_status, cam_id))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "status": new_status})


@app.route("/api/svg/<int:floor>")
@login_required
def get_svg(floor):
    svg_path = f"C:/Users/dania/Documents/School_Project/Floor{floor}_G.svg"   # путь к SVG 
    
    with open(svg_path, encoding="utf-8") as f:
        svg = f.read()
    return svg, 200, {"Content-Type": "image/svg+xml"}

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)

    
