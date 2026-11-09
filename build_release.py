#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Retic 发布包构建脚本（防呆版）

目的：生成**对外发布**的 zip 时，自动剔除本机现网运行数据，避免别人下载后
      看到设备清单、巡检报告、执行日志、拓扑布局或定制配置。

用法：
    python build_release.py                          # 用默认路径构建并校验
    python build_release.py --src <目录> --out <zip>  # 指定源目录 / 输出
    python build_release.py --check <zip>            # 只校验已有 zip，不重新打包

默认路径（相对本脚本所在目录）：
    源目录：Retic-v2.2.0/
    输出：  Retic-v2.2.0.zip

硬性规则：
  1. 排除 reports/、logs/ 下所有文件，以及 *.log/*.db/*.sqlite*/*.pyc/*.bak/*.tmp
  2. 排除 data/（手动拓扑等运行时数据）、topology_layout.json 与 topology*.json
  3. config/config.json 强制替换为内置默认模板（无设备、无 IP、无密码）
  4. config/commands.txt 若含「厂商_型号」定制分组，替换为内置通用模板
  5. 打包后逐项校验；任何一项不通过就删除临时文件并报错退出，不产出脏包
"""

import argparse
import io
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(HERE, "Retic-v2.2.0")
DEFAULT_OUT = os.path.join(HERE, "Retic-v2.2.0.zip")

# ── 排除规则 ──────────────────────────────────────────────────────────────
EXCLUDE_DIR_PREFIXES = ("reports/", "logs/", "执行日志/", "data/",
                        "__pycache__/", ".git/", ".idea/")
EXCLUDE_FILE_NAMES = {"topology_layout.json", "topology.json", "topology.bak.json"}
EXCLUDE_SUFFIXES = (".log", ".db", ".sqlite", ".sqlite3", ".pyc", ".bak", ".tmp", ".swp")
EXCLUDE_DIR_MARKERS = ("/reports/", "/logs/", "/执行日志/", "/data/",
                       "/__pycache__/", "/.git/")

# 需要检查 / 脱敏的命令配置（含程序内置副本）
COMMAND_FILES = ("config/commands.txt", "_internal/config/commands.txt")

# 内置默认配置（程序可安全读取的最小集）
FALLBACK_CONFIG = {
    "output_format": "HTML",
    "max_workers": 10,
    "retry_count": 0,
}

# 定制型号分组，例如 [huawei_s7706] / [zte_5950]
CUSTOM_GROUP_RE = re.compile(
    r"^\s*\[(?:huawei|h3c|cisco|ruijie|zte)_[a-z0-9_]+\]\s*$", re.I | re.M
)

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SENSITIVE_KEY_RE = re.compile(r"(devices|password|passwd|secret|设备清单|账号|凭据)", re.I)


def norm(path: str) -> str:
    """统一为 zip 内的 / 分隔符，且不带盘符 / 前导斜杠。"""
    return path.replace("\\", "/").lstrip("/")


def should_exclude(rel: str) -> bool:
    low = rel.lower()
    if low.startswith(EXCLUDE_DIR_PREFIXES):
        return True
    if any(m in "/" + low for m in EXCLUDE_DIR_MARKERS):
        return True
    if os.path.basename(low) in EXCLUDE_FILE_NAMES:
        return True
    if low.endswith(EXCLUDE_SUFFIXES):
        return True
    return False


def read_bytes(path: str):
    with open(path, "rb") as f:
        return f.read()


def clean_template_path(*parts) -> str:
    """始终从脚本旁的干净发布模板取默认值，绝不读可能被污染的源目录。"""
    return os.path.join(HERE, "Retic-v2.2.0", *parts)


def default_config_bytes() -> bytes:
    """内置默认配置（无设备、无 IP、无密码）。"""
    example = clean_template_path("_internal", "config", "config.example.json")
    if os.path.isfile(example):
        return read_bytes(example)
    return (json.dumps(FALLBACK_CONFIG, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


GENERIC_COMMANDS_FALLBACK = """[default]
show version
show ip interface brief
show interfaces
show ip route
show arp
show logging
"""


def generic_commands_bytes() -> bytes:
    """通用命令模板；若模板自身被定制过，退回最小兜底。"""
    example = clean_template_path("_internal", "config", "commands.txt")
    if os.path.isfile(example):
        data = read_bytes(example)
        if not CUSTOM_GROUP_RE.search(data.decode("utf-8", errors="ignore")):
            return data
    return GENERIC_COMMANDS_FALLBACK.encode("utf-8")


def build(src: str, out: str) -> str:
    if not os.path.isdir(src):
        raise SystemExit(f"[错误] 源目录不存在：{src}")

    tmp = out + ".tmp"
    skipped = []
    forced = []
    count = 0

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, dirs, files in os.walk(src):
            dirs.sort()
            files.sort()
            for name in files:
                full = os.path.join(base, name)
                rel = norm(os.path.relpath(full, src))
                if should_exclude(rel):
                    skipped.append(rel)
                    continue

                data = read_bytes(full)
                # 强制脱敏：运行配置 + 两处命令配置
                if rel == "config/config.json":
                    data = default_config_bytes()
                    forced.append(rel + " → 内置默认模板")
                elif rel in COMMAND_FILES and CUSTOM_GROUP_RE.search(
                    data.decode("utf-8", errors="ignore")
                ):
                    data = generic_commands_bytes()
                    forced.append(rel + " → 通用模板（检测到定制型号分组）")

                z.writestr(rel, data)
                count += 1

    # 先校验临时包，通过才替换正式文件
    problems = verify(tmp)
    if problems:
        try:
            os.remove(tmp)
        except OSError:
            pass
        print("[校验失败] 已丢弃临时包，未生成发布 zip：")
        for p in problems:
            print("   ✗ " + p)
        raise SystemExit(1)

    os.replace(tmp, out)
    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"[打包完成] {out}")
    print(f"   文件 {count} 个，{size_mb:.2f} MB")
    print(f"   已排除 {len(skipped)} 个运行期数据文件" + (f"（示例：{skipped[:3]}）" if skipped else ""))
    for f in forced:
        print(f"   已强制脱敏：{f}")
    print("[校验通过] 未发现设备清单 / 报告 / 日志 / 拓扑布局等现网数据")
    print("提示：前端与 Excel 模板中的占位示例文字（如区域名示例）属于产品文案，"
          "如需一并更换请修改前端源码后重新构建。")
    return out


def verify(zip_path: str):
    """返回问题列表；为空表示干净。"""
    problems = []
    try:
        z = zipfile.ZipFile(zip_path)
    except Exception as e:
        return [f"无法读取 zip：{e}"]

    names = z.namelist()
    if any("\\" in n for n in names):
        problems.append("zip 内存在反斜杠路径（不符合标准）")

    for n in names:
        low = n.lower()
        if should_exclude(low):
            problems.append(f"含被排除的运行期数据文件：{n}")
        if "topology" in low and low.endswith(".json"):
            problems.append(f"含拓扑数据文件：{n}")
        if re.search(r"[\u4e00-\u9fff\w\-]+_\d{8}_\d{6}\.(txt|html)$", n):
            problems.append(f"疑似巡检报告文件：{n}")

    required = ("Retic.exe", "config/commands.txt", "_internal/frontend/dist/index.html")
    for r in required:
        if r not in names:
            problems.append(f"缺少程序必需文件：{r}")

    # config/config.json 内容检查
    if "config/config.json" in names:
        raw = z.read("config/config.json").decode("utf-8", errors="ignore")
        try:
            j = json.loads(raw)
            for k, v in (j.items() if isinstance(j, dict) else []):
                if SENSITIVE_KEY_RE.search(k) and v not in (None, "", [], {}, False, 0):
                    problems.append(f"config.json 含非空敏感字段：{k}")
            if IP_RE.search(raw):
                problems.append("config.json 含 IP 地址")
        except Exception:
            problems.append("config.json 不是合法 JSON")

    # 命令配置（含内置副本）不应含定制型号分组
    for cf in COMMAND_FILES:
        if cf in names:
            cmd = z.read(cf).decode("utf-8", errors="ignore")
            if CUSTOM_GROUP_RE.search(cmd):
                problems.append(f"{cf} 含定制「厂商_型号」分组")

    z.close()
    return problems


def check_only(zip_path: str):
    if not os.path.isfile(zip_path):
        raise SystemExit(f"[错误] 文件不存在：{zip_path}")
    problems = verify(zip_path)
    z = zipfile.ZipFile(zip_path)
    print(f"[校验] {zip_path}")
    print(f"   文件 {len(z.namelist())} 个，{os.path.getsize(zip_path)/1024/1024:.2f} MB")
    z.close()
    if problems:
        print("[校验失败]")
        for p in problems:
            print("   ✗ " + p)
        raise SystemExit(1)
    print("[校验通过] 未发现设备清单 / 报告 / 日志 / 拓扑布局等现网数据")
    print("提示：前端与 Excel 模板中的占位示例文字属于产品文案，不在此校验范围内。")


def main():
    ap = argparse.ArgumentParser(description="Retic 发布包构建（自动剔除现网数据）")
    ap.add_argument("--src", default=DEFAULT_SRC, help="发布模板目录")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 zip 路径")
    ap.add_argument("--check", metavar="ZIP", help="只校验已有 zip，不重新打包")
    args = ap.parse_args()

    if args.check:
        check_only(args.check)
    else:
        build(args.src, args.out)


if __name__ == "__main__":
    main()
