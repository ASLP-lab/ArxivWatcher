#!/usr/bin/env python3
"""交互式本地账号管理工具（local 认证）。

功能：列出 / 创建 / 删除 / 修改密码。读写与 Web 服务同一份用户存储，
存储后端由环境变量 STORAGE_BACKEND（json / sqlite）与 SQLITE_PATH 决定，
与 start_web_internal.sh 保持一致即可。

用法：
    python user_admin.py            # 进入交互式菜单
    STORAGE_BACKEND=sqlite python user_admin.py
"""
from __future__ import annotations

import getpass
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import bcrypt

import storage

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data"
BJ_TZ = ZoneInfo("Asia/Shanghai")

# 与 web.py 注册一致的用户名规则
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
MIN_PASSWORD_LEN = 6


def _make_users_store():
    backend = storage.resolve_backend()
    sqlite_path = Path(os.environ.get("SQLITE_PATH") or (DATA_ROOT / storage.DEFAULT_SQLITE_NAME))
    store = storage.Store("users", data_dir=DATA_ROOT, backend=backend, sqlite_path=sqlite_path)
    return store, backend, sqlite_path


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _prompt_username(store: "storage.Store", must_exist: bool) -> str | None:
    """读取用户名。must_exist=True 时要求已存在，否则要求不存在且合法。"""
    while True:
        username = input("用户名（直接回车取消）: ").strip()
        if not username:
            return None
        users = store.all()
        exists = username in users
        if must_exist:
            if not exists:
                print(f"  ✗ 用户 '{username}' 不存在，请重试。")
                continue
            return username
        # 创建场景
        if not _USERNAME_RE.fullmatch(username):
            print("  ✗ 用户名需 3-32 位字母、数字或下划线，请重试。")
            continue
        if exists:
            print(f"  ✗ 用户 '{username}' 已存在，请重试。")
            continue
        return username


def _prompt_new_password() -> str | None:
    """两次输入新密码（隐藏输入）。返回 None 表示取消。"""
    while True:
        pwd = getpass.getpass(f"新密码（至少 {MIN_PASSWORD_LEN} 位，直接回车取消）: ")
        if not pwd:
            return None
        if len(pwd) < MIN_PASSWORD_LEN:
            print(f"  ✗ 密码至少 {MIN_PASSWORD_LEN} 位，请重试。")
            continue
        pwd2 = getpass.getpass("再次输入新密码: ")
        if pwd != pwd2:
            print("  ✗ 两次密码不一致，请重试。")
            continue
        return pwd


def list_users(store: "storage.Store") -> None:
    users = store.all()
    if not users:
        print("（暂无用户）")
        return
    print(f"共 {len(users)} 个用户：")
    print(f"  {'用户名':<24} {'来源':<8} {'创建时间':<20}")
    print("  " + "-" * 54)
    for name in sorted(users):
        info = users[name] or {}
        source = info.get("source") or ("local" if info.get("password_hash") else "?")
        created = (info.get("created_at") or "")[:19]
        print(f"  {name:<24} {source:<8} {created:<20}")


def create_user(store: "storage.Store") -> None:
    print("\n── 创建用户 ──")
    username = _prompt_username(store, must_exist=False)
    if username is None:
        print("已取消。")
        return
    password = _prompt_new_password()
    if password is None:
        print("已取消。")
        return
    store.put(username, {
        "password_hash": _hash_password(password),
        "source": "local",
        "created_at": datetime.now(BJ_TZ).isoformat(),
    })
    print(f"  ✓ 已创建用户 '{username}'。")


def delete_user(store: "storage.Store") -> None:
    print("\n── 删除用户 ──")
    username = _prompt_username(store, must_exist=True)
    if username is None:
        print("已取消。")
        return
    confirm = input(f"确认删除用户 '{username}'？此操作不可恢复 (yes/N): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("已取消。")
        return
    store.delete(username)
    print(f"  ✓ 已删除用户 '{username}'。")
    print("  （提示：该用户的评论 / 收藏 / 标记等数据不会被自动清除。）")


def change_password(store: "storage.Store") -> None:
    print("\n── 修改密码 ──")
    username = _prompt_username(store, must_exist=True)
    if username is None:
        print("已取消。")
        return
    info = dict(store.get(username) or {})
    if info.get("source") == "ldap" and not info.get("password_hash"):
        ans = input(f"'{username}' 是 LDAP 账号，设置本地密码将使其也能用本地登录，继续？(yes/N): ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            return
    password = _prompt_new_password()
    if password is None:
        print("已取消。")
        return
    info["password_hash"] = _hash_password(password)
    info.setdefault("source", "local")
    info.setdefault("created_at", datetime.now(BJ_TZ).isoformat())
    store.put(username, info)
    print(f"  ✓ 已更新 '{username}' 的密码。")


MENU = """
========= 本地账号管理 =========
  1) 列出用户
  2) 创建用户
  3) 删除用户
  4) 修改密码
  0) 退出
================================"""


def main() -> None:
    store, backend, sqlite_path = _make_users_store()
    location = str(sqlite_path) if backend == "sqlite" else str(DATA_ROOT / "users.json")
    print(f"存储后端: {backend}    位置: {location}")
    actions = {
        "1": list_users,
        "2": create_user,
        "3": delete_user,
        "4": change_password,
    }
    while True:
        print(MENU)
        try:
            choice = input("请选择操作: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return
        if choice in ("0", "q", "quit", "exit"):
            print("再见。")
            return
        action = actions.get(choice)
        if not action:
            print("  ✗ 无效选项，请输入 0-4。")
            continue
        try:
            action(store)
        except (EOFError, KeyboardInterrupt):
            print("\n已取消当前操作。")


if __name__ == "__main__":
    sys.exit(main())
