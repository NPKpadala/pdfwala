"""
app/routes/tool_factory.py — Universal route factory + tool handler.

Every standard tool endpoint for every module is generated here from the
compiled catalog. There is ONE handler pipeline and ONE registration loop, so:

    add a module = create catalog/manifest/<module>.yaml + implement the engine
                   + run catalog/build.py   ->  its routes appear automatically.

Only genuinely special endpoints (canvas, text-editor, health, download, ...)
stay hand-written in their own blueprints.

Handler pipeline (identical for every generated route):
    rate-limit -> build context -> save upload(s) -> resolve output ext
    -> sync/async dispatch -> Pipeline -> engine -> Result
"""
import logging

from flask import Blueprint, request

from app.controllers.job_controller import JobController
from catalog.registry import registry
from core.exceptions import ValidationError
from core.result import Result
from services.file_service import file_service

log = logging.getLogger("pdfwala.routes.factory")

# Unified op -> Celery task map, merged from every module's task registry.
# PDF tasks are manifest-generated; image/office are sync (task unused for
# dispatch) but merged so the handler is uniform and future-proof.
_TASK_MAP = {}


def _load_task_map():
    m = {}
    try:
        from tasks.pdf_tasks import PDF_TASK_MAP
        m.update(PDF_TASK_MAP)
    except Exception as ex:
        log.warning("pdf task map unavailable: %s", ex)
    for mod, attr in (("image", "IMAGE_TASK_MAP"), ("office", "OFFICE_TASK_MAP")):
        try:
            mapping = getattr(__import__("tasks.%s_tasks" % mod, fromlist=[attr]), attr)
            m.update(mapping)
        except Exception as ex:
            log.warning("%s task map unavailable: %s", mod, ex)
    return m


def _handle(spec):
    """The one and only tool handler. `spec` comes from registry.get_routes()."""
    rl = JobController.check_rate_limit(request)
    if rl:
        return rl
    ctx = JobController.build_ctx(request, spec["op"])
    try:
        if spec["multi"]:
            size = file_service.save_multiple(request, ctx, spec["field"])
        else:
            size = file_service.save_single(request, ctx, spec["field"])
    except ValidationError as ex:
        return Result.error(ex.message, 400)

    # Output extension may be dynamic (e.g. image convert picks the target format).
    output_ext = spec["output_ext"]
    if spec.get("output_ext_from"):
        output_ext = request.form.get(spec["output_ext_from"], output_ext)

    task_fn = _TASK_MAP.get(spec["op"])
    force_async = spec["force_async"]
    if task_fn is None and (force_async or file_service.is_async(size)):
        return Result.error(
            f"No async task is registered for '{spec['op']}'. This operation "
            "cannot be processed right now.", 501,
        )
    return JobController.run_or_enqueue(
        ctx, size, task_fn, output_ext, spec["success_msg"], force_async=force_async,
    )


def register_catalog_routes(app):
    """Create one blueprint per module and register every tool route from the
    catalog. Returns the number of routes registered."""
    global _TASK_MAP
    _TASK_MAP = _load_task_map()
    total = 0
    for mod in sorted(registry.modules):
        bp = Blueprint("tools_%s" % mod, __name__, url_prefix="/api/%s" % mod)
        for spec in registry.get_routes(mod):
            def _view(_s=spec):
                return _handle(_s)
            _view.__name__ = "t_" + spec["op"]
            bp.add_url_rule(spec["route"], endpoint="t_" + spec["op"],
                            view_func=_view, methods=["POST"])
            total += 1
        app.register_blueprint(bp)
    log.info("route factory: registered %d tool routes across %d modules",
             total, len(registry.modules))
    return total
