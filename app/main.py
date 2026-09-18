"""FastAPI 服务：HTTP 内容协商（Accept）调试器。"""

from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .negotiation import (
    AcceptSyntaxError,
    AcceptRange,
    CandidateResult,
    SPECIFICITY_NAMES,
    audit_accept,
    negotiate,
)

logger = logging.getLogger("content-negotiation")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器启动初始化：仅记录就绪日志；依赖安装已在镜像构建阶段完成。
    logger.info("内容协商调试服务已就绪：POST /negotiate, POST /audit-accept, POST /compare-accept")
    yield
    logger.info("内容协商调试服务关闭")


app = FastAPI(
    title="HTTP 内容协商调试器",
    description="调试 Accept 头与候选表示之间的匹配、q 值与排序过程；支持静态审计与双 Accept 对比。",
    version="1.0.0",
    lifespan=lifespan,
)


class CandidateIn(BaseModel):
    media_type: str = Field(..., description="具体的候选媒体类型，如 application/json")
    server_priority: float = Field(
        0.0, description="服务端优先级，数值越大越优先"
    )


class NegotiateRequest(BaseModel):
    accept: str = Field(..., description="原始 Accept 头字符串")
    candidates: list[CandidateIn] = Field(..., description="候选表示列表（有序）")


class SelectedOut(BaseModel):
    index: int
    media_type: str
    server_priority: float
    matched_range: str
    q: float
    specificity: int
    specificity_name: str


class NegotiateResponse(BaseModel):
    selected: SelectedOut
    candidates: list[dict]
    parsed_accept: list[dict]


class RangeHitOut(BaseModel):
    index: int
    range: str
    q: float
    specificity: int
    specificity_name: str
    effective: bool


class ConflictOut(BaseModel):
    specificity: int
    specificity_name: str
    ranges: list[dict]


class AuditCandidateOut(BaseModel):
    index: int
    media_type: str
    server_priority: float
    eligible: bool
    eliminated_reason: str | None
    effective_range: dict | None
    matched_ranges: list[RangeHitOut]
    conflicts: list[ConflictOut]


class AuditResponse(BaseModel):
    candidates: list[AuditCandidateOut]
    shadowed: list[dict]
    parsed_accept: list[dict]


class CompareRequest(BaseModel):
    baseline_accept: str = Field(..., description="基线 Accept 头字符串")
    trial_accept: str = Field(..., description="试验 Accept 头字符串")
    candidates: list[CandidateIn] = Field(
        ..., description="两侧共用的候选表示列表（有序）"
    )


class CompareSideOut(BaseModel):
    status: str = Field(
        ..., description="ok 或 not_acceptable（对应该侧单独协商时的 406 结果）"
    )
    selected: SelectedOut | None
    candidates: list[dict]
    parsed_accept: list[dict]


class CompareCandidateSideOut(BaseModel):
    q: float | None
    specificity: int | None
    specificity_name: str | None
    matched_range: str | None
    eligible: bool
    eliminated_reason: str | None


class CompareCandidateOut(BaseModel):
    index: int
    media_type: str
    server_priority: float
    baseline: CompareCandidateSideOut
    trial: CompareCandidateSideOut
    changed: bool = Field(..., description="该候选的两侧评估结果是否不一致")


class CompareResponse(BaseModel):
    selection_changed: bool = Field(
        ..., description="两侧最终选定项是否不同（含一侧无可选候选的情形）"
    )
    baseline: CompareSideOut
    trial: CompareSideOut
    candidates: list[CompareCandidateOut]


class ErrorResponse(BaseModel):
    error: str
    detail: str


def _bad_request(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "bad_request", "detail": message},
    )


@app.exception_handler(RequestValidationError)
async def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # 请求体结构/类型错误统一返回 400（非法请求语法）。
    return JSONResponse(
        status_code=400,
        content={"error": "bad_request", "detail": "请求体格式非法", "errors": exc.errors()},
    )


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "http-content-negotiation-debugger",
        "docs": "/docs",
        "endpoints": ["POST /negotiate", "POST /audit-accept", "POST /compare-accept"],
    }


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}


def _accept_to_dict(r: AcceptRange) -> dict:
    return {
        "index": r.index,
        "range": r.raw,
        "type": r.type,
        "subtype": r.subtype,
        "q": r.q,
        "specificity": r.specificity,
        "specificity_name": SPECIFICITY_NAMES[r.specificity],
    }


def _selected_to_dict(r: CandidateResult) -> dict:
    return {
        "index": r.index,
        "media_type": r.media_type,
        "server_priority": r.server_priority,
        "matched_range": r.matched_range,
        "q": r.q,
        "specificity": r.specificity,
        "specificity_name": r.specificity_name,
    }


def _candidate_side_view(r: CandidateResult) -> dict:
    """对比视图中单侧的候选评估结果。"""
    return {
        "q": r.q,
        "specificity": r.specificity,
        "specificity_name": r.specificity_name,
        "matched_range": r.matched_range,
        "eligible": r.eligible,
        "eliminated_reason": r.eliminated_reason,
    }


def _compare_side_payload(
    selected_index: int | None,
    results: list[CandidateResult],
    accept_ranges: list[AcceptRange],
) -> dict:
    """单侧协商结果：与 /negotiate 响应同形，406 时 selected 为 null。"""
    return {
        "status": "ok" if selected_index is not None else "not_acceptable",
        "selected": (
            _selected_to_dict(results[selected_index])
            if selected_index is not None
            else None
        ),
        "candidates": [r.to_dict() for r in results],
        "parsed_accept": [_accept_to_dict(r) for r in accept_ranges],
    }


def _validate_candidates(
    payload: NegotiateRequest | CompareRequest,
) -> JSONResponse | None:
    # 业务层校验：候选列表不能为空，server_priority 必须为有限数。
    if not payload.candidates:
        return _bad_request("candidates 不能为空")
    for i, cand in enumerate(payload.candidates):
        if not math.isfinite(cand.server_priority):
            return _bad_request(f"candidates[{i}].server_priority 必须是有限数")
    return None


@app.post(
    "/negotiate",
    response_model=NegotiateResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Accept 头或候选非法"},
        406: {"model": ErrorResponse, "description": "无可用候选表示"},
    },
)
async def do_negotiate(payload: NegotiateRequest):
    invalid = _validate_candidates(payload)
    if invalid is not None:
        return invalid

    raw_candidates = [(c.media_type, c.server_priority) for c in payload.candidates]
    try:
        selected_index, results, accept_ranges = negotiate(
            payload.accept, raw_candidates
        )
    except AcceptSyntaxError as exc:
        return _bad_request(str(exc))

    candidate_payload = [r.to_dict() for r in results]

    if selected_index is None:
        # q=0 排除或无范围命中：协商失败。
        return JSONResponse(
            status_code=406,
            content={
                "error": "not_acceptable",
                "detail": "没有可接受的候选表示",
                "candidates": candidate_payload,
                "parsed_accept": [_accept_to_dict(r) for r in accept_ranges],
            },
        )

    chosen = results[selected_index]
    return {
        "selected": _selected_to_dict(chosen),
        "candidates": candidate_payload,
        "parsed_accept": [_accept_to_dict(r) for r in accept_ranges],
    }


@app.post(
    "/audit-accept",
    response_model=AuditResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Accept 头或候选非法"},
        406: {"model": ErrorResponse, "description": "无可用候选表示"},
    },
)
async def do_audit_accept(payload: NegotiateRequest):
    # 静态审计：与 /negotiate 相同的校验与匹配语义，但不返回单一选定项。
    invalid = _validate_candidates(payload)
    if invalid is not None:
        return invalid

    raw_candidates = [(c.media_type, c.server_priority) for c in payload.candidates]
    try:
        audits, shadowed, accept_ranges = audit_accept(payload.accept, raw_candidates)
    except AcceptSyntaxError as exc:
        return _bad_request(str(exc))

    body = {
        "candidates": [a.to_dict() for a in audits],
        "shadowed": [_accept_to_dict(r) for r in shadowed],
        "parsed_accept": [_accept_to_dict(r) for r in accept_ranges],
    }

    if not any(a.eligible for a in audits):
        # 与 /negotiate 一致：无可用候选（全部 q=0 或无命中）时返回 406，
        # 响应体仍带完整审计明细。
        return JSONResponse(
            status_code=406,
            content={
                "error": "not_acceptable",
                "detail": "没有可接受的候选表示",
                **body,
            },
        )
    return body


@app.post(
    "/compare-accept",
    response_model=CompareResponse,
    responses={
        400: {"model": ErrorResponse, "description": "任一 Accept 头或候选非法"},
    },
)
async def do_compare_accept(payload: CompareRequest):
    # 双 Accept 对比：同一组候选分别按 baseline/trial 协商，定位规则调整的影响。
    invalid = _validate_candidates(payload)
    if invalid is not None:
        return invalid

    raw_candidates = [(c.media_type, c.server_priority) for c in payload.candidates]

    sides: dict[str, tuple[int | None, list[CandidateResult], list[AcceptRange]]] = {}
    for name, accept in (
        ("baseline", payload.baseline_accept),
        ("trial", payload.trial_accept),
    ):
        try:
            sides[name] = negotiate(accept, raw_candidates)
        except AcceptSyntaxError as exc:
            return _bad_request(f"{name}_accept: {exc}")

    baseline_selected, baseline_results, baseline_ranges = sides["baseline"]
    trial_selected, trial_results, trial_ranges = sides["trial"]

    # 按候选 index 对齐两侧评估结果，逐字段对比。
    rows = []
    for baseline_r, trial_r in zip(baseline_results, trial_results):
        baseline_view = _candidate_side_view(baseline_r)
        trial_view = _candidate_side_view(trial_r)
        rows.append(
            {
                "index": baseline_r.index,
                "media_type": baseline_r.media_type,
                "server_priority": baseline_r.server_priority,
                "baseline": baseline_view,
                "trial": trial_view,
                "changed": baseline_view != trial_view,
            }
        )

    # 某一侧无可接受候选时，该侧保留 406 结果（status/selected=None）与候选明细，
    # 整体对比不受影响，因此输入合法时固定返回 200。
    return {
        "selection_changed": baseline_selected != trial_selected,
        "baseline": _compare_side_payload(
            baseline_selected, baseline_results, baseline_ranges
        ),
        "trial": _compare_side_payload(trial_selected, trial_results, trial_ranges),
        "candidates": rows,
    }
