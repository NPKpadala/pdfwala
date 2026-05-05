"""
app.py — PDFWala Enterprise V13.0
Flask application factory. Max 50 lines of logic.
"""

import logging
import os

from flask import Flask
from flask_cors import CORS

from config import get_config, Config


def create_app(env: str = None) -> Flask:
    cfg = get_config(env)
    cfg.init_dirs()

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
    from app.routes.alias_routes  import alias_bp

    app.register_blueprint(pdf_bp)
    app.register_blueprint(office_bp)
    app.register_blueprint(image_bp)
    app.register_blueprint(system_bp)
    # Flat /api/* aliases used by the static frontend (fixes 404s on
    # /api/compress, /api/pdf-to-word, /api/word-to-pdf, etc.)
    app.register_blueprint(alias_bp)

    # ── Global error handlers ────────────────────────────────────────────
    from core.exceptions import PDFWalaError
    from core.result import Result

    @app.errorhandler(PDFWalaError)
    def handle_pdfwala_error(ex):
        return Result.error(ex.message, ex.http_code)

    @app.errorhandler(413)
    def handle_too_large(_):
        return Result.error(
            f"File too large. Maximum is "
            f"{cfg.MAX_CONTENT_LENGTH // (1024*1024)} MB.", 413
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
