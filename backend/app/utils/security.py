# -*- coding: utf-8 -*-
"""
安全防御工具模块 - Security Utilities
包含 SSRF 防护、URL 白名单校验与安全检查
"""

import ipaddress
import logging
import socket
import urllib.parse
from typing import Tuple

logger = logging.getLogger(__name__)


def validate_safe_external_url(url: str) -> Tuple[bool, str]:
    """
    深度校验外呼 URL，防止服务端请求伪造 (SSRF)
    拦截内网私有地址、云元数据接口 (169.254.169.254)、回环地址及保留网段

    Args:
        url: 待校验的外部 API 地址

    Returns:
        (is_safe, error_message): 是否安全及拦截原因
    """
    if not url or not isinstance(url, str):
        return False, "URL 不能为空"

    url_str = url.strip()
    try:
        parsed = urllib.parse.urlparse(url_str)
    except Exception as e:
        return False, f"URL 格式解析失败: {e}"

    if parsed.scheme.lower() not in ("http", "https"):
        return False, f"仅支持 HTTP/HTTPS 协议，当前协议为: {parsed.scheme}"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL 缺少有效的主机名"

    lower_host = hostname.lower()

    # 1. 拦截直接的本地回环与云元数据标识
    blocked_hosts = {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "metadata.google.internal",
        "instance-data",
    }
    if lower_host in blocked_hosts or lower_host.endswith(".local") or lower_host.endswith(".internal"):
        return False, f"安全拦截：禁止连接本地或内网域名 [{hostname}]"

    # 2. DNS 解析并检测真实 IP 地址（防 DNS 重绑定与内网别名解析）
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        addr_infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        logger.warning(f"SSRF 检查 DNS 解析异常 [{hostname}]: {e}")
        return False, f"无法解析主机名 [{hostname}]: {e}"
    except Exception as e:
        logger.warning(f"SSRF 检查主机解析错误 [{hostname}]: {e}")
        return False, f"主机解析错误: {e}"

    for family, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"非法 IP 地址: {ip_str}"

        if ip.is_loopback:
            return False, f"安全拦截：禁止连接本地回环 IP [{ip_str}]"
        if ip.is_private:
            return False, f"安全拦截：禁止连接内网私有 IP [{ip_str}]"
        if ip.is_link_local:
            return False, f"安全拦截：禁止连接链路本地/云元数据 IP [{ip_str}]"
        if ip.is_multicast:
            return False, f"安全拦截：禁止连接组播 IP [{ip_str}]"
        if ip.is_reserved:
            return False, f"安全拦截：禁止连接保留网段 IP [{ip_str}]"

    return True, ""
