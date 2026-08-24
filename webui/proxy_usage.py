"""跨任务、跨请求的全局代理池租借统计。"""
from __future__ import annotations

import logging
from collections import defaultdict

from . import db

logger = logging.getLogger("proxy_usage")


TASK_LABELS = {
    "register": "注册任务",
    "login": "登录任务",
    "quota": "额度查询",
    "candidate_join": "候选申请加入",
    "other": "其他任务",
}

DETAIL_LABELS = {
    "auto_register": "全自动注册",
    "auto_login": "批量登录/重登录",
    "workspace_credentials": "空间凭证登录",
    "workspace_401_relogin": "候选额度 401 重登录",
    "public_quota": "公开页额度巡检",
    "workspace_quota_manual": "候选额度手动查询",
    "workspace_quota_scheduled": "候选额度定时查询",
    "workspace_quota_trash_recheck": "候选垃圾箱额度复查",
    "public_401_relogin": "公开页 401 重登录",
    "candidate_join": "候选子号申请加入",
}


def _canonical_task_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in TASK_LABELS else "other"


def record_lease(proxy: str, task_type: str, task_detail: str = "") -> None:
    """记录一次真实代理池租借；统计故障不得影响业务任务。"""
    proxy_value = str(proxy or "").strip()
    if not proxy_value:
        return
    canonical_type = _canonical_task_type(task_type)
    detail = str(task_detail or "").strip().lower()
    try:
        db.record_proxy_lease_usage(proxy_value, canonical_type, detail)
    except Exception:
        logger.warning(
            "全局代理租借计数写入失败 task_type=%s task_detail=%s",
            canonical_type,
            detail,
            exc_info=True,
        )


def snapshot() -> dict:
    """返回分类总览、任务明细和逐代理累计计数。"""
    rows = db.list_proxy_lease_usage()
    category_counts = {key: 0 for key in TASK_LABELS}
    detail_counts: dict[tuple[str, str], int] = defaultdict(int)
    proxies: dict[str, dict] = {}
    total = 0
    updated_at = 0.0

    for row in rows:
        proxy = str(row.get("proxy") or "").strip()
        if not proxy:
            continue
        count = max(0, int(row.get("leased_count") or 0))
        task_type = _canonical_task_type(row.get("task_type") or "")
        detail = str(row.get("task_detail") or "").strip().lower()
        first_at = float(row.get("first_leased_at") or 0)
        last_at = float(row.get("last_leased_at") or 0)

        total += count
        category_counts[task_type] += count
        detail_counts[(task_type, detail)] += count
        updated_at = max(updated_at, last_at)

        item = proxies.setdefault(proxy, {
            "proxy": proxy,
            "leased_count": 0,
            "register": 0,
            "login": 0,
            "quota": 0,
            "candidate_join": 0,
            "other": 0,
            "first_leased_at": first_at,
            "last_leased_at": last_at,
        })
        item["leased_count"] += count
        item[task_type] += count
        if not item["first_leased_at"] or (first_at and first_at < item["first_leased_at"]):
            item["first_leased_at"] = first_at
        item["last_leased_at"] = max(item["last_leased_at"], last_at)

    try:
        started_at = float(db.get_setting("proxy_usage_since", "0") or 0)
    except (TypeError, ValueError):
        started_at = 0.0

    categories = [
        {
            "task_type": key,
            "label": label,
            "leased_count": category_counts[key],
        }
        for key, label in TASK_LABELS.items()
        if key != "other" or category_counts[key]
    ]
    details = [
        {
            "task_type": task_type,
            "task_label": TASK_LABELS[task_type],
            "task_detail": detail,
            "label": DETAIL_LABELS.get(detail, detail or TASK_LABELS[task_type]),
            "leased_count": count,
        }
        for (task_type, detail), count in detail_counts.items()
    ]
    details.sort(key=lambda item: (-item["leased_count"], item["label"]))
    proxy_rows = sorted(
        proxies.values(),
        key=lambda item: (-item["leased_count"], -item["last_leased_at"], item["proxy"]),
    )
    return {
        "persistent": True,
        "started_at": started_at,
        "updated_at": updated_at,
        "leased_count": total,
        "categories": categories,
        "details": details,
        "proxies": proxy_rows,
    }


def reset() -> dict:
    db.reset_proxy_lease_usage()
    return snapshot()
