"""Misc page routes — reserved for future functions (Task 4, TBD)."""
from flask import Blueprint, jsonify

bp = Blueprint("misc", __name__)


@bp.route("/api/misc/status")
def misc_status():
    return jsonify({"status": "not_implemented", "message": "TBD"})
