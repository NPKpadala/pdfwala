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
    from app.routes.pdf_routes    import pdf_bp
    from app.routes.office_routes import office_bp
    from app.routes.image_routes  import image_bp
    from app.routes.system_routes import system_bp

    app.register_blueprint(pdf_bp)
    app.register_blueprint(office_bp)
    app.register_blueprint(image_bp)
    app.register_blueprint(system_bp)

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
    application.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=application.config["DEBUG"],
    )
