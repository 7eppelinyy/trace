"""测试测试隔离性：外网套接字阻断与凭据屏蔽验证。"""

import socket
import pytest


def test_external_network_blocked():
    """测试离线测试套件下，尝试连接外部公网 IP/域名会立即被拦截抛出异常。"""
    s = socket.socket()
    with pytest.raises(RuntimeError) as exc_info:
        s.connect(("8.8.8.8", 53))
    assert "External network connection to 8.8.8.8 is blocked" in str(exc_info.value)
    s.close()
