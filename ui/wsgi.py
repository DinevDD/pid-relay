"""WSGI entry point. The UI is mounted under PIDRELAY_UI_BASE (default /pid-manager)."""
import os

from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response

from .app import create_app

base = os.environ.get("PIDRELAY_UI_BASE", "/pid-manager").rstrip("/")
_app = create_app()
_app.wsgi_app = ProxyFix(_app.wsgi_app, x_for=1, x_proto=1, x_host=1)
application = DispatcherMiddleware(
    lambda env, start: Response("Not here. See " + base + "/", status=404)(env, start),
    {base: _app.wsgi_app},
)
