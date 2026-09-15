#!/bin/bash
cd /opt/ipdftoo/apps/api
exec /opt/ipdftoo/apps/api/.venv/bin/gunicorn app.main:app -k uvicorn.workers.UvicornWorker --bind 127.0.0.1:8000 --workers 1 --timeout 300
