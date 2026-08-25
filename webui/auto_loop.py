"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：基于近期历史租借计数（LRU 式）挑选使用次数最少的代理
  - 状态机：stopped → running → paused → running / stopped
  - 优雅暂停/停止：当前 worker 跑完才退出，不强杀
  - 复用 registrar.start_registration：每个号开一个 run，由 worker 等其结束
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Optional

from . import db, proxy_usage, registrar
from mail_providers import MailProviderError, get_provider_class

logger = logging.getLogger("auto_loop")


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


def _parse_proxy_pool(text: str) -> list[str]:
    """把多行代理字符串拆成列表。空行 / # 开头注释跳过。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _login_context_key(options: dict) -> str:
    """登录任务按空间和凭证策略划分上下文。

    ``ensure_credentials`` 会改变任务的副作用：公开“仅登录”任务可以为
    已有密码、但缺少 TOTP 的账号补齐 2FA；空间凭证刷新任务必须只刷新
    token。两者不能共用同一条队列，否则后到的请求可能被并入前一个任务并沿用
    错误的策略。把策略编码进 key，既保留同一策略的队列合并，也让两类任务
    可以在不同控制器中独立排队。
    """
    value = (options or {}).get("workspace_db_id")
    if value not in (None, ""):
        base = f"workspace:{value}"
    else:
        value = (options or {}).get("workspace_id")
        if value not in (None, ""):
            base = f"workspace_external:{value}"
        else:
            base = "personal"
    suffix = "ensure" if bool((options or {}).get("ensure_credentials", True)) else "refresh"
    if bool((options or {}).get("login_no_rt_only")):
        suffix = f"{suffix}:no_rt"
    return f"{base}:{suffix}"


class AutoLoopController:
    """多 worker auto-loop 控制器。

    options 关键字段：
      proxy:                单代理（兼容旧版，concurrency=1 时用）
      proxy_pool:           多代理字符串（每行一个；每个任务优先取租取次数最少者）
      concurrency:          并发 worker 数（1-20）
      cool_down_seconds:    每个 worker 跑完后冷却时间（默认 3）
      group_name:           邮箱分组；空=未分组，__all__=全部
      其余参数透传给 registrar.start_registration
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停
        # 进度统计
        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_fail = 0
        # 任务统计以“账号”为单位，而不是以 run/动作尝试为单位。
        # 一个账号在重试期间保持 pending/retrying，只有最终成功或最终失败
        # 才会增加 completed；retry_count 只统计发生过重试的不同账号。
        self._task_total: Optional[int] = None
        self._task_total_known: bool = False
        self._task_completed: int = 0
        self._retry_count: int = 0
        self._retry_attempts: int = 0
        self._account_records: dict[str, dict] = {}
        self._account_retry_used: dict[str, int] = {}
        self._registration_retry_queue: list[dict] = []
        # 当前每个 worker 在跑啥（worker_id → email）
        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        # 熔断状态
        self._consecutive_network_fails = 0
        self._circuit_break_threshold = 3
        self._last_break_reason = ""
        # SSE 订阅
        self._subscribers: list[queue.Queue] = []
        # 代理池 / 并发数
        self._proxy_pool: list[str] = []
        self._proxy_usage: list[int] = []
        self._single_proxy_usage: int = 0
        self._concurrency: int = 1
        # 仅登录模式按启动瞬间的注册结果建立队列，保证每个账号只投送一次。
        self._login_queue: list[dict] = []
        self._login_candidate_count: int = 0
        self._login_seen: set[str] = set()
        self._login_context: str = ""
        # 目标成功数：0 = 不限量（保持旧行为）；>0 时累计成功达标即自动停止
        self._target_count: int = 0
        self._account_retry_count: int = 1

    # ──────────────────────── 公共 API ────────────────────────

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                # 同一空间的后续登录请求并入正在运行的全空间任务，
                # 不再返回“已经在跑了”。不同空间/不同任务类型仍然互斥。
                incoming = dict(options or {})
                if (
                    incoming.get("login_only")
                    and self._options.get("login_only")
                    and _login_context_key(incoming) == self._login_context
                ):
                    return self._enqueue_login_locked(incoming)
                return {"ok": False, "error": f"已经在跑了 (state={self._state})"}
            # 重置
            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            # 目标成功数（0=不限量）。先解析它，后面非池化 provider 的
            # “任务总数”只能在用户给出目标时确定。
            self._target_count = max(0, int(self._options.get("target_count") or 0))
            self._task_total = None
            self._task_total_known = False
            self._task_completed = 0
            self._retry_count = 0
            self._retry_attempts = 0
            self._account_records = {}
            self._account_retry_used = {}
            self._registration_retry_queue = []
            if self._options.get("login_only"):
                try:
                    self._login_queue = db.list_login_candidates(
                        self._options.get("group_name", ""),
                        filter_rt=("no_rt" if self._options.get("login_no_rt_only") else "all"),
                        # 只有开启“补齐2FA”时，才把外部 OTP 号池行纳入仅登录
                        # 快照；该模式还要求已有 OpenAI 密码和可用 OTP 链接。
                        include_mailbox_only=bool(
                            self._options.get("ensure_credentials", True)
                        ),
                        require_2fa_inputs=bool(
                            self._options.get("ensure_credentials", True)
                        ),
                    )
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                # 注册结果页可传入精确的邮箱快照，仅重登录当前选中的账号。
                login_emails = self._options.get("login_emails")
                if login_emails:
                    wanted = {
                        str(email).strip().lower()
                        for email in login_emails
                        if str(email).strip()
                    }
                    self._login_queue = [
                        row for row in self._login_queue
                        if (row.get("email") or "").strip().lower() in wanted
                    ]
                usage_detail = str(
                    self._options.get("proxy_usage_detail") or "auto_login"
                ).strip().lower()
                for row in self._login_queue:
                    row["_proxy_usage_detail"] = usage_detail
                if not self._login_queue:
                    return {
                        "ok": False,
                        "error": (
                            "当前分组没有可登录的账号；补齐2FA要求账号已有 OpenAI 密码，"
                            "通用 OTP 导入还必须带 OTP 中转链接"
                        ),
                    }
                self._login_candidate_count = len(self._login_queue)
                self._login_seen = {
                    (row.get("email") or "").strip().lower()
                    for row in self._login_queue
                    if (row.get("email") or "").strip()
                }
                self._login_context = _login_context_key(self._options)
                self._task_total = len(self._login_queue)
                self._task_total_known = True
                for row in self._login_queue:
                    key = self._account_key(row)
                    if key:
                        self._account_records[key] = {
                            "email": row.get("email", ""),
                            "status": "pending",
                            "attempts": 0,
                            "retry_count": 0,
                        }
            else:
                self._login_queue = []
                self._login_candidate_count = 0
                self._login_seen = set()
                self._login_context = ""
                # 注册任务的初始对象数取启动瞬间的 available 快照。非池化
                # provider（例如临时地址）没有有限对象，只有目标数存在时
                # 才能显示确定的进度分母。
                mail_source = db.get_setting("mail_source", "outlook")
                try:
                    provider_cls = get_provider_class(mail_source)
                except MailProviderError as e:
                    return {"ok": False, "error": str(e)}
                if provider_cls.pooled:
                    try:
                        self._task_total = db.count_accounts(
                            status="available",
                            kind=mail_source,
                            group_name=self._options.get("group_name", ""),
                        )
                    except ValueError as e:
                        return {"ok": False, "error": str(e)}
                    self._task_total_known = True
                elif self._target_count:
                    self._task_total = self._target_count
                    self._task_total_known = True
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_fail = 0
            self._worker_status.clear()
            self._consecutive_network_fails = 0
            self._last_message = "auto-loop 启动"
            # 解析并发参数
            self._concurrency = max(1, min(20, int(self._options.get("concurrency") or 1)))
            pool_text = self._options.get("proxy_pool") or ""
            self._proxy_pool = _parse_proxy_pool(pool_text)
            # 基于近期历史的租借计数（LRU 式），而非从零开始的快照。
            # 默认统计最近 3 小时内的租借记录。
            self._proxy_history_window = float(
                self._options.get("proxy_history_window") or 10800
            )
            if self._proxy_pool:
                since = time.time() - self._proxy_history_window
                historical = db.proxy_lease_counts_since(
                    self._proxy_pool, since,
                )
                self._proxy_usage = [
                    historical.get(p, 0) for p in self._proxy_pool
                ]
                logger.info(
                    "[auto-loop] 代理池已加载历史计数 (window=%.0fs): %s",
                    self._proxy_history_window,
                    list(zip(self._proxy_pool, self._proxy_usage)),
                )
            else:
                self._proxy_usage = []
            self._single_proxy_usage = 0
            # 每个账号独立的失败重试次数；1 表示首次失败后再尝试一次。
            self._account_retry_count = max(
                0, min(10, int(self._options.get("account_retry_count", 1) or 0))
            )
            # 启 manage 线程
            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "target_count": self._target_count,
            "login_only": bool(self._options.get("login_only")),
            "ensure_credentials": bool(self._options.get("ensure_credentials", True)),
            "login_no_rt_only": bool(self._options.get("login_no_rt_only")),
            "login_candidate_count": self._login_candidate_count,
            "task_total": self._task_total,
            "task_total_known": self._task_total_known,
            "task_completed": self._task_completed,
            "retry_count": self._retry_count,
        }

    def _enqueue_login_locked(self, options: dict) -> dict:
        """将同一空间的新登录请求追加到共享队列（调用方已持有锁）。"""
        try:
            rows = db.list_login_candidates(
                options.get("group_name", ""),
                filter_rt=("no_rt" if options.get("login_no_rt_only") else "all"),
                include_mailbox_only=bool(
                    options.get("ensure_credentials", True)
                ),
                require_2fa_inputs=bool(
                    options.get("ensure_credentials", True)
                ),
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        requested = options.get("login_emails")
        if requested:
            wanted = {
                str(email).strip().lower()
                for email in requested
                if str(email).strip()
            }
            rows = [
                row for row in rows
                if (row.get("email") or "").strip().lower() in wanted
            ]
        new_rows = []
        usage_detail = str(
            options.get("proxy_usage_detail") or "auto_login"
        ).strip().lower()
        for row in rows:
            email = (row.get("email") or "").strip().lower()
            if email and email not in self._login_seen:
                self._login_seen.add(email)
                row["_proxy_usage_detail"] = usage_detail
                new_rows.append(row)
        self._login_queue.extend(new_rows)
        self._login_candidate_count += len(new_rows)
        if self._task_total_known:
            self._task_total = int(self._task_total or 0) + len(new_rows)
        for row in new_rows:
            key = self._account_key(row)
            if key:
                self._account_records.setdefault(key, {
                    "email": row.get("email", ""),
                    "status": "pending",
                    "attempts": 0,
                    "retry_count": 0,
                })
        # 首个请求的空间任务参数定义 worker 数和代理快照；后续请求只
        # 追加候选人，绝不重置代理计数器或替换代理池。
        self._last_message = (
            f"登录队列追加 {len(new_rows)} 个候选人，待处理 {len(self._login_queue)} 个"
        )
        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "queued": len(new_rows),
            "login_candidate_count": self._login_candidate_count,
            "pending": len(self._login_queue),
            "concurrency": self._concurrency,
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "已请求暂停（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._last_message = "已恢复"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "没在跑"}
            self._stop_event.set()
            self._pause_event.clear()
            self._last_message = "已请求停止（当前 worker 跑完才生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        try:
            q.put_nowait({"kind": "state", "data": self._snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try: self._subscribers.remove(q)
            except ValueError: pass

    # ──────────────────────── 内部 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            stats = db.stats()
            active_by_proxy: dict[str, list[int]] = {}
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": info.get("proxy", ""),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            for wid, info in self._worker_status.items():
                proxy = info.get("proxy", "") or ""
                if proxy:
                    active_by_proxy.setdefault(proxy, []).append(wid)

            if self._proxy_pool:
                proxy_usage = [
                    {
                        "index": i + 1,
                        "proxy": proxy,
                        "leased_count": self._proxy_usage[i],
                        "active_count": len(active_by_proxy.get(proxy, [])),
                        "active_workers": active_by_proxy.get(proxy, []),
                    }
                    for i, proxy in enumerate(self._proxy_pool)
                ]
            elif (self._options.get("proxy") or "").strip():
                single_proxy = (self._options.get("proxy") or "").strip()
                proxy_usage = [{
                    "index": 1,
                    "proxy": single_proxy,
                    "leased_count": self._single_proxy_usage,
                    "active_count": len(active_by_proxy.get(single_proxy, [])),
                    "active_workers": active_by_proxy.get(single_proxy, []),
                }]
            else:
                proxy_usage = []

            active_count = len(self._worker_status)
            retrying_count = sum(
                1 for record in self._account_records.values()
                if record.get("status") == "retrying"
            )
            task_remaining = (
                max(0, int(self._task_total or 0) - self._task_completed)
                if self._task_total_known else None
            )
            if self._task_total_known:
                if self._task_total:
                    progress_percent = round(
                        min(100.0, self._task_completed * 100.0 / self._task_total),
                        1,
                    )
                else:
                    progress_percent = (
                        100.0 if self._state == AutoLoopState.STOPPED else 0.0
                    )
            else:
                progress_percent = None
            return {
                "state": self._state,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "retry_count": self._retry_count,
                "retry_attempts": self._retry_attempts,
                "task_total": self._task_total,
                "task_total_known": self._task_total_known,
                "task_completed": self._task_completed,
                "task_in_progress": active_count,
                "task_retrying": retrying_count,
                "task_remaining": task_remaining,
                "progress_percent": progress_percent,
                "task_stats": {
                    "total": self._task_total if self._task_total_known else None,
                    "completed": self._task_completed,
                    "in_progress": active_count,
                    "success": self._registered_ok,
                    "failed": self._registered_fail,
                    "retry": self._retry_count,
                    "retry_attempts": self._retry_attempts,
                },
                "target_count": self._target_count,
                "account_retry_count": self._account_retry_count,
                "login_only": bool(self._options.get("login_only")),
                "ensure_credentials": bool(self._options.get("ensure_credentials", True)),
                "task_type": "login" if self._options.get("login_only") else "register",
                "login_candidate_count": self._login_candidate_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "proxy_pool_usage": proxy_usage,
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _set_message(self, msg: str):
        with self._lock:
            self._last_message = msg
        self._broadcast("state", self._snapshot())

    def _candidate_workspace_db_id(self) -> Optional[int]:
        """返回候选状态所属的本地空间 ID。

        登录流程的 ``workspace_id`` 是上游 Workspace UUID，不能直接
        ``int()``；空间任务另外传入 ``workspace_db_id``。兼容旧调用方
        仍传本地数字 ID 的情况。
        """
        value = self._options.get("workspace_db_id")
        if value in (None, ""):
            value = self._options.get("workspace_id")
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _proxy_for_worker(
        self,
        worker_id: int = 0,
        exclude_proxy: str = "",
        task_detail: str = "",
    ) -> str:
        """为新任务领取当前租取次数最少的代理。

        初始计数基于近期历史（LRU 式），选择和计数递增在同一把锁内完成，
        避免并发 worker 同时拿到同一个最小计数。
        """
        leased_from_pool = False
        should_record = False
        task_type = "register"
        detail = ""
        with self._lock:
            if self._proxy_pool:
                candidates = list(range(len(self._proxy_pool)))
                # 有其他地址可选时避开刚被 CF 403 的代理；只有一条时仍重新领取
                # 同一动态代理 URL，以便新 HTTP 会话触发出口 IP 轮换。
                alternatives = [
                    i for i in candidates if self._proxy_pool[i] != exclude_proxy
                ]
                if alternatives:
                    candidates = alternatives
                min_count = min(self._proxy_usage[i] for i in candidates)
                # min() 保证同计数时按代理池原始顺序稳定选择。
                index = next(i for i in candidates if self._proxy_usage[i] == min_count)
                proxy = self._proxy_pool[index]
                self._proxy_usage[index] += 1
                leased_from_pool = True
            else:
                proxy = self._options.get("proxy", "") or ""
                if proxy:
                    self._single_proxy_usage += 1
            task_type = "login" if self._options.get("login_only") else "register"
            detail = str(
                task_detail
                or self._options.get("proxy_usage_detail")
                or ("auto_login" if task_type == "login" else "auto_register")
            ).strip().lower()
            should_record = self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED)
        if leased_from_pool and should_record:
            proxy_usage.record_lease(proxy, task_type, detail)
        return proxy

    @staticmethod
    def _account_key(account: Optional[dict]) -> str:
        """返回一个稳定的账号级统计 key。

        池化邮箱直接使用邮箱地址；非池化 provider 没有真正的邮箱池行，
        调度器会给它传入 ``_auto_task_key``，这样同一个逻辑任务重试时
        仍只算一个账号。
        """
        if not isinstance(account, dict):
            return ""
        value = account.get("_auto_task_key") or account.get("email") or ""
        return str(value).strip().lower()

    def _begin_account_attempt(self, account: dict) -> str:
        """标记账号开始一次尝试，并返回账号 key。"""
        key = self._account_key(account)
        if not key:
            return ""
        with self._lock:
            record = self._account_records.setdefault(key, {
                "email": account.get("email", ""),
                "status": "pending",
                "attempts": 0,
                "retry_count": 0,
            })
            # 理论上不会把已终态账号再次投递；即使外部队列被重复写入，
            # 也不要让同一账号的最终统计被覆盖或重复增加。
            if record.get("status") in {
                "success", "failed", "running", "retry_reserved",
            }:
                return ""
            record["status"] = "running"
            record["attempts"] = int(record.get("attempts") or 0) + 1
        return key

    def _next_retry_number(self, account_key: str, category: str) -> int:
        """返回本账号下一次可用的重试序号；0 表示不再重试。"""
        # account = 明确账号失效；credential = 本地不可恢复的凭证缺失。
        # 两者重试都不会改变结果，尤其是缺失 TOTP secret 时反复登录只会
        # 触发更多风控请求。
        if not account_key or category in {"account", "credential"}:
            return 0
        with self._lock:
            if self._stop_event.is_set():
                return 0
            used = int(self._account_retry_used.get(account_key, 0) or 0)
            if used >= self._account_retry_count:
                return 0
            record = self._account_records.get(account_key)
            if record and record.get("status") in {"success", "failed"}:
                return 0
            return used + 1

    def _queue_login_retry(self, account: dict) -> bool:
        with self._lock:
            self._login_queue.insert(0, account)
        return True

    def _queue_registration_retry(self, account: dict, pooled: bool) -> bool:
        """为注册任务保留同一个账号的下一次尝试。

        registrar 在网络错误时会把池号 release、在其它错误时标记 failed。
        这里立即重新 claim，避免另一个 worker 把这条账号抢走；如果任务
        在重试前停止，manage_loop 的 finally 会释放这条保留记录。
        """
        retry_account = dict(account)
        if pooled:
            email = (account.get("email") or "").strip()
            if not email:
                return False
            # registrar release 与 worker 读到 runs.failed 之间存在一个很短
            # 的窗口，另一 worker 可能已经 claim 到同一邮箱并把它暂存到
            # 重试队列。先复用这条已持有的 claim，避免再次 claim 失败。
            with self._lock:
                if any(
                    (queued.get("email") or "").strip().lower() == email.lower()
                    for queued in self._registration_retry_queue
                ):
                    return True
            try:
                retry_account = db.claim_account(email) or {}
            except Exception:
                logger.exception("重试账号重新 claim 失败 email=%s", email)
                return False
            if not retry_account:
                return False
        if account.get("_auto_task_key"):
            retry_account["_auto_task_key"] = account["_auto_task_key"]
        with self._lock:
            self._registration_retry_queue.insert(0, retry_account)
        return True

    def _reserve_retry_state(self, account_key: str) -> None:
        """在把账号放入队列前暂时锁住它，避免其它 worker 抢先开始重试。"""
        if not account_key:
            return
        with self._lock:
            record = self._account_records.get(account_key)
            if record and record.get("status") not in {"success", "failed"}:
                record["status"] = "retry_reserved"

    def _record_finish(
        self,
        ok: bool,
        category: str,
        account_key: str = "",
        retry_number: int = 0,
        retry: bool = False,
    ):
        """记录一次账号尝试的结果。

        ``retry_number`` 大于 0 时表示下一次尝试已经成功排队：本次失败
        只进入 retry 统计，不进入最终失败/已完成统计。保留前两个参数的
        调用形式，方便旧调用方和外部测试继续使用；``retry=True`` 是
        ``retry_number=1`` 的兼容写法。
        """
        if retry and not retry_number:
            retry_number = 1
        if not account_key:
            # 兼容没有账号上下文的旧调用；正常 worker 永远会传真实 key。
            account_key = f"__attempt__{time.time_ns()}"
        with self._lock:
            record = self._account_records.setdefault(account_key, {
                "email": account_key,
                "status": "running",
                "attempts": 0,
                "retry_count": 0,
            })
            if record.get("status") in {"success", "failed"}:
                return
            if ok:
                self._registered_ok += 1
                self._task_completed += 1
                record["status"] = "success"
                self._consecutive_network_fails = 0
            elif retry_number > 0:
                # 重试序号在队列成功后才提交，避免“没重新 claim 到账号”
                # 仍被显示成已经重试。
                self._account_retry_used[account_key] = retry_number
                record["status"] = "retrying"
                record["retry_count"] = retry_number
                self._retry_attempts += 1
                if retry_number == 1:
                    self._retry_count += 1
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                self._task_completed += 1
                record["status"] = "failed"
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"账号进度 {self._task_completed}"
                f"/{self._task_total if self._task_total_known else '?'}，"
                f"成功 {self._registered_ok} / 最终失败 {self._registered_fail}"
                f" / 重试 {self._retry_count}"
            )
            # 目标数量：累计成功达标 → 触发停止（stop_event 幂等，多 worker 同时命中也安全）
            target_reached = bool(
                self._target_count and self._registered_ok >= self._target_count
            )
            trigger_break = (
                self._consecutive_network_fails >= self._circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
            )

        # 每一次尝试都会刷新页面统计，尤其是“重试中”状态不能等到最终
        # 结果才让用户看到。
        self._broadcast("state", self._snapshot())

        if target_reached:
            with self._lock:
                self._stop_event.set()
                self._last_message = (
                    f"🎯 已达目标 {self._target_count} 个，自动停止"
                    f"（成功 {self._registered_ok} / 最终失败 {self._registered_fail}"
                    f" / 重试 {self._retry_count}）"
                )
            logger.info(f"已达目标 {self._target_count} 个成功，触发自动停止")
            self._broadcast("state", self._snapshot())
            return

        if trigger_break:
            with self._lock:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                self._last_break_reason = (
                    f"连续 {self._consecutive_network_fails} 次网络/环境错误，"
                    f"自动暂停（号已自动 release，请检查代理后点恢复）"
                )
                self._last_message = self._last_break_reason
                self._consecutive_network_fails = 0
            logger.warning(self._last_break_reason)
            self._broadcast("circuit_break", {"reason": self._last_break_reason})

    def _finish_with_optional_retry(
        self,
        account: dict,
        ok: bool,
        category: str,
        *,
        pooled: bool,
    ) -> bool:
        """按账号口径收尾一次尝试，并在条件满足时排队重试。

        返回值表示是否已排入下一次尝试。
        """
        key = self._account_key(account)
        retry_number = self._next_retry_number(key, category) if not ok else 0
        queued = False
        if retry_number:
            self._reserve_retry_state(key)
            queued = (
                self._queue_login_retry(account)
                if self._options.get("login_only")
                else self._queue_registration_retry(account, pooled)
            )
        self._record_finish(
            ok,
            category,
            key,
            retry_number=retry_number if queued else 0,
        )
        return queued

    def _manage_loop(self):
        """主控线程：启动 worker，等所有 worker 结束，更新最终状态。"""
        try:
            workers = []
            for wid in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_loop, args=(wid,),
                    daemon=True, name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                # 每个 worker 之间错开 1s 启动，避免同时打 OpenAI
                time.sleep(1.0)
            self._workers = workers
            # 等所有 worker 退出
            for t in workers:
                t.join()
        except Exception as e:
            logger.exception(f"manage_loop 异常: {e}")
        finally:
            # 注册重试队列里的池号已经被提前 claim；任务被停止/熔断后不能
            # 把它们永久留在 in_use。登录队列不涉及号池状态。
            with self._lock:
                held_retry_accounts = list(self._registration_retry_queue)
                self._registration_retry_queue = []
            for account in held_retry_accounts:
                try:
                    if account.get("email"):
                        db.release_unused(account["email"])
                except Exception:
                    logger.exception(
                        "释放未执行的重试账号失败 email=%s", account.get("email", "")
                    )
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                # 任务结束后清除本次快照，避免计数污染下一次执行。
                self._proxy_usage = []
                self._proxy_pool = []
                self._single_proxy_usage = 0
                self._login_queue = []
                self._login_seen = set()
                self._login_context = ""
                self._last_message = (
                    f"已停止（完成 {self._task_completed}"
                    f"/{self._task_total if self._task_total_known else '?'}，"
                    f"成功 {self._registered_ok} / 最终失败 {self._registered_fail}"
                    f" / 重试 {self._retry_count}）"
                )
            self._broadcast("state", self._snapshot())

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        idle_round = 0
        logger.info(f"[worker-{worker_id}] 启动")

        while True:
            # 检查停止
            if self._stop_event.is_set():
                logger.info(f"[worker-{worker_id}] 已停止")
                return

            # 检查暂停
            if self._pause_event.is_set():
                while self._pause_event.is_set() and not self._stop_event.is_set():
                    time.sleep(0.5)
                if self._stop_event.is_set():
                    return

            # 目标数量闸门：已成功 + 在跑的（复用 _worker_status 当在跑数）≥ 目标 → 本 worker 退出
            # 不新增易泄漏的计数器；_worker_status 已在锁内正常维护，最大限度压低超额
            with self._lock:
                if self._target_count and (
                    self._registered_ok + len(self._worker_status) >= self._target_count
                ):
                    logger.info(
                        f"[worker-{worker_id}] 目标 {self._target_count} 已锁定，退出"
                    )
                    return

            # 仅登录从本次任务的注册结果快照取号，不触碰邮箱池状态。
            if self._options.get("login_only"):
                with self._lock:
                    account = self._login_queue.pop(0) if self._login_queue else None
                if not account:
                    logger.info(f"[worker-{worker_id}] 仅登录队列已完成")
                    return
                account.setdefault("_auto_task_key", self._account_key(account))
                pooled = False
            else:
                account = None

            # 注册模式 claim 下一个号。要不要走号池由 provider 的 pooled 决定，
            # 非池化的（CF 这类自己造地址的）用虚拟占位。
            if not self._options.get("login_only"):
                mail_source = db.get_setting("mail_source", "outlook")
                try:
                    pooled = get_provider_class(mail_source).pooled
                except MailProviderError as e:
                    logger.error(f"[worker-{worker_id}] {e}，停止")
                    self._set_message(str(e))
                    return
                if pooled:
                    # 重试账号在上一次失败后已经被本控制器重新 claim，
                    # 优先从保留队列取，不能让普通 claim 抢走它。
                    with self._lock:
                        account = (
                            self._registration_retry_queue.pop(0)
                            if self._registration_retry_queue else None
                        )
                    if account is None:
                        try:
                            account = db.claim_next(
                                kind=mail_source,
                                group_name=self._options.get("group_name", ""),
                            )
                        except ValueError as e:
                            logger.error(f"[worker-{worker_id}] 分组参数无效: {e}")
                            self._set_message(str(e))
                            return
                else:
                    with self._lock:
                        account = (
                            self._registration_retry_queue.pop(0)
                            if self._registration_retry_queue else None
                        )
                    if account is None:
                        account = {
                            "email": f"{mail_source}_placeholder_"
                                     f"{int(time.time())}_{worker_id}@placeholder.local",
                            "password": "", "client_id": "", "refresh_token": "",
                            "relay_url": "", "kind": mail_source,
                            # 临时地址每次都是新的邮箱，但一次失败重试
                            # 仍属于同一个逻辑账号对象。
                            "_auto_task_key": f"placeholder:{time.time_ns()}:{worker_id}",
                        }
            if not account:
                idle_round += 1
                if idle_round == 1:
                    self._set_message(
                        f"worker-{worker_id} 号池空，等待新号..."
                    )
                # 空 10 轮（约 30s）就停掉这个 worker
                if idle_round >= 10:
                    logger.info(f"[worker-{worker_id}] 号池空 30s，停止")
                    return
                # 等 3s 再试
                for _ in range(30):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)
                continue
            idle_round = 0

            account_key = self._begin_account_attempt(account)
            if not account_key:
                # 防御性处理：重复终态账号不应再次执行；若它来自池，
                # 及时释放此前保留的 claim。
                with self._lock:
                    record = self._account_records.get(
                        self._account_key(account), {}
                    )
                    retry_reserved = record.get("status") == "retry_reserved"
                    if retry_reserved:
                        # 另一个 worker 恰好在把账号放入重试队列，
                        # 等原 worker 完成统计后再取，不能把队列项丢掉。
                        if self._options.get("login_only"):
                            self._login_queue.insert(0, account)
                        else:
                            self._registration_retry_queue.insert(0, account)
                if retry_reserved:
                    time.sleep(0.01)
                    continue
                if (
                    pooled
                    and not self._options.get("login_only")
                    and record.get("status") == "running"
                ):
                    # registrar 刚 release，同邮箱被本 worker 抢到了，但
                    # 原 worker 还没来得及把失败记成 retrying。保留这条
                    # 已 claim 的队列项，让原 worker 直接复用。
                    account.setdefault("_auto_task_key", self._account_key(account))
                    with self._lock:
                        if not any(
                            self._account_key(queued) == self._account_key(account)
                            for queued in self._registration_retry_queue
                        ):
                            self._registration_retry_queue.insert(0, account)
                    time.sleep(0.01)
                    continue
                # status=running 表示另一个 worker 正在使用这条账号；
                # 不能在这里 release，否则会把对方的 in_use claim 提前
                # 改回 available。其它终态/异常队列项才需要清理。
                if (
                    pooled
                    and account.get("email")
                    and record.get("status") != "running"
                ):
                    db.release_unused(account["email"])
                continue

            # 每个任务开始时重新领取代理；不要在 worker 循环外固定一次。
            # 领取逻辑基于近期历史计数，优先选择租取次数最少的代理。
            lease_detail = str(
                account.get("_proxy_usage_detail")
                or self._options.get("proxy_usage_detail")
                or ("auto_login" if self._options.get("login_only") else "auto_register")
            ).strip().lower()
            proxy = self._proxy_for_worker(worker_id, task_detail=lease_detail)
            logger.info(f"[worker-{worker_id}] 领取代理 (proxy={proxy or '直连'})")

            # 给这个 run 注入 worker 自己的代理
            run_options = dict(self._options)
            if proxy:
                run_options["proxy"] = proxy
            active_proxy = [proxy]

            def _switch_proxy(current_proxy: str, reason: str) -> str:
                replacement = self._proxy_for_worker(
                    worker_id,
                    exclude_proxy=current_proxy,
                    task_detail=lease_detail,
                )
                if not replacement:
                    return ""
                active_proxy[0] = replacement
                with self._lock:
                    info = self._worker_status.get(worker_id)
                    if info:
                        info["proxy"] = replacement
                logger.warning(
                    f"[worker-{worker_id}] {reason}，重新租取代理继续当前任务"
                )
                self._broadcast("state", self._snapshot())
                return replacement

            # 仅自动任务拥有代理池租取上下文。回调保留在线程内，registrar/AuthFlow
            # 遇到 warmup 403 时可立即换出口，而无需结束并重新认领账号。
            if proxy:
                run_options["_proxy_switch_callback"] = _switch_proxy

            # 启一个 run
            try:
                run_id = registrar.start_registration(account, run_options)
            except Exception as e:
                logger.exception(f"[worker-{worker_id}] 启动注册失败: {e}")
                if pooled:
                    db.release_unused(account["email"])
                category = registrar.classify_error(
                    str(e), "login_only" if self._options.get("login_only") else ""
                )
                queued = self._finish_with_optional_retry(
                    account, False, category, pooled=pooled,
                )
                if queued:
                    with self._lock:
                        retry_no = self._account_retry_used.get(account_key, 0)
                    logger.info(
                        f"[worker-{worker_id}] {account.get('email', '')} 启动失败，"
                        f"将重试 ({retry_no}/{self._account_retry_count})"
                    )
                elif pooled and category != "network":
                    # 没有后续尝试时才把池号落为 failed；网络错误保持
                    # available，便于用户之后手动再次执行。
                    db.mark_failed(account["email"], f"[{category}] {e}")

                # 启动阶段也要做账号级错误分类。账号永久失效只针对
                # 仅登录/候选场景做后处理，且不会进入重试队列。
                if self._options.get("login_only") and category == "account":
                    try:
                        db.mark_registered_permanently_invalid(account.get("email", ""), str(e))
                    except Exception:
                        logger.exception("注册结果永久失效状态写入失败 email=%s", account.get("email", ""))
                    candidate_workspace_id = self._candidate_workspace_db_id()
                    if candidate_workspace_id:
                        try:
                            db.update_workspace_candidate_status(
                                candidate_workspace_id,
                                account.get("email", ""),
                                "permanently_invalid",
                            )
                        except Exception:
                            logger.exception(
                                "候选账号永久失效状态写入失败 workspace=%s email=%s",
                                candidate_workspace_id,
                                account.get("email", ""),
                            )
                        try:
                            if db.get_workspace_settings(candidate_workspace_id).get("trash_invalid_enabled", True):
                                from .workspace_membership import trash_workspace_candidates_by_email
                                trash_workspace_candidates_by_email(
                                    account.get("email", ""),
                                    reason="login_403",
                                )
                        except Exception:
                            logger.exception("登录启动阶段垃圾箱处理失败 email=%s", account.get("email", ""))
                    logger.warning(
                        "[worker-%s] %s 登录启动阶段确认账号已永久失效，跳过重试",
                        worker_id,
                        account.get("email", ""),
                    )
                elif (not self._options.get("login_only")) and not self._stop_event.is_set():
                    # 保留注册模式原有的启动失败退避，避免 start_registration
                    # 连续抛错时在重试队列里高速空转。
                    time.sleep(2)
                continue

            with self._lock:
                self._worker_status[worker_id] = {
                    "email": account["email"],
                    "run_id": run_id,
                    "proxy": active_proxy[0],
                    "started_at": time.time(),
                }
            self._broadcast("state", self._snapshot())
            self._broadcast("run_started", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "proxy": active_proxy[0],
            })

            # 等当前 run 跑完
            ok, category = self._wait_run_finish(run_id)

            with self._lock:
                self._worker_status.pop(worker_id, None)
            retry_scheduled = self._finish_with_optional_retry(
                account,
                ok,
                category,
                pooled=pooled,
            )
            if retry_scheduled:
                with self._lock:
                    retry_no = self._account_retry_used.get(account_key, 0)
                logger.info(
                    f"[worker-{worker_id}] {account.get('email', '')} 本次失败，"
                    f"将重试 ({retry_no}/{self._account_retry_count})"
                )
            candidate_workspace_id = self._candidate_workspace_db_id()
            if (not ok) and category == "account":
                try:
                    db.mark_registered_permanently_invalid(account.get("email", ""), str(category))
                except Exception:
                    logger.exception("注册结果永久失效状态写入失败 email=%s", account.get("email", ""))
            if (not ok) and category == "account" and candidate_workspace_id:
                try:
                    db.update_workspace_candidate_status(candidate_workspace_id, account.get("email", ""), "permanently_invalid")
                    if db.get_workspace_settings(candidate_workspace_id).get("trash_invalid_enabled", True):
                        from .workspace_membership import trash_workspace_candidates_by_email
                        trash_workspace_candidates_by_email(
                            account.get("email", ""),
                            reason="login_403",
                        )
                    logger.warning("候选账号标记为已永久失效 workspace=%s email=%s", candidate_workspace_id, account.get("email", ""))
                except Exception:
                    logger.exception("候选账号永久失效状态写入失败 workspace=%s email=%s", candidate_workspace_id, account.get("email", ""))
            self._broadcast("state", self._snapshot())
            self._broadcast("run_finished", {
                "worker_id": worker_id,
                "email": account["email"],
                "run_id": run_id,
                "ok": ok,
                "category": category,
                "retry_scheduled": retry_scheduled,
            })

            # 冷却（每个 worker 自己的节奏）
            cool_down = float(self._options.get("cool_down_seconds") or 3)
            if cool_down > 0:
                for _ in range(int(cool_down * 10)):
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    time.sleep(0.1)

    def _wait_run_finish(self, run_id: str, timeout: int = 1800) -> tuple[bool, str]:
        """轮询 runs 表，等 run 跑完。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            # stop 只禁止领取新任务；已经启动的注册/登录 run 必须等到
            # registrar 写入终态后再统计。否则用户点击“停止”时，底层线程
            # 仍会继续跑，但这里会提前把它记成失败，最终出现“日志成功、
            # 页面失败”的矛盾状态。
            con = db._conn()
            cur = con.execute(
                "SELECT status, error_category FROM runs WHERE run_id=?", (run_id,)
            )
            row = cur.fetchone()
            if row:
                st = row["status"]
                if st == "done":
                    return True, ""
                if st == "failed":
                    return False, (row["error_category"] or "")
            time.sleep(1)
        logger.warning(f"run {run_id} 等了 {timeout}s 没结束，超时放弃")
        return False, ""


# 全局单例
CONTROLLER = AutoLoopController()

# 注册和登录是两种不同的任务类型。登录控制器按空间建立：同一空间的
# 后续登录请求会进入同一个共享队列，不同空间可以各自并行。
_LOGIN_CONTROLLERS_LOCK = threading.Lock()
_LOGIN_CONTROLLERS: dict[str, AutoLoopController] = {}


def login_controller_for(
    workspace_db_id=None,
    workspace_id: str = "",
    *,
    ensure_credentials: bool = True,
    login_no_rt_only: bool = False,
) -> AutoLoopController:
    key = _login_context_key({
        "workspace_db_id": workspace_db_id,
        "workspace_id": workspace_id,
        "ensure_credentials": ensure_credentials,
        "login_no_rt_only": login_no_rt_only,
    })
    with _LOGIN_CONTROLLERS_LOCK:
        controller = _LOGIN_CONTROLLERS.get(key)
        if controller is None:
            controller = AutoLoopController()
            _LOGIN_CONTROLLERS[key] = controller
        return controller


def all_login_controllers() -> list[AutoLoopController]:
    with _LOGIN_CONTROLLERS_LOCK:
        return list(_LOGIN_CONTROLLERS.values())


# 兼容普通个人登录入口；空间登录通过 login_controller_for() 选择对应队列。
LOGIN_CONTROLLER = login_controller_for()
