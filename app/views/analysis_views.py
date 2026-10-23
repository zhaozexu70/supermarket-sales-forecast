# -*- coding: utf-8 -*-
"""销售数据分析展示：页面，以及给图表提供数据的 JSON 接口"""
from flask import Blueprint, abort, jsonify, render_template, request

from app.constants import CATEGORIES, REGIONS
from app.services import analysis_service

bp = Blueprint("analysis", __name__)


@bp.route("/analysis")
def index():
    """页面本身只有筛选条件和 8 个空的图表框，图表的数据由页面里的脚本向下面的接口要"""
    f = analysis_service.clean_filters(request.args)
    return render_template("analysis.html", f=f, stale=analysis_service.stale_years(f),
                           regions=REGIONS, categories=CATEGORIES, charts=list(analysis_service.CHARTS))


@bp.route("/api/analysis/<name>")
def chart_data(name):
    """图表数据接口，比如 /api/analysis/trend?start=2021&end=2024&region=华东，返回 JSON"""
    if name not in analysis_service.CHARTS:
        abort(404)
    f = analysis_service.clean_filters(request.args)
    if f["start"] is None:
        return jsonify(empty=True)
    return jsonify(analysis_service.CHARTS[name](f))
