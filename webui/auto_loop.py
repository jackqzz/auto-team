"""auto-loop 控制器：多 worker 并发，每个 worker 用独立代理。

设计：
  - 主控线程 manage_loop：监听 stop/pause、根据 concurrency 启停 worker
  - 多个 worker 线程：claim_next() → 注册 → 完成 → 继续
  - 代理池：每个任务优先领取当前租取次数最少的代理
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

from . import db, registrar
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
    """登录任务按空间划分上下文；同一空间后续请求进入同一队列。"""
    value = (options or {}).get("workspace_db_id")
    if value not in (None, ""):
        base = f"workspace:{value}"
    else:
        value = (options or {}).get("workspace_id")
        if value not in (None, ""):
            base = f"workspace_external:{value}"
        else:
            base = "personal"
    if bool((options or {}).get("login_no_rt_only")):
        return f"{base}:no_rt"
    return base


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
            if self._options.get("login_only"):
                try:
                    self._login_queue = db.list_login_candidates(
                        self._options.get("group_name", ""),
                        filter_rt=("no_rt" if self._options.get("login_no_rt_only") else "all"),
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
                if not self._login_queue:
                    return {"ok": False, "error": "当前分组没有可登录的注册结果"}
                self._login_candidate_count = len(self._login_queue)
                self._login_seen = {
                    (row.get("email") or "").strip().lower()
                    for row in self._login_queue
                    if (row.get("email") or "").strip()
                }
                self._login_context = _login_context_key(self._options)
            else:
                self._login_queue = []
                self._login_candidate_count = 0
                self._login_seen = set()
                self._login_context = ""
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
            # 计数只属于本次自动任务，任务重新启动时建立全新的快照。
            self._proxy_usage = [0] * len(self._proxy_pool)
            # 目标成功数（0=不限量）
            self._target_count = max(0, int(self._options.get("target_count") or 0))
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
            "login_no_rt_only": bool(self._options.get("login_no_rt_only")),
            "login_candidate_count": self._login_candidate_count,
        }

    def _enqueue_login_locked(self, options: dict) -> dict:
        """将同一空间的新登录请求追加到共享队列（调用方已持有锁）。"""
        try:
            rows = db.list_login_candidates(
                options.get("group_name", ""),
                filter_rt=("no_rt" if options.get("login_no_rt_only") else "all"),
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
        for row in rows:
            email = (row.get("email") or "").strip().lower()
            if email and email not in self._login_seen:
                self._login_seen.add(email)
                new_rows.append(row)
        self._login_queue.extend(new_rows)
        self._login_candidate_count += len(new_rows)
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
            return {
                "state": self._state,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_fail": self._registered_fail,
                "target_count": self._target_count,
                "account_retry_count": self._account_retry_count,
                "login_only": bool(self._options.get("login_only")),
                "task_type": "login" if self._options.get("login_only") else "register",
                "login_candidate_count": self._login_candidate_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok)
                    if self._target_count else None
                ),
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "proxy_pool_usage": [
                    {"proxy": proxy, "leased_count": self._proxy_usage[i]}
                    for i, proxy in enumerate(self._proxy_pool)
                ],
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

    def _proxy_for_worker(self, worker_id: int = 0, exclude_proxy: str = "") -> str:
        """为新任务领取当前租取次数最少的代理。

        选择和计数递增在同一把锁内完成，避免并发 worker 同时拿到同一个
        最小计数。计数快照仅在本次自动任务生命周期内有效。
        """
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
                return proxy
            return self._options.get("proxy", "") or ""

    def _record_finish(self, ok: bool, category: str):
        """worker 结束一个 run 后调，更新计数 + 熔断。"""
        with self._lock:
            if ok:
                self._registered_ok += 1
                self._consecutive_network_fails = 0
            else:
                self._registered_fail += 1
                if category == "network":
                    self._consecutive_network_fails += 1
                else:
                    self._consecutive_network_fails = 0
            self._last_message = (
                f"累计 ok={self._registered_ok} fail={self._registered_fail}"
            )
            # 目标数量：累计成功达标 → 触发停止（stop_event 幂等，多 worker 同时命中也安全）
            target_reached = bool(
                self._target_count and self._registered_ok >= self._target_count
            )
            trigger_break = (
                self._consecutive_network_fails >= self._circuit_break_threshold
                and self._state == AutoLoopState.RUNNING
            )

        if target_reached:
            with self._lock:
                self._stop_event.set()
                self._last_message = (
                    f"🎯 已达目标 {self._target_count} 个，自动停止"
                    f"（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
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
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                # 任务结束后清除本次快照，避免计数污染下一次执行。
                self._proxy_usage = []
                self._proxy_pool = []
                self._login_queue = []
                self._login_seen = set()
                self._login_context = ""
                self._last_message = (
                    f"已停止（成功 {self._registered_ok} / 失败 {self._registered_fail}）"
                )
            self._broadcast("state", self._snapshot())

    def _worker_loop(self, worker_id: int):
        """单 worker 循环：claim → 跑 → 等结束 → 继续。"""
        idle_round = 0
        retry_counts: dict[str, int] = {}
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
                    account = {
                        "email": f"{mail_source}_placeholder_"
                                 f"{int(time.time())}_{worker_id}@placeholder.local",
                        "password": "", "client_id": "", "refresh_token": "",
                        "relay_url": "", "kind": mail_source,
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

            # 每个任务开始时重新领取代理；不要在 worker 循环外固定一次。
            # 领取逻辑会优先选择本次任务快照中租取次数最少的代理。
            proxy = self._proxy_for_worker(worker_id)
            logger.info(f"[worker-{worker_id}] 领取代理 (proxy={proxy or '直连'})")

            # 给这个 run 注入 worker 自己的代理
            run_options = dict(self._options)
            if proxy:
                run_options["proxy"] = proxy
            active_proxy = [proxy]

            def _switch_proxy(current_proxy: str, reason: str) -> str:
                replacement = self._proxy_for_worker(
                    worker_id, exclude_proxy=current_proxy,
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
                # 启动阶段也要做账号级错误分类。此前这里统一按 unknown
                # 处理，导致 403 账号停用错误仍被重新放回登录队列。
                if self._options.get("login_only"):
                    key = (account.get("email") or "").strip().lower()
                    category = registrar.classify_error(str(e), "login_only")
                    self._record_finish(False, category)
                    if category == "account":
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
                            if candidate_workspace_id and db.get_workspace_settings(candidate_workspace_id).get("trash_invalid_enabled", True):
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
                        continue
                    used = retry_counts.get(key, 0)
                    if used < self._account_retry_count and not self._stop_event.is_set():
                        retry_counts[key] = used + 1
                        with self._lock:
                            self._login_queue.insert(0, account)
                        logger.info(
                            f"[worker-{worker_id}] {account['email']} 启动失败，"
                            f"将重试 ({used + 1}/{self._account_retry_count})"
                        )
                    continue
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
            self._record_finish(ok, category)
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
            })

            # 账号级重试：失败后将同一账号重新放回本次登录快照队列，
            # 每个账号独立计数，成功或达到上限后不再投递。
            if (
                not ok
                and self._options.get("login_only")
                and category != "account"
                and not self._stop_event.is_set()
            ):
                key = (account.get("email") or "").strip().lower()
                used = retry_counts.get(key, 0)
                if used < self._account_retry_count:
                    retry_counts[key] = used + 1
                    with self._lock:
                        self._login_queue.insert(0, account)
                    logger.info(
                        f"[worker-{worker_id}] {account['email']} 失败，"
                        f"将重试 ({used + 1}/{self._account_retry_count})"
                    )

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
            if self._stop_event.is_set():
                return False, ""
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


def login_controller_for(workspace_db_id=None, workspace_id: str = "") -> AutoLoopController:
    key = _login_context_key({
        "workspace_db_id": workspace_db_id,
        "workspace_id": workspace_id,
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
