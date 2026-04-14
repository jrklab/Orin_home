"""Robot page routes — reserved for LeKiwi robot control (Task 3)."""
from flask import Blueprint, jsonify

bp = Blueprint("robot", __name__)


@bp.route("/api/robot/status")
def robot_status():
    return jsonify({"status": "not_implemented", "message": "LeKiwi control coming in Task 3"})
