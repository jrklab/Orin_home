"""Shared Flask-SocketIO instance.

Created here (not in app.py) so any module can import it without
triggering a circular import through app.py.
"""
from flask_socketio import SocketIO

socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")
