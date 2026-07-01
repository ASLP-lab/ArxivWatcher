"""认证配置与 LDAP 认证后端。

通过环境变量配置：

- AUTH_METHODS         启用的认证方式（按优先级，逗号/空格分隔）：local、ldap。
                       例如 "local"、"ldap"、"local,ldap"（先本地后 LDAP）、"ldap,local"。
                       默认 "local"。
- ALLOW_REGISTER       是否允许注册（true/false），默认 true。仅对本地账号有效。

LDAP（当 AUTH_METHODS 含 ldap 时生效）：

- LDAP_URI             单个 LDAP 地址（与 LDAP_URLS 二选一），如 "ldap://ldap.example.com"。
- LDAP_URLS            多个地址（逗号分隔，按顺序 failover），如 "ldap://ldap1.example.com,ldap://ldap2.example.com"。
- LDAP_START_TLS       是否在绑定前 StartTLS（true/false），默认 false。

两种查找用户的方式（二选一）：

1) 直接绑定模式（简单，推荐已知 DN 规则时）：
   - LDAP_USER_DN_TEMPLATE   用户 DN 模板，{username} 会被替换，
                             如 "uid={username},ou=people,dc=example,dc=com"。

2) 搜索模式（先用服务账号搜索到用户 DN，再用其密码绑定）：
   - LDAP_BASE_DN            搜索基准 DN，如 "dc=ldapdomain,dc=com"。
   - LDAP_USER_FILTER        过滤器，{username} 会被转义后替换，默认 "(uid={username})"。
   - LDAP_BIND_DN            搜索用服务账号 DN（对应 LDAP_ADMIN_DN）。
   - LDAP_BIND_PASSWORD      服务账号密码（对应 LDAP_ADMIN_PASSWORD）。
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("arxivwatcher.auth")

try:
    import ldap3
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars
    _HAS_LDAP3 = True
except ImportError:  # 未安装 ldap3 时，LDAP 功能不可用
    ldap3 = None
    LDAPException = Exception
    _HAS_LDAP3 = False

    def escape_filter_chars(s):  # type: ignore
        return s

# 登录名基本校验：字母数字与 . _ -，长度 1-64（防注入 / 异常存储 key）
_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _envbool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def auth_methods() -> list[str]:
    """按优先级返回启用的认证方式（local / ldap）。默认仅 local。"""
    raw = os.environ.get("AUTH_METHODS", "local")
    methods: list[str] = []
    for m in re.split(r"[,\s]+", raw.strip().lower()):
        if m in ("local", "ldap") and m not in methods:
            methods.append(m)
    return methods or ["local"]


def local_enabled() -> bool:
    return "local" in auth_methods()


def ldap_enabled() -> bool:
    return "ldap" in auth_methods()


def registration_enabled() -> bool:
    """是否允许注册：需 ALLOW_REGISTER 为真，且启用了 local 方式。"""
    if not _envbool("ALLOW_REGISTER", True):
        return False
    return local_enabled()


def valid_login_name(username: str) -> bool:
    return bool(_LOGIN_RE.fullmatch(username or ""))


def config_summary() -> str:
    parts = [f"methods={'+'.join(auth_methods())}", f"register={'on' if registration_enabled() else 'off'}"]
    if ldap_enabled():
        mode = "dn-template" if os.environ.get("LDAP_USER_DN_TEMPLATE") else "search"
        uris = _ldap_uris()
        parts.append(f"ldap={uris[0] if uris else '(未配置)'}({mode})")
        if len(uris) > 1:
            parts.append(f"ldap-failover={len(uris)}")
        if not _HAS_LDAP3:
            parts.append("ldap3=缺失")
    return ", ".join(parts)


def _ldap_uris() -> list[str]:
    """读取 LDAP 地址列表：LDAP_URLS（逗号分隔）优先，否则 LDAP_URI。"""
    urls_raw = os.environ.get("LDAP_URLS", "").strip()
    if urls_raw:
        return [u.strip() for u in urls_raw.split(",") if u.strip()]
    uri = os.environ.get("LDAP_URI", "").strip()
    return [uri] if uri else []


def _ldap_server(uri: str):
    use_ssl = uri.lower().startswith("ldaps://")
    return ldap3.Server(uri, use_ssl=use_ssl, get_info=ldap3.NONE)


def _auto_bind():
    return ldap3.AUTO_BIND_TLS_BEFORE_BIND if _envbool("LDAP_START_TLS", False) else ldap3.AUTO_BIND_NO_TLS


def ldap_authenticate(username: str, password: str) -> tuple[bool, str]:
    """用 LDAP 校验账号密码。返回 (是否成功, 失败原因)。"""
    if not _HAS_LDAP3:
        return False, "服务器未安装 ldap3，无法使用 LDAP 认证"
    if not password:
        return False, "密码不能为空"
    uris = _ldap_uris()
    if not uris:
        return False, "未配置 LDAP_URI 或 LDAP_URLS"

    template = os.environ.get("LDAP_USER_DN_TEMPLATE", "").strip()
    last_err = "LDAP 服务暂时不可用"

    for uri in uris:
        try:
            server = _ldap_server(uri)
            if template:
                # 直接绑定模式：DN 模板 + 用户密码
                user_dn = template.format(username=username)
                conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=_auto_bind())
                bound = bool(conn.bound)
                conn.unbind()
                if bound:
                    return True, ""
                continue

            # 搜索模式：服务账号搜索用户 DN，再用其密码绑定
            base_dn = os.environ.get("LDAP_BASE_DN", "").strip()
            if not base_dn:
                return False, "未配置 LDAP_USER_DN_TEMPLATE 或 LDAP_BASE_DN"
            user_filter = os.environ.get("LDAP_USER_FILTER", "(uid={username})")
            bind_dn = os.environ.get("LDAP_BIND_DN", "").strip() or None
            bind_pw = os.environ.get("LDAP_BIND_PASSWORD", "") or None

            search_conn = ldap3.Connection(server, user=bind_dn, password=bind_pw, auto_bind=_auto_bind())
            flt = user_filter.format(username=escape_filter_chars(username))
            search_conn.search(base_dn, flt, attributes=[])
            entries = list(search_conn.entries)
            user_dn = entries[0].entry_dn if entries else None
            search_conn.unbind()
            if not user_dn:
                continue  # 该服务器上无此用户，尝试下一个 URI

            user_conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=_auto_bind())
            bound = bool(user_conn.bound)
            user_conn.unbind()
            if bound:
                return True, ""
        except LDAPException as e:
            log.warning(f"LDAP 认证未通过 ({username} @ {uri}): {e}")
            last_err = "用户名或密码错误"
            continue
        except Exception as e:
            log.warning(f"LDAP 认证异常 ({username} @ {uri}): {e}")
            continue

    return False, last_err
