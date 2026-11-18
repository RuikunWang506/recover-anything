"""A camera or phone plugged in by USB talks PTP or MTP, not as a disk, so
`devices` used to show nothing and nobody said to take the card out. These
parse each system's own listing of USB devices (recorded shapes)."""
import json
import plistlib
import sys

import pytest

from recover_anything import cli, devices


def test_mac_finds_a_ptp_camera_and_an_mtp_phone_but_not_a_webcam_or_a_card_reader():
    tree = [
        {"IORegistryEntryName": "Canon Digital Camera", "USB Product Name": "Canon Digital Camera",
         "USB Vendor Name": "Canon Inc.", "IORegistryEntryChildren": [
             {"IOObjectClass": "IOUSBHostInterface", "bInterfaceClass": 6, "bInterfaceSubClass": 1}]},
        {"USB Product Name": "Pixel 8", "IORegistryEntryChildren": [
            {"bInterfaceClass": 255, "USB Interface Name": "MTP"},
            {"bInterfaceClass": 255, "USB Interface Name": "ADB Interface"}]},
        {"USB Product Name": "FaceTime HD Camera", "IORegistryEntryChildren": [{"bInterfaceClass": 14}]},
        {"USB Product Name": "SD Card Reader", "IORegistryEntryChildren": [{"bInterfaceClass": 8}]},
    ]
    found = devices._mac_usb_cameras(plistlib.loads(plistlib.dumps(tree)))
    assert [c["model"] for c in found] == ["Canon Digital Camera", "Pixel 8"]


@pytest.mark.skipif(sys.platform == "win32", reason="sysfs names hold ':', which Windows folders can't")
def test_linux_reads_sysfs(tmp_path):
    def dev(name, product, *ifaces):
        (tmp_path / name).mkdir()
        (tmp_path / name / "product").write_text(product + "\n")
        for i, (cls, label) in enumerate(ifaces):
            d = tmp_path / ("%s:1.%d" % (name, i))
            d.mkdir()
            (d / "bInterfaceClass").write_text(cls + "\n")
            if label:
                (d / "interface").write_text(label + "\n")
    dev("1-1", "iPhone", ("06", "PTP"), ("ff", "Apple USB Multiplexor"))
    dev("1-2", "SAMSUNG_Android", ("ff", "MTP"), ("ff", None))
    dev("1-3", "Mass Storage", ("08", None))
    assert [c["model"] for c in devices._linux_usb_cameras(str(tmp_path))] == ["iPhone", "SAMSUNG_Android"]


def test_windows_reads_wpd_devices_but_not_usb_sticks():
    one = json.dumps({"FriendlyName": "Galaxy S23", "Manufacturer": "Samsung",
                      "InstanceId": "USB\\VID_04E8&PID_6860&MI_00\\6&2F1B3C&0&0000"})
    assert devices._win_usb_cameras(one) == [{"model": "Galaxy S23", "vendor": "Samsung"}]
    stick = json.dumps([{"FriendlyName": "E:\\", "Manufacturer": None,
                         "InstanceId": "SWD\\WPDBUSENUM\\_??_USBSTOR#DISK&VEN_SANDISK&PROD_ULTRA#4C53&0#"}])
    assert devices._win_usb_cameras(stick) == []
    assert devices._win_usb_cameras("") == []


def test_devices_tells_you_to_use_a_card_reader(monkeypatch, capsys):
    monkeypatch.setattr(devices, "list_devices", lambda: [])
    monkeypatch.setattr(devices, "usb_cameras", lambda: [{"model": "Canon EOS R6", "vendor": "Canon"}])
    cli.main(["devices"])
    out = capsys.readouterr().out
    assert "Canon EOS R6 is plugged in as a phone or camera" in out and "card reader" in out
    cli.main(["devices", "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert listed == [dict(listed[0], path=None, kind="phone-or-camera", readable=False)]
