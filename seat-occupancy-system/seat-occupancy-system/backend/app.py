"""
app.py
Flask web application for the Seat Occupancy System.

Routes:
  GET  /login          -> login page
  POST /login          -> authenticate
  GET  /logout         -> clear session
  GET  /                -> dashboard (login required)
  GET  /video_feed      -> MJPEG live annotated camera view (login required)
  GET  /api/status       -> JSON: live per-chair occupied/empty state
  GET  /api/sessions      -> JSON: recent completed sit-sessions
  GET  /api/analytics      -> JSON: summary stats for charts

Run:
  python app.py                # webcam index 0
  VIDEO_SOURCE=demo.mp4 python app.py
"""

import os
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response, flash
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required, current_user
)
from werkzeug.security import check_password_hash

import database as db
from detector import OccupancyEngine

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]


@login_manager.user_loader
def load_user(user_id):
    row = db.get_user_by_id(int(user_id))
    return User(row) if row else None


# ---- video engine setup -------------------------------------------------
VIDEO_SOURCE = os.environ.get("VIDEO_SOURCE", "0")
VIDEO_SOURCE = int(VIDEO_SOURCE) if VIDEO_SOURCE.isdigit() else VIDEO_SOURCE
MODEL_PATH = os.environ.get("MODEL_PATH", "yolov8n.pt")

engine = OccupancyEngine(source=VIDEO_SOURCE, model_path=MODEL_PATH)


# ---- auth routes ----------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = db.get_user(username)
        if row and check_password_hash(row["password_hash"], password):
            login_user(User(row))
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---- dashboard --------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", username=current_user.username)


@app.route("/video_feed")
@login_required
def video_feed():
    def generate():
        import time
        while True:
            frame = engine.get_jpeg()
            if frame is not None:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
            time.sleep(0.1)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ---- JSON API -----------------------------------------------------------
@app.route("/api/status")
@login_required
def api_status():
    live = engine.get_state()
    live_by_id = {c["chair_id"]: c["occupied"] for c in live}
    rows = db.get_live_status()
    for r in rows:
        if r["chair_id"] in live_by_id:
            r["occupied"] = live_by_id[r["chair_id"]]
    return jsonify(rows)


@app.route("/api/sessions")
@login_required
def api_sessions():
    limit = int(request.args.get("limit", 200))
    chair_id = request.args.get("chair_id", type=int)
    return jsonify(db.get_sessions(limit=limit, chair_id=chair_id))


@app.route("/api/analytics")
@login_required
def api_analytics():
    return jsonify(db.get_analytics_summary())


if __name__ == "__main__":
    db.init_db()
    engine.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
