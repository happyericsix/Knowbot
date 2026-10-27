"""C1 USB 传感器：身份解析纯函数测试（WndProc 链路需真实插拔，线下验证）。"""
from knowbot.sensors.winmsg import parse_usb_ids


def test_parse_vid_pid_from_interface_path():
    path = "\\\\?\\usb#vid_8087&pid_0026#5&2f31c4f&0&1#{a5dcbf10-6530-11d2-901f-00c04fb951ed}"
    assert parse_usb_ids(path) == ("8087", "0026")


def test_parse_lowercase_and_uppercase():
    path = "\\\\?\\usb#vid_0A12&pid_0001#serial#{guid}"
    assert parse_usb_ids(path) == ("0A12", "0001")


def test_parse_no_match():
    assert parse_usb_ids("\\\\?\\hid#some_device#{guid}") == (None, None)
    assert parse_usb_ids("") == (None, None)
    assert parse_usb_ids(None) == (None, None)


def test_path_contains_instance_serial():
    """接口路径含实例串——比音频端点 GUID 更稳的设备身份（实例级记忆的 key 素材）。"""
    path = "\\\\?\\usb#vid_1234&pid_5678#SER1234567890#0#{a5dcbf10-6530-11d2-901f-00c04fb951ed}"
    vid, pid = parse_usb_ids(path)
    assert (vid, pid) == ("1234", "5678")
    assert "SER1234567890" in path
