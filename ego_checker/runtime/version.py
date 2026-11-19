#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# runtime/version.py — 分析报告版本标识
#
# 本包内嵌于 EGO Courier，报告中的版本号跟随 EGO Courier 的应用版本，
# 不再单独维护 version.toml，避免两处版本不一致。
# ============================================================================

import sys


def _courier_version():
    try:
        import ego_relay
        return str(getattr(ego_relay, "__version__", ""))
    except Exception:  # noqa: BLE001 - 独立运行时没有 ego_relay
        return ""


def load_version(path=None):
    """返回分析工具版本字符串（编辑为 EGO Courier 版本）；缺失时给出明确占位。"""
    version = _courier_version().strip()
    return version or "未识别版本"


APP_VERSION = load_version()


def load_build_date(path=None):
    """构建日期：源码运行不借用旧构建或当前日期。"""
    return "打包运行" if getattr(sys, "frozen", False) else "源码运行"


BUILD_DATE = load_build_date()


def application_title():
    return f"数采分析      {APP_VERSION} {BUILD_DATE}"


def package_directory_name(version=APP_VERSION):
    return f"EGOChecker_{version}"


__all__ = ["load_version", "APP_VERSION", "load_build_date", "BUILD_DATE",
           "application_title", "package_directory_name"]
