"""
wsgi.py — PDFWala Enterprise V14.0
Flask application factory + WSGI entry point.

V14 FIX:
  - Config.validate() called after app creation (not at import time)
    to avoid blocking startup when env vars not set in dev mode
  - PIL MAX_IMAGE_PIXELS set here too for gunicorn preload_app=True
"""

import logging
import os

from flask import Flask
from flask_cors import CORS

from config import get_config, Config


def create_app(env: str = None) -> Flask:
    cfg = get_config(env)
    cfg.init_dirs()
    cfg.validate()   # warn in dev, raise in prod

    # FIX V14: Set PIL bomb guard globally for gunicorn preload_app workers
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = cfg.MAX_IMAGE_PIXELS
    except ImportError:
        pass

    app = Flask(__name__, static_folder="static")
    app.config.from_object(cfg)

    # ── CORS ────────────────────────────────────────────────────────────
    CORS(app, origins=cfg.CORS_ORIGINS)

    # ── Logging ─────────────────────────────────────────────────────────
    logging.basicConfig(
        level=logging.DEBUG if cfg.DEBUG else logging.INFO,
        format="%(message)s",
    )

    # ── Register engines (populates Pipeline registry) ───────────────────
    import engines.pdf_engine      # noqa: F401
    import engines.office_engine   # noqa: F401
    import engines.image_engine    # noqa: F401

    # ── Register blueprints ─────────────────────────────────────────────
    from app.routes.pdf_routes     import pdf_bp        # special PDF endpoints only
    from app.routes.system_routes  import system_bp
    from app.routes.catalog_routes import catalog_bp
    from app.routes.tool_factory   import register_catalog_routes

    # Every standard tool route for every module is generated from the catalog.
    register_catalog_routes(app)

    app.register_blueprint(pdf_bp)      # canvas + text-editor specials
    app.register_blueprint(system_bp)
    app.register_blueprint(catalog_bp)

    # ── Unique-visitor tracking (best-effort, never breaks a request) ────
    # Counts distinct client IPs per day via Redis HyperLogLog. Internal
    # probes and static/download traffic are excluded so the number reflects
    # real site visitors. Surfaced at GET /metrics/visitors for the ops monitor.
    from flask import request as _request
    from services.visitors import record_visit

    _VISITOR_SKIP_PREFIXES = (
        "/health", "/ready", "/live", "/metrics", "/download",
        "/api/v1/health", "/api/v1/ready", "/api/v1/live",
    )

    @app.before_request
    def _track_visitor():
        try:
            path = _request.path or ""
            if _request.method == "OPTIONS" or path.startswith(_VISITOR_SKIP_PREFIXES):
                return
            ip = (
                _request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or _request.remote_addr
                or ""
            )
            record_visit(ip)
        except Exception:
            pass  # tracking must never affect the response

    # ── Global error handlers ────────────────────────────────────────────
    from core.exceptions import PDFWalaError
    from core.result import Result

    @app.errorhandler(PDFWalaError)
    def handle_pdfwala_error(ex):
        return Result.error(ex.message, ex.http_code)

    @app.errorhandler(413)
    def handle_too_large(_):
        # Show the user-facing cap (MAX_FILE_SIZE), not the buffered request
        # cap (MAX_CONTENT_LENGTH = MAX_FILE_SIZE + multipart overhead).
        max_mb = cfg.MAX_FILE_SIZE // (1024 * 1024)
        return Result.error(
            f"File too large. Maximum size is {max_mb}MB for free use — "
            f"try Compress PDF first, or use Split PDF to break it into "
            f"smaller pieces.", 413
        )

    @app.errorhandler(404)
    def handle_404(_):
        return Result.error("Endpoint not found", 404)

    @app.errorhandler(500)
    def handle_500(ex):
        return Result.error("Internal server error", 500)

    return app


application = create_app()

if __name__ == "__main__":
    # Direct `python wsgi.py` invocation is a dev convenience only.
    # We deliberately do NOT forward `debug=` from config: the Werkzeug
    # interactive debugger is RCE-equivalent. Opt in EXPLICITLY by setting
    # FLASK_DEBUG=1 in your shell when you actually want it.
    application.run(
        host="127.0.0.1",     # local-only — never bind 0.0.0.0 in this code path
        port=int(os.getenv("PORT", 5000)),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
