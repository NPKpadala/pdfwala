"""
app/routes/catalog_routes.py — public, read-only Platform Catalog API.

Serves the compiled Single Source of Truth so websites, the future mobile app,
external integrations and an admin panel all consume the SAME data:

  GET /api/catalog             full nav (modules -> categories -> tools) + version
  GET /api/catalog/version     schema + module versions + build sha
  GET /api/catalog/modules     module metadata
  GET /api/catalog/categories  categories per module
  GET /api/catalog/tools       all tool cards (?module=pdf to filter)
  GET /api/catalog/tools/<slug> one full tool definition
  GET /api/catalog/search?q=   platform-wide search across every module
"""
from flask import Blueprint, jsonify, request

from catalog.registry import registry

catalog_bp = Blueprint("catalog", __name__, url_prefix="/api/catalog")


def _cache(resp):
    # Catalog changes only on deploy; let clients/CDN cache briefly.
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@catalog_bp.route("")
@catalog_bp.route("/")
def catalog():
    return _cache(jsonify({"version": registry.version,
                           "modules": registry.get_modules()}))


@catalog_bp.route("/version")
def version():
    return _cache(jsonify(registry.version))


@catalog_bp.route("/modules")
def modules():
    out = [{"key": k, "label": v.get("label"), "icon": v.get("icon"),
            "order": v.get("order"), "manifest_version": v.get("manifest_version"),
            "tool_count": len(registry.module(k))}
           for k, v in sorted(registry.modules.items(), key=lambda kv: kv[1].get("order", 99))]
    return _cache(jsonify({"modules": out}))


@catalog_bp.route("/categories")
def categories():
    out = {k: v.get("categories", {}) for k, v in registry.modules.items()}
    return _cache(jsonify({"categories": out}))


@catalog_bp.route("/tools")
def tools():
    mod = request.args.get("module")
    ts = registry.module(mod) if mod else registry.all()
    cards = [registry._card(t) for t in ts if not t.get("hidden")]
    return _cache(jsonify({"count": len(cards), "tools": cards}))


@catalog_bp.route("/tools/<slug>")
def tool(slug):
    t = registry.get_tool(slug)
    if not t:
        return jsonify({"error": "tool not found"}), 404
    return _cache(jsonify(t))


@catalog_bp.route("/search")
def search():
    q = (request.args.get("q") or "").lower().strip()
    if not q:
        return jsonify({"query": q, "count": 0, "results": []})
    results = []
    for i in registry.get_search_index():
        if (q in i["display_name"].lower() or q in i["slug"]
                or any(q in k for k in i["keywords"])):
            results.append(i)
    return _cache(jsonify({"query": q, "count": len(results), "results": results}))
