"""Team 母号专用 HTTP 会话。

母号请求必须通过这里创建客户端：只读取 workspace_masters.proxy_url，绝不读取注册
任务的单代理或代理池。没有绑定代理时直接报错，防止意外直连或借用代理池出口。
"""
from __future__ import annotations

from http_client import create_http_session

from . import db


def create_workspace_http_session(workspace_id: int):
    master = db.get_workspace_master(workspace_id)
    if not master:
        raise RuntimeError("母号不存在")
    proxy = db.normalize_workspace_proxy(master.get("proxy_url", ""))
    return create_http_session(proxy=proxy), master
