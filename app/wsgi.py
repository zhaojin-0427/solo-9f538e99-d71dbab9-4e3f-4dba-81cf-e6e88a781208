"""WSGI 入口：gunicorn app.wsgi:app / flask --app app.wsgi run"""
from .app import create_app

app = create_app()
