"""Standalone Team registration workflow.

This is the executable entry point for ``auto_team``.  Configuration, browser
helpers, invitation requests and retry logic live here; the only other
project-local runtime module is the session exchange implementation.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import random
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit
from uuid import uuid4

import requests

try:
    from .exchange_team_session import exchange_team_session
except ImportError:
    from exchange_team_session import exchange_team_session

try:
    from .proxy_pool import ProxyPool, load_proxy_urls
except ImportError:
    from proxy_pool import ProxyPool, load_proxy_urls

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - depends on the runtime environment
    curl_requests = None

CAMOUFOX_LOCALE = "en-US"
DEFAULT_PROXY_URL = "socks5://127.0.0.1:7897"
DEFAULT_PROXY_TEST_URL = "https://api.ipify.org?format=json"
DEFAULT_PROXY_INFO_URL = "https://ipapi.co/json/"
DEFAULT_PROXY_TEST_TIMEOUT = 8.0
DEFAULT_CODEX_CONFIG_PATH: str | Path = Path(__file__).with_name("auto_team_config.json")
REGISTER_WAIT_TIMEOUT_MS = 30000
REG_URL = "https://auth.openai.com/create-account"
PERSONAL_SESSION_OUTPUT_DIR: str | Path = "."
TEAM_SESSION = False
PROXY_URL = DEFAULT_PROXY_URL
# 修改这里即可切换脚本启动流程："login_2fa" 或 "register"。
START_MODE = "register"
team_owner_session: dict[str, Any] = {}
workspace_id = ""

DEBUG_TIME_OUT = 300000
REQUIRED_CONFIG_PATHS = ("mother_session_file", "mail_cdk_file", "output_dir")
DEFAULT_CODEX_CONFIG = {
    "access_key": "",
    "register_url": REG_URL,
    "login_url": "https://auth.openai.com/login",
    "proxy_url": "",
    "proxy_pool_file": "",
    "proxy_test_url": DEFAULT_PROXY_TEST_URL,
    "proxy_info_url": DEFAULT_PROXY_INFO_URL,
    "proxy_test_timeout": DEFAULT_PROXY_TEST_TIMEOUT,
    "camoufox": {
        "headless": False,
        "geoip": True,
        "locale": [CAMOUFOX_LOCALE],
        "os": "macos",
    },
    "timeouts": {"debug_time_out": DEBUG_TIME_OUT},
}


def _deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_codex_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """读取浏览器超时配置；配置文件不存在时使用内置默认值。"""
    path = Path(config_path) if config_path is not None else Path(DEFAULT_CODEX_CONFIG_PATH)
    if not path.exists():
        return _deep_merge_config(DEFAULT_CODEX_CONFIG, {})
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置文件不是有效 JSON: {path}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"配置文件顶层必须是 JSON 对象: {path}")
    return _deep_merge_config(DEFAULT_CODEX_CONFIG, loaded)


def navigation_timeout_ms(
    config: dict[str, Any] | None = None,
    multiplier: int | float = 1,
) -> int:
    loaded_config = config or load_codex_config()
    timeouts = loaded_config.get("timeouts")
    timeouts = timeouts if isinstance(timeouts, dict) else {}
    try:
        base_timeout = int(timeouts.get("debug_time_out") or DEBUG_TIME_OUT)
    except (TypeError, ValueError):
        base_timeout = DEBUG_TIME_OUT
    try:
        factor = float(multiplier)
    except (TypeError, ValueError):
        factor = 1
    return max(1, int(base_timeout * max(1, factor)))


def goto_with_timeout(
    page,
    url: str,
    config: dict[str, Any] | None = None,
    multiplier: int | float = 1,
    **kwargs,
):
    kwargs.setdefault("timeout", navigation_timeout_ms(config, multiplier=multiplier))
    return page.goto(url, **kwargs)


def wait_for_specific_pages(page, dest_titles, interval=1, counts=60) -> bool:
    for _ in range(counts):
        title = page.title()
        print(f"current title is: {title}")
        if title in dest_titles:
            return True
        page.wait_for_timeout(interval*1000)
    return False


def read_session_json_from_page(page, response=None) -> dict[str, Any]:
    """从 ``/api/auth/session`` 页面读取 JSON。"""
    raw = ""
    if response is not None:
        try:
            raw = response.text()
        except Exception:
            raw = ""
    if not raw:
        try:
            raw = page.locator("pre").inner_text(timeout=3000)
        except Exception:
            raw = ""
    if not raw:
        raw = page.locator("body").inner_text(timeout=5000)

    raw = str(raw or "").strip()
    if not raw:
        raise RuntimeError("ChatGPT session 页面为空")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ChatGPT session 页面不是 JSON: {raw[:300]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("ChatGPT session JSON 不是对象")
    return data


def load_config(config_path: str | Path) -> dict[str, Any]:
    """读取运行配置，并将相对路径解析到配置文件所在目录。"""
    path = Path(config_path).expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"无法读取配置文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件不是有效 JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("配置文件顶层必须是 JSON 对象")

    required_paths = REQUIRED_CONFIG_PATHS if TEAM_SESSION else (
        "mail_cdk_file",
        "output_dir",
    )
    missing = [
        key for key in required_paths if not str(raw.get(key) or "").strip()
    ]
    if missing:
        raise ValueError(f"配置缺少必填字段: {', '.join(missing)}")

    loaded = dict(raw)
    for key in required_paths:
        configured_path = Path(str(raw[key])).expanduser()
        loaded[key] = (
            configured_path
            if configured_path.is_absolute()
            else path.parent / configured_path
        )
    loaded["config_path"] = path
    loaded["proxy_url"] = str(raw.get("proxy_url") or "").strip()
    return loaded


def load_mother_session(session_path: str | Path) -> dict[str, Any]:
    path = Path(session_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"无法读取母号 session 文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"母号 session JSON 格式无效: {path}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError("母号 session JSON 顶层必须是非空对象")
    return payload


def _workspace_id_from_session(session: dict[str, Any]) -> str:
    candidates = [session]
    nested = session.get("session")
    if isinstance(nested, dict):
        candidates.append(nested)
    for candidate in candidates:
        account = candidate.get("account")
        if isinstance(account, dict) and str(account.get("id") or "").strip():
            return str(account["id"]).strip()
        account_id = str(candidate.get("account_id") or "").strip()
        if account_id:
            return account_id
    raise ValueError("母号 session 中缺少 account.id")

try:
    from faker import Faker
except ImportError:  # pragma: no cover - handled by the project dependency
    Faker = None

fake = Faker("en_US") if Faker is not None else None


def generate_random_person() -> tuple[str, int]:
    """生成随机英文名和 18～55 岁之间的正态分布年龄。"""
    if fake is None:
        raise RuntimeError("请先安装 Faker 依赖: pip install Faker")
    name = fake.first_name()

    while True:
        age = round(random.gauss(mu=36.5, sigma=8))
        if 18 <= age <= 55:
            return name, age


def normalize_proxy_url(proxy_url: str) -> str:
    value = str(proxy_url or "").strip()
    if value.lower().startswith("socks5://"):
        return "socks5h://" + value[len("socks5://") :]
    return value


def _browser_proxy_settings(proxy_url: str) -> dict[str, str] | None:
    """Convert a proxy URL to the subset supported by Playwright/Camoufox."""
    value = str(proxy_url or "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if not parsed.hostname:
        raise RuntimeError("浏览器代理缺少 host")
    if scheme in {"socks5", "socks5h"} and (
        parsed.username is not None or parsed.password is not None
    ):
        raise RuntimeError("Camoufox 不支持 SOCKS5 代理认证")
    browser_scheme = "socks5" if scheme == "socks5h" else scheme
    host = parsed.hostname
    display_host = f"[{host}]" if ":" in host else host
    netloc = f"{display_host}:{parsed.port}" if parsed.port is not None else display_host
    settings = {"server": f"{browser_scheme}://{netloc}"}
    if parsed.username is not None:
        settings["username"] = unquote(parsed.username)
    if parsed.password is not None:
        settings["password"] = unquote(parsed.password)
    return settings


def _redact_proxy_credentials(text: str, proxy_url: str) -> str:
    """Remove proxy userinfo from an exception string while keeping host/port."""
    value = str(proxy_url or "").strip()
    if not value:
        return text
    parsed = urlsplit(value)
    if parsed.username is None and parsed.password is None:
        return text
    host = parsed.hostname or "proxy"
    display_host = f"[{host}]" if ":" in host else host
    host_port = f"{display_host}:{parsed.port}" if parsed.port is not None else display_host
    replacement = f"{parsed.scheme}://[REDACTED]@{host_port}"
    redacted = text.replace(value, replacement)
    for credential in (parsed.username, parsed.password):
        if credential:
            redacted = redacted.replace(credential, "[REDACTED]")
            decoded = unquote(credential)
            if decoded:
                redacted = redacted.replace(decoded, "[REDACTED]")
    return redacted


def _request_proxy_kwargs(
    timeout: int | float = 20,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    effective_proxy_url = PROXY_URL if proxy_url is None else proxy_url
    proxy_url = normalize_proxy_url(effective_proxy_url)
    proxy = {"http": proxy_url, "https": proxy_url} if proxy_url else {
        "http": None,
        "https": None,
    }
    return {"timeout": timeout, "proxies": proxy}


def _session_object(session_json: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(session_json, dict) and isinstance(session_json.get("session"), dict):
        return session_json["session"]
    return session_json if isinstance(session_json, dict) else {}


def _session_device_id(session_json: dict[str, Any] | None) -> str:
    session = _session_object(session_json)
    return str(
        session.get("deviceId")
        or session.get("device_id")
        or session.get("oaiDeviceId")
        or session.get("oai_device_id")
        or ""
    ).strip()


def _session_oai_session_id(session_json: dict[str, Any] | None) -> str:
    session = _session_object(session_json)
    return str(
        session.get("sessionId")
        or session.get("session_id")
        or session.get("oaiSessionId")
        or session.get("oai_session_id")
        or ""
    ).strip()


def _extract_session_access_token(session_json: dict[str, Any] | None) -> str | None:
    if not isinstance(session_json, dict):
        return None
    for key in ("accessToken", "access_token"):
        token = session_json.get(key)
        if isinstance(token, str) and token.strip():
            return token.strip()
    nested_session = session_json.get("session")
    if isinstance(nested_session, dict):
        return _extract_session_access_token(nested_session)
    return None


def _session_account_id(session_json: dict[str, Any] | None) -> str:
    session = _session_object(session_json)
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    return str(account.get("id") or session.get("account_id") or "").strip()


def _build_team_invite_headers(
    access_token: str,
    workspace_id: str,
    *,
    did: str = "",
    oai_session_id: str = "",
) -> dict[str, str]:
    target_path = f"/backend-api/accounts/{workspace_id}/invites"
    return {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": workspace_id,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/admin/members",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "x-openai-target-path": target_path,
        "x-openai-target-route": "/backend-api/accounts/{account_id}/invites",
        "oai-language": "zh-CN",
        "oai-device-id": did or str(uuid4()),
        "oai-session-id": oai_session_id or str(uuid4()),
    }


def send_team_invites(
    mother_session_json: dict[str, Any],
    *,
    workspace_id: str,
    email_addresses: list[str],
    role: str,
    seat_type: str,
    resend_emails: bool = True,
    proxy: str | None = None,
) -> dict[str, Any]:
    """使用与浏览器一致的 curl_cffi 请求发送 Team 邀请。"""
    access_token = _extract_session_access_token(mother_session_json)
    if not access_token:
        raise RuntimeError("母号 session JSON 缺少 accessToken")
    workspace_id = str(workspace_id or _session_account_id(mother_session_json) or "").strip()
    if not workspace_id:
        raise RuntimeError("缺少 workspace/account id")
    emails = [
        str(email).strip().lower()
        for email in email_addresses
        if str(email or "").strip()
    ]
    if not emails:
        raise RuntimeError("至少需要一个邀请邮箱")
    role = str(role or "standard-user").strip() or "standard-user"
    seat_type = str(seat_type or "").strip()
    if not seat_type:
        raise RuntimeError("seat_type 不能为空")

    if curl_requests is None:
        raise RuntimeError("邀请请求需要安装 curl_cffi: pip install curl_cffi")

    payload = {
        "email_addresses": emails,
        "role": role,
        "seat_type": seat_type,
        "resend_emails": bool(resend_emails),
    }
    headers = _build_team_invite_headers(
        access_token,
        workspace_id,
        did=_session_device_id(mother_session_json),
        oai_session_id=_session_oai_session_id(mother_session_json),
    )
    try:
        request_kwargs = _request_proxy_kwargs(proxy_url=proxy)
        request_kwargs["impersonate"] = "firefox133"
        response = curl_requests.post(
            f"https://chatgpt.com/backend-api/accounts/{workspace_id}/invites",
            headers=headers,
            data=json.dumps(payload, ensure_ascii=False),
            **request_kwargs,
        )
    except Exception as exc:
        raise RuntimeError(f"邀请请求失败: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = response.text[:1000]
    ok = 200 <= response.status_code < 300
    return {
        "ok": ok,
        "status_code": response.status_code,
        "workspace_id": workspace_id,
        "request_payload": payload,
        "response": body,
        "error": "" if ok else (body if isinstance(body, str) else response.text[:1000]),
    }


def invite_member(
    mother_session_json: dict[str, Any] | str,
    member_email: str,
    *,
    workspace_id: str = "",
    role: str = "standard-user",
    seat_type: str = "default",
    resend_emails: bool = True,
    proxy: str | None = None,
) -> dict[str, Any]:
    """邀请一个邮箱加入 workspace。

    ``mother_session_json`` 可以传入 session 字典或 JSON 字符串。
    """
    if isinstance(mother_session_json, str):
        try:
            mother_session = json.loads(mother_session_json)
        except json.JSONDecodeError as exc:
            raise ValueError("母号 session JSON 格式无效") from exc
    else:
        mother_session = mother_session_json

    if not isinstance(mother_session, dict):
        raise ValueError("母号 session 必须是字典或 JSON 字符串")

    member_email = str(member_email or "").strip()
    if not member_email:
        raise ValueError("成员邮箱不能为空")

    return send_team_invites(
        mother_session,
        workspace_id=str(workspace_id or "").strip(),
        email_addresses=[member_email],
        role=role,
        seat_type=seat_type,
        resend_emails=resend_emails,
        proxy=proxy,
    )


def invite_member_with_retries(
    mother_session_json: dict[str, Any] | str,
    member_email: str,
    *,
    workspace_id: str = "",
    max_attempts: int = 3,
    retry_interval: float = 2,
    proxy: str | None = None,
) -> dict[str, Any]:
    """邀请成员，失败后按间隔重试，达到次数后抛出异常。"""
    if max_attempts <= 0:
        raise ValueError("max_attempts 必须大于 0")
    if retry_interval < 0:
        raise ValueError("retry_interval 不能小于 0")

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = invite_member(
                mother_session_json,
                member_email,
                workspace_id=workspace_id,
                proxy=proxy,
            )
            if isinstance(response, dict) and response.get("ok") is False:
                error = response.get("error") or (
                    f"邀请接口返回失败，HTTP {response.get('status_code', 'unknown')}"
                )
                raise RuntimeError(str(error))
            return response
        except Exception as exc:
            last_error = exc
            print(f"第 {attempt}/{max_attempts} 次邀请失败: {exc}")
            if attempt < max_attempts:
                time.sleep(retry_interval)

    raise RuntimeError(f"邀请连续失败 {max_attempts} 次") from last_error


def exchange_team_session_with_retries(
    session_json: dict[str, Any] | str,
    workspace_id: str,
    *,
    proxy: str | None = None,
    max_attempts: int = 3,
    retry_interval: float = 2,
) -> dict[str, Any]:
    """交换 Team session，失败后按间隔重试，达到次数后抛出异常。"""
    if max_attempts <= 0:
        raise ValueError("max_attempts 必须大于 0")
    if retry_interval < 0:
        raise ValueError("retry_interval 不能小于 0")

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = exchange_team_session(
                session_json,
                workspace_id,
                proxy=proxy,
            )
            account = response.get("account") if isinstance(response, dict) else {}
            returned_workspace_id = str((account or {}).get("id") or "").strip()
            if returned_workspace_id and returned_workspace_id != str(workspace_id).strip():
                raise RuntimeError("交换结果仍是个人 workspace")
            return response
        except Exception as exc:
            last_error = exc
            print(f"第 {attempt}/{max_attempts} 次交换 session 失败: {exc}")
            if attempt < max_attempts:
                time.sleep(retry_interval)

    raise RuntimeError(f"交换 session 连续失败 {max_attempts} 次") from last_error


def wait_input_item(page, name: str, timeout=5) -> Any:
    input_item = page.get_by_label(name, exact=True)
    for i in range(timeout):
        if input_item.count() > 0:
            return input_item
        else:
            time.sleep(1)
        pass
    return False


def _click_registration_next(page) -> str:
    """Click whichever final registration button the current UI exposes."""
    for button_text in ("Finish creating account", "Continue"):
        try:
            button = page.get_by_text(button_text, exact=True)
            if button.count() > 0:
                button.first.click(timeout=5000)
                return button_text
        except Exception:
            continue
    raise RuntimeError(
        "注册年龄/姓名页面未找到 Finish creating account 或 Continue 按钮"
    )

def wait_for_join_team(page, timeout=300) -> Any:
    for i in range(timeout):
        if page.get_by_text("personal account").count():
            return True
        else:
            time.sleep(1)
        if i%60 == 0:
            page.goto("https://chatgpt.com", wait_until="domcontentloaded")
    return False

def register(
    email,
    code_url,
    proxy_url: str | None = None,
):
    from camoufox import DefaultAddons
    from camoufox.sync_api import Camoufox
    config = load_codex_config()
    effective_proxy_url = PROXY_URL if proxy_url is None else proxy_url
    with Camoufox(
        headless=False,
        proxy=_browser_proxy_settings(effective_proxy_url),
        exclude_addons=[DefaultAddons.UBO],
        geoip=True,
        locale=CAMOUFOX_LOCALE,
    ) as browser:

        page = browser.new_page()

        page.goto(REG_URL, wait_until="domcontentloaded")
        if not wait_for_specific_pages(page,['ChatGPT','ChatGPT: Chat, Work, Create & Code with AI','Your session has ended - OpenAI']):
            return -2
        if wait_for_specific_pages(page,['Your session has ended - OpenAI']):
            page.get_by_text("Log in", exact=True).click()
        if not wait_for_specific_pages(page,['Welcome back - OpenAI']):
            return -2
        page.goto(REG_URL, wait_until="domcontentloaded")
        if not wait_for_specific_pages(page,['Create an account - OpenAI']):
            return -2
        email_input = wait_input_item(page, "Email address", timeout=10)
        if not email_input:
            return -2
        email_input.first.fill(email)
        page.get_by_text("Continue", exact=True).click()
        if wait_for_specific_pages(page,['Create a password - OpenAI','Check your inbox - OpenAI']):
            if page.title() == 'Create a password - OpenAI':
                page.get_by_label("Password",exact=True).fill("mqkj12345678")
                page.get_by_text("Continue", exact=True).click()
            elif page.title() == 'Check your inbox - OpenAI':
                page.get_by_text("Continue with password").click()
                wait_for_specific_pages(page, ['Create a password - OpenAI','Enter your password - OpenAI'])
                page.get_by_text("Password",exact=True).fill("mqkj12345678")
                page.get_by_text("Continue", exact=True).click()

        if not wait_for_specific_pages(page, ['Check your inbox - OpenAI']):
            return -2
        try:
            for i in range(3):
                code = get_verification_code(
                    url=code_url,
                    proxy_url=effective_proxy_url,
                )
                page.get_by_label('Code', exact=True).fill(code)
                page.get_by_text("Continue", exact=True).click()
                if not wait_for_specific_pages(page,['How old are you? - OpenAI','ChatGPT'],interval=1,counts=12)  and page.get_by_text("Incorrect code").count() == 0:
                    return -2
                elif wait_for_specific_pages(page,['How old are you? - OpenAI', 'ChatGPT']):
                    break
            else:
                print("重试3次后失败")
            print(code)
        except Exception:
            print("无法接到验证码")
            return -2

        if page.title() == "How old are you? - OpenAI":
            ##年龄与姓名
            print("输入年龄与姓名")
            name, age = generate_random_person()
            print(name, age)
            page.get_by_label('Full name', exact=True).fill(name)
            page.get_by_label('Age', exact=True).fill(f"{age}")
            time.sleep(1.8)
            _click_registration_next(page)
            print("完成注册")
        print("等待加载主页")
        if not wait_for_specific_pages(page, ['ChatGPT']):
            return -2
        else:
            response = goto_with_timeout(page, "https://chatgpt.com/api/auth/session", config, multiplier=2,
                                         wait_until="networkidle")
            session_json = read_session_json_from_page(page, response)
        personal_session_path = save_personal_session_json(
            email,
            session_json,
            output_dir=PERSONAL_SESSION_OUTPUT_DIR,
        )
        print(f"注册完成，仅保存 personal session: {personal_session_path}")
        return -1


def _build_mail_api_proxies(proxy_url: str) -> dict[str, str] | None:
    """把配置中的代理地址转换成 requests 的 HTTP/HTTPS 代理映射。"""
    normalized_url = normalize_proxy_url(proxy_url)
    if not normalized_url:
        return None
    return {"http": normalized_url, "https": normalized_url}


def _proxy_info_from_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    ip = str(payload.get("ip") or payload.get("query") or "").strip()
    country = str(
        payload.get("country_name")
        or payload.get("country")
        or payload.get("countryName")
        or ""
    ).strip()
    region = str(
        payload.get("region")
        or payload.get("regionName")
        or payload.get("state")
        or payload.get("state_prov")
        or ""
    ).strip()
    city = str(payload.get("city") or "").strip()
    location = " / ".join(part for part in (country, region, city) if part)
    result: dict[str, Any] = {}
    if ip:
        result["ip"] = ip
    if location:
        result["location"] = location
    return result


def _proxy_health_summary(info: dict[str, Any]) -> str:
    latency = info.get("latency_ms")
    try:
        latency_text = f"{float(latency):.0f}ms"
    except (TypeError, ValueError):
        latency_text = "未知"
    return (
        f"出口 IP: {info.get('ip', '未知')}，"
        f"地区: {info.get('location', '未知地区')}，"
        f"延迟: {latency_text}"
    )


def _test_proxy_health(
    proxy_url: str,
    test_url: str,
    timeout: float,
    info_url: str = DEFAULT_PROXY_INFO_URL,
) -> bool | dict[str, Any]:
    """Check a proxy and return its exit IP/location when available."""
    try:
        proxies = _build_mail_api_proxies(proxy_url)
        started_at = time.monotonic()
        response = requests.get(
            test_url,
            proxies=proxies,
            timeout=timeout,
        )
        latency_ms = round((time.monotonic() - started_at) * 1000, 1)
        if not 200 <= int(response.status_code) < 400:
            return False

        info: dict[str, Any] = {}
        try:
            info = _proxy_info_from_payload(response.json())
        except Exception:
            info = {}
        if not info.get("ip") or not info.get("location"):
            try:
                geo_response = requests.get(info_url, proxies=proxies, timeout=timeout)
                if 200 <= int(geo_response.status_code) < 400:
                    geo_info = _proxy_info_from_payload(geo_response.json())
                    info = {**info, **geo_info}
            except Exception:
                # 连通性请求已成功；地理信息服务异常时保留代理并打印未知地区。
                pass
        if not info.get("ip"):
            return False
        info.setdefault("location", "未知地区")
        info["latency_ms"] = latency_ms
        return info
    except Exception:
        # The caller logs only a generic failure and the proxy host/port; never
        # expose proxy credentials or response details in batch output.
        return False


def _proxy_host_port(proxy_url: str) -> str:
    parsed = urlparse(str(proxy_url or ""))
    if not parsed.hostname:
        return "未知代理"
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname


def get_verification_code(url: str, proxy_url: str | None = None) -> str:
    """轮询验证码接口，最多请求 10 次，每次间隔 2 秒。"""
    for attempt in range(10):
        request_kwargs = {"timeout": 30}
        effective_proxy_url = PROXY_URL if proxy_url is None else proxy_url
        proxies = _build_mail_api_proxies(effective_proxy_url)
        if proxies:
            request_kwargs["proxies"] = proxies
        response = requests.get(url, **request_kwargs)
        response.raise_for_status()
        payload: Any = response.json()

        if not isinstance(payload, dict):
            raise RuntimeError(f"验证码接口返回格式异常: {payload!r}")
        if  payload.get("code") == -3:
            raise ValueError("邮箱失效")
        data = payload.get("data")

        code = data.get("code") if isinstance(data, dict) else None
        if code:
            return str(code).strip()
        if attempt < 9:
            time.sleep(2)
    raise TimeoutError("轮询 10 次后仍未获取到验证码")


def read_mail_cdk_file(file_path: str | Path) -> list[tuple[str, str]]:
    """读取 ``邮箱----验证码链接`` 格式的邮箱任务列表。"""
    path = Path(file_path).expanduser()
    entries: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("----", 1)
        if len(parts) != 2:
            raise ValueError(
                f"{path} 第 {line_number} 行格式错误，应为：邮箱----验证码链接"
            )
        email, code_url = (part.strip() for part in parts)
        if not email or not code_url:
            raise ValueError(
                f"{path} 第 {line_number} 行内容为空，应为：邮箱----验证码链接"
            )
        entries.append((email, code_url))
    return entries


def save_team_session_json(
    email: str,
    team_session: dict[str, Any],
    *,
    output_dir: str | Path = ".",
) -> Path:
    """将 team session 以格式化 JSON 保存为 ``<邮箱>.json``。"""
    path = _session_json_path(email, output_dir=output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(team_session, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _session_json_path(
    email: str,
    *,
    output_dir: str | Path = ".",
    prefix: str = "",
) -> Path:
    email = str(email or "").strip()
    if not email:
        raise ValueError("邮箱不能为空")
    safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", email).strip(" .")
    if not safe_name:
        raise ValueError("邮箱无法生成有效文件名")
    return Path(output_dir).expanduser() / f"{prefix}{safe_name}.json"


def save_personal_session_json(
    email: str,
    personal_session: dict[str, Any] | str,
    *,
    output_dir: str | Path = ".",
) -> Path:
    """将个人 session 保存为 ``personal-{邮箱}.json``。"""
    if isinstance(personal_session, str):
        try:
            personal_session = json.loads(personal_session)
        except json.JSONDecodeError as exc:
            raise ValueError("个人 session JSON 格式无效") from exc
    if not isinstance(personal_session, dict) or not personal_session:
        raise ValueError("个人 session 必须是非空字典或 JSON 字符串")

    path = _session_json_path(email, output_dir=output_dir, prefix="personal-")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(personal_session, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_personal_session_json(
    email: str,
    *,
    output_dir: str | Path = ".",
) -> dict[str, Any]:
    """读取 ``personal-{邮箱}.json`` 中的个人 session。"""
    path = _session_json_path(email, output_dir=output_dir, prefix="personal-")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"个人 session 文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"个人 session 文件格式无效: {path}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"个人 session 文件内容无效: {path}")
    return payload


def process_mailbox_with_retries(
    email: str,
    code_url: str,
    workspace_id: str,
    *,
    personal_session_dir: str | Path = ".",
    team_session_dir: str | Path = ".",
    proxy: str | None = None,
    max_attempts: int = 3,
    retry_interval: float = 2,
) -> dict[str, Any] | None:
    """处理单个邮箱；无母号模式返回已保存的 personal session。"""
    if max_attempts <= 0:
        raise ValueError("max_attempts 必须大于 0")
    if retry_interval < 0:
        raise ValueError("retry_interval 不能小于 0")

    personal_session: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            if personal_session is None:
                if proxy is None:
                    result = register(email, code_url)
                else:
                    result = register(email, code_url, proxy_url=proxy)
                if result == -2:
                    print(f"{email} 注册返回 -2，第 {attempt}/{max_attempts} 次结束")
                elif result == -1:
                    if not TEAM_SESSION:
                        return load_personal_session_json(
                            email,
                            output_dir=personal_session_dir,
                        )
                    personal_session = load_personal_session_json(
                        email,
                        output_dir=personal_session_dir,
                    )
                elif isinstance(result, dict) and result:
                    save_team_session_json(
                        email,
                        result,
                        output_dir=team_session_dir,
                    )
                    return result
                else:
                    raise RuntimeError(f"register 返回无效结果: {result!r}")

            if personal_session is not None:
                team_session = exchange_team_session_with_retries(
                    personal_session,
                    workspace_id,
                    proxy=proxy,
                )
                if not isinstance(team_session, dict) or not team_session:
                    raise RuntimeError("交换 session 返回无效结果")
                save_team_session_json(
                    email,
                    team_session,
                    output_dir=team_session_dir,
                )
                return team_session
        except Exception as exc:
            print(f"{email} 第 {attempt}/{max_attempts} 次处理失败: {exc}")

        if attempt < max_attempts:
            time.sleep(retry_interval)

    print(f"{email} 连续失败 {max_attempts} 次，跳过")
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行独立的 Team 注册流程")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CODEX_CONFIG_PATH),
        help="运行配置文件路径",
    )
    return parser


def run(config_path: str | Path) -> int:
    """读取配置并依次处理邮箱任务。"""
    global DEFAULT_CODEX_CONFIG_PATH, PERSONAL_SESSION_OUTPUT_DIR, PROXY_URL
    global team_owner_session, workspace_id

    loaded_config = load_config(config_path)
    if TEAM_SESSION:
        mother_session = load_mother_session(loaded_config["mother_session_file"])
        current_workspace_id = _workspace_id_from_session(mother_session)
    else:
        mother_session = {}
        current_workspace_id = ""
    output_dir = Path(loaded_config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    DEFAULT_CODEX_CONFIG_PATH = loaded_config["config_path"]
    team_owner_session = mother_session
    workspace_id = current_workspace_id
    PROXY_URL = loaded_config["proxy_url"] or DEFAULT_PROXY_URL
    PERSONAL_SESSION_OUTPUT_DIR = output_dir

    proxy_test_url = str(
        loaded_config.get("proxy_test_url") or DEFAULT_PROXY_TEST_URL
    ).strip()
    proxy_info_url = str(
        loaded_config.get("proxy_info_url") or DEFAULT_PROXY_INFO_URL
    ).strip()
    try:
        proxy_test_timeout = float(
            loaded_config.get("proxy_test_timeout") or DEFAULT_PROXY_TEST_TIMEOUT
        )
    except (TypeError, ValueError):
        proxy_test_timeout = DEFAULT_PROXY_TEST_TIMEOUT
    if proxy_test_timeout <= 0:
        proxy_test_timeout = DEFAULT_PROXY_TEST_TIMEOUT

    proxy_file = loaded_config.get("proxy_pool_file")
    if proxy_file:
        proxy_file = Path(str(proxy_file)).expanduser()
        if not proxy_file.is_absolute():
            proxy_file = loaded_config["config_path"].parent / proxy_file
    proxy_pool = (
        ProxyPool(load_proxy_urls(proxy_file))
        if proxy_file is not None and str(proxy_file).strip()
        else None
    )

    tasks = read_mail_cdk_file(loaded_config["mail_cdk_file"])
    print(f"共读取 {len(tasks)} 个邮箱任务")
    for email, code_url in tasks:
        print(f"开始处理: {email}")
        if proxy_pool is None:
            leased_proxy_url = PROXY_URL or None
        else:
            while True:
                try:
                    leased_proxy_url = proxy_pool.acquire()
                except RuntimeError as exc:
                    print(f"{email} 代理池不可用: {exc}")
                    leased_proxy_url = None
                    break
                proxy_label = _proxy_host_port(leased_proxy_url)
                proxy_info = _test_proxy_health(
                    leased_proxy_url,
                    proxy_test_url,
                    proxy_test_timeout,
                    proxy_info_url,
                )
                if proxy_info:
                    if isinstance(proxy_info, dict):
                        print(
                            f"代理 {proxy_label} 连通，{_proxy_health_summary(proxy_info)}"
                        )
                    break
                print(f"{email} 代理 {proxy_label} 测试失败，切换下一个代理")
                proxy_pool.disable(leased_proxy_url)
                leased_proxy_url = None
        if leased_proxy_url is None:
            if proxy_pool is None:
                print(f"{email} 代理测试失败，跳过")
            continue

        if proxy_pool is None:
            proxy_info = _test_proxy_health(
                leased_proxy_url,
                proxy_test_url,
                proxy_test_timeout,
                proxy_info_url,
            )
            if not proxy_info:
                print(f"{email} 代理测试失败，跳过")
                continue
            if isinstance(proxy_info, dict):
                print(
                    f"代理 {_proxy_host_port(leased_proxy_url)} 连通，"
                    f"{_proxy_health_summary(proxy_info)}"
                )
        try:
            result = process_mailbox_with_retries(
                email,
                code_url,
                current_workspace_id,
                personal_session_dir=output_dir,
                team_session_dir=output_dir,
                proxy=leased_proxy_url,
            )
        finally:
            if proxy_pool is not None:
                proxy_pool.release(leased_proxy_url)
        if result is None:
            print(f"{email} 处理失败，跳过")
        else:
            print(f"{email} 处理成功")
    return 0


def start_selected_mode(
    config_path: str | Path,
    *,
    key_file: str | Path | None = None,
    workers: int = 1,
    status_file: str | Path | None = None,
    limit: int | None = None,
    proxy_file: str | Path | None = None,
) -> int:
    """Start the flow selected by the module-level ``START_MODE`` setting."""
    mode = str(START_MODE or "").strip().lower()
    if mode == "register":
        return run(config_path)
    if mode == "login_2fa":
        global DEFAULT_CODEX_CONFIG_PATH
        DEFAULT_CODEX_CONFIG_PATH = config_path
        return run_2fa_batch(
            key_file=key_file,
            workers=workers,
            status_file=status_file,
            limit=limit,
            proxy_file=proxy_file,
        )
    raise ValueError(
        f"START_MODE 无效: {START_MODE!r}，只能是 'login_2fa' 或 'register'"
    )


def _parse_2fa_cdkey(cdkey: str) -> tuple[str, str, str]:
    parts = [part.strip() for part in str(cdkey or "").split("----")]
    if len(parts) != 3 or not all(parts):
        raise ValueError("2FA账号格式必须为：邮箱----密码----2FA密钥")
    return parts[0], parts[1], parts[2]


def _totp_now(secret: str) -> str:
    try:
        import pyotp
    except ImportError as exc:
        raise RuntimeError("请先安装 pyotp: pip install pyotp") from exc

    value = str(secret or "").strip()
    if not value:
        raise ValueError("2FA密钥不能为空")
    try:
        if value.lower().startswith("otpauth://"):
            return pyotp.parse_uri(value).now()
        normalized = re.sub(r"\s+", "", value).upper()
        return pyotp.TOTP(normalized).now()
    except Exception as exc:
        raise ValueError("2FA密钥无效，无法生成验证码") from exc


def _wait_for_first_locator(
    page,
    selectors: list[str] | tuple[str, ...],
    *,
    attempts: int = 10,
    wait_ms: int = 400,
):
    for attempt in range(max(1, attempts)):
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() > 0:
                    return locator
            except Exception:
                continue
        if attempt < max(1, attempts) - 1:
            page.wait_for_timeout(wait_ms)
    return None


def _is_codex_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    return bool(parse_qs(parsed.query).get("code"))


def _install_callback_capture(page) -> dict[str, str]:
    captured = {"url": ""}

    def remember(url: str) -> None:
        if not captured["url"] and _is_codex_callback_url(url):
            captured["url"] = str(url)

    def on_request(request) -> None:
        remember(getattr(request, "url", ""))

    def on_response(response) -> None:
        remember(getattr(response, "url", ""))
        try:
            remember((getattr(response, "headers", None) or {}).get("location", ""))
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    return captured


def _wait_for_callback_url(
    page,
    captured: dict[str, str],
    *,
    attempts: int = 120,
    wait_ms: int = 1000,
) -> str:
    for attempt in range(max(1, attempts)):
        callback_url = str(captured.get("url") or "")
        if _is_codex_callback_url(callback_url):
            return callback_url
        current_url = str(getattr(page, "url", "") or "")
        if _is_codex_callback_url(current_url):
            return current_url
        if attempt < max(1, attempts) - 1:
            page.wait_for_timeout(wait_ms)
    raise RuntimeError(f"等待 Codex OAuth callback 超时，当前 URL: {getattr(page, 'url', '')}")


PHONE_NUMBER_TITLES = (
    "Phone number required - OpenAI",
    "Phone number - OpenAI",
)
PHONE_CODE_TITLE = "Check your phone - OpenAI"
CODEX_CONSENT_TITLE = "Sign in to Codex with ChatGPT - OpenAI"


def _page_has_totp_evidence(page, title: str) -> tuple[bool, str, str]:
    """Use URL and page text as corroboration when the title is stale."""
    url = str(getattr(page, "url", "") or "")
    body_text = ""
    try:
        body_text = str(page.locator("body").inner_text(timeout=1000) or "")
    except Exception:
        pass
    haystack = f"{title} {url} {body_text}".lower()
    evidence = any(
        marker in haystack
        for marker in (
            "authenticator",
            "two-factor",
            "two factor",
            "2fa",
            "/mfa/",
            "/challenge",
            "verification code",
        )
    )
    return evidence, url, body_text


def _wait_for_phone_submission_result(page) -> bool:
    """Return whether the SMS code input becomes visible within five seconds."""
    try:
        page.get_by_label("Code", exact=True).wait_for(
            state="visible",
            timeout=5000,
        )
        return True
    except Exception:
        return False


def _restore_phone_number_page(page, tfa_secret: str | None = None) -> bool:
    """Go back from the SMS-code page to 2FA and submit a fresh TOTP."""
    if tfa_secret is None:
        return False

    totp_selectors = (
        "input[autocomplete='one-time-code']",
        "input[name='totp']",
        "input[name='code']",
        "input[inputmode='numeric']",
    )
    continue_selectors = (
        "button[data-dd-action-name='Continue']",
        "button:has-text('Continue')",
        "button:has-text('继续')",
        "button[type='submit']",
    )

    try:
        for _ in range(2):
            try:
                page.go_back(wait_until="domcontentloaded")
            except TypeError:
                page.go_back()
            page.wait_for_timeout(5000)
    except Exception:
        return False

    try:
        title = str(page.title() or "")
    except Exception:
        title = ""
    has_totp_evidence, url, _body_text = _page_has_totp_evidence(page, title)
    print(
        f"回退到 2FA 检查: title={title}, url={url}, "
        f"totp_evidence={has_totp_evidence}"
    )
    if not has_totp_evidence:
        return False

    try:
        totp_input = _wait_for_first_locator(
            page,
            totp_selectors,
            attempts=20,
            wait_ms=300,
        )
        if totp_input is None:
            return False
        totp_input.fill(_totp_now(tfa_secret))
        totp_continue = _wait_for_first_locator(
            page,
            continue_selectors,
            attempts=20,
            wait_ms=300,
        )
        if totp_continue is None:
            return False
        totp_continue.click(timeout=5000)
    except Exception:
        return False

    return wait_for_specific_pages(
        page,
        list(PHONE_NUMBER_TITLES),
        interval=0.5,
        counts=20,
    )

def _handle_phone_verification_if_needed(
    page,
    *,
    tfa_secret: str | None = None,
    client=None,
) -> bool:
    """Rent numbers until OpenAI accepts one and its SMS code reaches consent."""
    title = str(page.title() or "")
    if title not in PHONE_NUMBER_TITLES:
        return False

    if client is None:
        try:
            from .smsbower import SmsBowerClient
        except ImportError:  # 支持直接执行 auto_team 目录下的脚本
            from smsbower import SmsBowerClient

        client = SmsBowerClient.from_config(DEFAULT_CODEX_CONFIG_PATH)

    max_attempts = client.settings.max_phone_attempts
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        activation = None
        completed = False
        entered_phone_code_page = False
        try:
            activation = client.rent_number()
            page.get_by_label("Phone number", exact=True).fill(activation.phone_number)
            page.get_by_text("Continue", exact=True).click()
            if not _wait_for_phone_submission_result(page):
                raise RuntimeError(
                    f"手机号 {activation.phone_number} 提交 5 秒后仍未进入验证码页面"
                )
            entered_phone_code_page = True

            client.mark_ready(activation.activation_id)
            code = client.wait_for_code(activation.activation_id)
            page.get_by_label("Code", exact=True).fill(code)
            page.get_by_text("Continue", exact=True).click()
            if not wait_for_specific_pages(
                page,
                [CODEX_CONSENT_TITLE],
                interval=0.5,
                counts=60,
            ):
                raise RuntimeError("提交手机验证码后未进入 Codex OAuth consent 页面")

            completed = bool(client.finish(activation.activation_id))
            if not completed:
                print(f"SmsBower activation {activation.activation_id} 完成失败，尝试取消")
            else:
                print(f"SmsBower 手机接码验证成功: {activation.phone_number}")
            return True
        except Exception as exc:
            last_error = exc
            print(f"第 {attempt}/{max_attempts} 个 SmsBower 手机号验证失败: {exc}")
        finally:
            if activation is not None and not completed:
                client.cancel(activation.activation_id)

        if attempt < max_attempts and entered_phone_code_page and not _restore_phone_number_page(
            page, tfa_secret
        ):
            raise RuntimeError("手机验证失败后无法返回手机号输入页面") from last_error

    raise RuntimeError(f"连续 {max_attempts} 个手机号验证失败: {last_error}") from last_error


def login_2fa(
    cdkey: str,
    submit: bool = True,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    from camoufox import DefaultAddons
    from camoufox.sync_api import Camoufox

    try:
        from .cpa_oauth import get_codex_auth_url, submit_codex_oauth_callback
    except ImportError:  # 支持直接执行 auto_team 目录下的脚本
        from cpa_oauth import get_codex_auth_url, submit_codex_oauth_callback

    email, password, tfa_secret = _parse_2fa_cdkey(cdkey)
    oauth = get_codex_auth_url()
    login_url = str(oauth.get("url") or "").strip()
    oauth_state = str(oauth.get("state") or "").strip()
    if not login_url or not oauth_state:
        raise RuntimeError(f"CPA OAuth 响应缺少 url 或 state: {oauth}")

    config = load_codex_config()
    effective_proxy_url = PROXY_URL if proxy_url is None else proxy_url
    with Camoufox(
        headless=False,
        proxy=_browser_proxy_settings(effective_proxy_url),
        exclude_addons=[DefaultAddons.UBO],
        geoip=True,
        enable_cache=True,
        locale=CAMOUFOX_LOCALE,
    ) as browser:
        page = browser.new_page()
        captured = _install_callback_capture(page)
        goto_with_timeout(
            page,
            login_url,
            config,
            wait_until="domcontentloaded",
        )

        email_input = _wait_for_first_locator(
            page,
            [
                "input[type='email']",
                "input[name='email']",
                "input[autocomplete='email']",
                ".react-aria-TextField input",
            ],
            attempts=20,
            wait_ms=500,
        )
        if email_input is None:
            raise RuntimeError("Codex 登录页未找到邮箱输入框")
        email_input.fill(email)

        email_continue = _wait_for_first_locator(
            page,
            [
                "form[action='/log-in'] button[name='intent'][value='email']",
                "button[data-dd-action-name='Continue']",
                "button:has-text('Continue')",
                "button:has-text('继续')",
                "button[type='submit']",
            ],
        )
        if email_continue is None:
            raise RuntimeError("填写邮箱后未找到 Continue 按钮")
        email_continue.click(timeout=5000)

        password_input = _wait_for_first_locator(
            page,
            [
                "input[type='password']",
                "input[name='password']",
                "input[autocomplete='current-password']",
            ],
            attempts=30,
            wait_ms=500,
        )
        if password_input is None:
            raise RuntimeError("Codex 登录页未找到密码输入框")
        password_input.fill(password)

        password_continue = _wait_for_first_locator(
            page,
            [
                "button[data-dd-action-name='Continue']",
                "button:has-text('Continue')",
                "button:has-text('继续')",
                "button[type='submit']",
            ],
        )
        if password_continue is None:
            raise RuntimeError("填写密码后未找到 Continue 按钮")
        password_continue.click(timeout=5000)

        totp_input = _wait_for_first_locator(
            page,
            [
                "input[autocomplete='one-time-code']",
                "input[name='code']",
                "input[name='totp']",
                "input[inputmode='numeric']",
            ],
            attempts=30,
            wait_ms=500,
        )
        if totp_input is None:
            raise RuntimeError("Codex 登录页未找到 2FA 验证码输入框")
        totp_input.fill(_totp_now(tfa_secret))

        totp_continue = _wait_for_first_locator(
            page,
            [
                "button[data-dd-action-name='Continue']",
                "button:has-text('Continue')",
                "button:has-text('继续')",
                "button[type='submit']",
            ],
        )
        if totp_continue is None:
            raise RuntimeError("填写 2FA 验证码后未找到 Continue 按钮")
        totp_continue.click(timeout=5000)

        if not wait_for_specific_pages(
            page,
            [*PHONE_NUMBER_TITLES, CODEX_CONSENT_TITLE],
            interval=0.5,
            counts=60,
        ):
            raise RuntimeError("2FA 登录后未进入手机验证或 Codex OAuth consent 页面")
        _handle_phone_verification_if_needed(page, tfa_secret=tfa_secret)

        consent_continue = _wait_for_first_locator(
            page,
            [
                "form[action='/sign-in-with-chatgpt/codex/consent'] button[type='submit']",
                "form[action='/sign-in-with-chatgpt/codex/consent'] button[data-dd-action-name='Continue']",
                "button[data-dd-action-name='Continue']",
                "button:has-text('Continue')",
                "button:has-text('继续')",
            ],
            attempts=30,
            wait_ms=500,
        )
        if consent_continue is None:
            raise RuntimeError("未找到 Codex OAuth consent Continue 按钮")
        consent_continue.click(timeout=5000)
        if submit:
            callback_url = _wait_for_callback_url(page, captured)
            return submit_codex_oauth_callback(callback_url, oauth_state)
        else:
            return True


def run_2fa_batch(
    key_file: str | Path | None = None,
    workers: int = 1,
    status_file: str | Path | None = None,
    limit: int | None = None,
    proxy_file: str | Path | None = None,
) -> int:
    """Read ``2fa_key.txt`` and run the non-submitting 2FA flow per line."""
    if workers < 1:
        raise ValueError("并发数 workers 必须大于 0")
    if limit is not None and limit < 1:
        raise ValueError("执行数量 limit 必须大于 0")
    config = load_codex_config()
    if proxy_file is None:
        configured_path = config.get("proxy_pool_file")
        if configured_path:
            proxy_file = Path(configured_path).expanduser()
            if not proxy_file.is_absolute():
                proxy_file = Path(DEFAULT_CODEX_CONFIG_PATH).expanduser().parent / proxy_file
    proxy_pool = (
        ProxyPool(load_proxy_urls(proxy_file))
        if proxy_file is not None and str(proxy_file).strip()
        else None
    )
    proxy_test_url = str(config.get("proxy_test_url") or DEFAULT_PROXY_TEST_URL).strip()
    proxy_info_url = str(config.get("proxy_info_url") or DEFAULT_PROXY_INFO_URL).strip()
    try:
        proxy_test_timeout = float(config.get("proxy_test_timeout") or DEFAULT_PROXY_TEST_TIMEOUT)
    except (TypeError, ValueError):
        proxy_test_timeout = DEFAULT_PROXY_TEST_TIMEOUT
    if proxy_test_timeout <= 0:
        proxy_test_timeout = DEFAULT_PROXY_TEST_TIMEOUT
    path = Path(key_file) if key_file is not None else Path(__file__).with_name("2fa_key.txt")
    status_path = (
        Path(status_file)
        if status_file is not None
        else Path(__file__).with_name("phone_verification_status.json")
    )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"无法读取 2FA 文件 {path}: {exc}")
        return 1

    entries = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if limit is not None:
        entries = entries[:limit]
    if not entries:
        print(f"2FA 文件没有可处理的账号: {path}")
        return 1

    try:
        statuses = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        statuses = {}
    if not isinstance(statuses, dict):
        statuses = {}
    status_lock = threading.Lock()

    def save_status(email: str, status: str) -> None:
        with status_lock:
            statuses[email] = status
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(
                json.dumps(statuses, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    total = len(entries)
    prepared: list[tuple[int, str, str | None, str | None, str | None]] = []
    skipped = 0
    for index, cdkey in enumerate(entries, start=1):
        try:
            email, password, tfa_secret = _parse_2fa_cdkey(cdkey)
        except ValueError:
            prepared.append((index, cdkey, None, None, None))
            continue
        if statuses.get(email) == "passed":
            print(f"[{index}/{total}] {email} 已通过手机接码，跳过")
            skipped += 1
            continue
        prepared.append((index, cdkey, email, password, tfa_secret))

    def run_one(item: tuple[int, str, str | None, str | None, str | None]) -> bool:
        index, cdkey, email, password, tfa_secret = item
        if email is None:
            try:
                _parse_2fa_cdkey(cdkey)
            except ValueError as exc:
                print(f"[{index}/{total}] 格式错误: {exc}")
                return False
        assert email is not None and password is not None and tfa_secret is not None

        def execute(proxy_url: str | None = None) -> bool:
            proxy_label = ""
            if proxy_url:
                parsed_proxy = urlparse(proxy_url)
                proxy_label = f"，代理 {parsed_proxy.hostname}:{parsed_proxy.port}"
            print(f"[{index}/{total}] 开始处理 {email}{proxy_label}")
            try:
                if proxy_url is None:
                    login_2fa(cdkey, submit=False)
                else:
                    login_2fa(cdkey, submit=False, proxy_url=proxy_url)
            except Exception as exc:
                safe_error = str(exc)
                for secret in (password, tfa_secret):
                    if secret:
                        safe_error = safe_error.replace(secret, "[REDACTED]")
                effective_error_proxy = PROXY_URL if proxy_url is None else proxy_url
                safe_error = _redact_proxy_credentials(
                    safe_error,
                    effective_error_proxy,
                )
                save_status(email, "failed")
                print(f"[{index}/{total}] {email} 接码/登录失败: {safe_error}")
                return False
            else:
                save_status(email, "passed")
                print(f"[{index}/{total}] {email} 登录流程成功")
                return True

        def check_proxy(proxy_url: str) -> bool:
            parsed_proxy = urlparse(proxy_url)
            proxy_label = (
                f"{parsed_proxy.hostname}:{parsed_proxy.port}"
                if parsed_proxy.hostname
                else "直连"
            )
            health_info = _test_proxy_health(
                proxy_url,
                proxy_test_url,
                proxy_test_timeout,
                proxy_info_url,
            )
            if not health_info:
                print(f"[{index}/{total}] 代理 {proxy_label} 测试失败")
                return False
            if isinstance(health_info, dict):
                print(
                    f"[{index}/{total}] 代理 {proxy_label} 连通，"
                    f"{_proxy_health_summary(health_info)}"
                )
            return True

        if proxy_pool is None:
            if not check_proxy(PROXY_URL):
                save_status(email, "failed")
                print(f"[{index}/{total}] {email} 接码/登录失败: 代理不可用")
                return False
            return execute()
        while True:
            try:
                leased_proxy_url = proxy_pool.acquire()
            except RuntimeError as exc:
                save_status(email, "failed")
                print(f"[{index}/{total}] {email} 接码/登录失败: {exc}")
                return False

            parsed_proxy = urlparse(leased_proxy_url)
            proxy_label = f"{parsed_proxy.hostname}:{parsed_proxy.port}"
            if check_proxy(leased_proxy_url):
                try:
                    return execute(leased_proxy_url)
                finally:
                    proxy_pool.release(leased_proxy_url)

            print(f"[{index}/{total}] 代理 {proxy_label} 测试失败，切换下一个代理")
            proxy_pool.disable(leased_proxy_url)

    if workers == 1:
        results = [run_one(item) for item in prepared]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(run_one, prepared))

    failures = sum(not result for result in results)
    print(f"批量处理完成: {total - failures}/{total} 成功, {failures} 失败")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args.config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批量测试 2FA 和 SmsBower 接码")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CODEX_CONFIG_PATH),
        help="配置文件路径",
    )
    parser.add_argument(
        "--key-file",
        default=str(Path(__file__).with_name("2fa_key.txt")),
        help="2FA 密钥文件路径",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并发账号数，默认 1",
    )
    parser.add_argument(
        "--status-file",
        default=str(Path(__file__).with_name("phone_verification_status.json")),
        help="手机接码状态文件路径",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多执行前 N 个账号，默认不限制",
    )
    parser.add_argument(
        "--proxy-file",
        default=None,
        help="SOCKS5 代理池文件，一行一个代理；不传则使用原单代理配置",
    )
    cli_args = parser.parse_args()
    try:
        raise SystemExit(
            start_selected_mode(
                cli_args.config,
                key_file=cli_args.key_file,
                workers=cli_args.workers,
                status_file=cli_args.status_file,
                limit=cli_args.limit,
                proxy_file=cli_args.proxy_file,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
