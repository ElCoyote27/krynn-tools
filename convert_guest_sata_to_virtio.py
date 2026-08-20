#!/usr/bin/env python3
"""Convert KVM guest SATA disks to virtio with iothreads.

Usage:
    convert_sata_to_virtio.py [--dry-run] VM [VM ...]
    convert_sata_to_virtio.py [--dry-run] --all

Backs up each domain XML before changes.
Rollback: virsh define /root/VMNAME.xml.pre-virtio.TIMESTAMP
"""

import argparse
import datetime
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter

LETTERS = "abcdefghijklmnopqrstuvwxyz"

EXCLUDE_VMS = {
    "dsm7", "freenas-11",
    "win10-x64", "win11-x64",
    "rhel3-x86", "rhel4-x86",
    "rhel5-x86", "rhel5-x64",
}
EXCLUDE_PREFIXES = ("overcloud-", "instack-")

VIRTIO_DRIVER = {
    "name": "qemu",
    "type": "qcow2",
    "cache": "writethrough",
    "discard": "ignore",
    "detect_zeroes": "off",
    "queues": "4",
}


def vd(idx):
    if idx >= len(LETTERS):
        raise SystemExit("ERROR: too many virtio disks (>26)")
    return "vd" + LETTERS[idx]


def is_excluded(name):
    if name in EXCLUDE_VMS:
        return True
    for pfx in EXCLUDE_PREFIXES:
        if name.startswith(pfx):
            return True
    return False


def classify_disk(elem):
    """Return 'boot', 'data', or 'cdrom'."""
    if elem.get("device") == "cdrom":
        return "cdrom"
    source = elem.find("source")
    if source is None or not source.get("file"):
        return "cdrom"
    basename = os.path.basename(source.get("file"))
    if "c0t" in basename and "d0" in basename:
        return "boot"
    return "data"


def used_pci(devices):
    """Set of (bus_str, slot_int) tuples."""
    out = set()
    for a in devices.iter("address"):
        if a.get("type") == "pci":
            out.add((a.get("bus", "0x00"), int(a.get("slot", "0"), 16)))
    return out


def free_pci_slots(devices, n):
    """Return list of n (bus_str, slot_hex_str) tuples."""
    taken = used_pci(devices)
    free = []
    for slot in range(2, 31):
        if ("0x00", slot) not in taken:
            free.append(("0x00", "0x%02x" % slot))
            if len(free) >= n:
                return free
    for slot in range(1, 31):
        if ("0x01", slot) not in taken:
            free.append(("0x01", "0x%02x" % slot))
            if len(free) >= n:
                return free
    for slot in range(1, 31):
        if ("0x02", slot) not in taken:
            free.append(("0x02", "0x%02x" % slot))
            if len(free) >= n:
                return free
    raise SystemExit("Cannot find %d free PCI slots" % n)


def set_virtio_driver(driver_elem, iothread):
    for k, v in VIRTIO_DRIVER.items():
        driver_elem.set(k, v)
    driver_elem.set("iothread", str(iothread))


def set_pci_addr(disk_elem, bus_str, slot_str):
    addr = disk_elem.find("address")
    if addr is not None:
        disk_elem.remove(addr)
    addr = ET.SubElement(disk_elem, "address")
    for k, v in [("type", "pci"), ("domain", "0x0000"),
                 ("bus", bus_str), ("slot", slot_str),
                 ("function", "0x0")]:
        addr.set(k, v)


def convert_vm(name, dry_run=False):
    hdr = "=" * 60
    print(hdr)
    print("VM: %s%s" % (name, " [DRY RUN]" if dry_run else ""))
    print(hdr)

    try:
        state = subprocess.check_output(
            ["virsh", "domstate", name], text=True).strip()
    except subprocess.CalledProcessError:
        print("  NOT FOUND")
        return False

    if state != "shut off":
        print("  SKIP (state=%s, must be shut off)" % state)
        return False

    xml_text = subprocess.check_output(
        ["virsh", "dumpxml", name], text=True)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = "/root/%s.xml.pre-virtio.%s" % (name, stamp)
    with open(backup, "w") as f:
        f.write(xml_text)
    print("  BACKUP  %s" % backup)

    root = ET.fromstring(xml_text)
    devices = root.find("devices")

    all_disk = [d for d in list(devices)
                if d.tag.split("}")[-1] == "disk"]
    all_ctrl = [c for c in list(devices)
                if c.tag.split("}")[-1] == "controller"]

    boot, data, cdrom = [], [], []
    for d in all_disk:
        cat = classify_disk(d)
        t = d.find("target")
        s = d.find("source")
        dev = t.get("dev", "?")
        bus = t.get("bus", "?")
        src = os.path.basename(s.get("file", "-")) if s is not None else "-"
        {"boot": boot, "data": data, "cdrom": cdrom}[cat].append(d)
        print("  %-5s  %-4s bus=%-6s %s" % (cat, dev, bus, src))

    sata_boot = [d for d in boot
                 if d.find("target").get("bus") == "sata"]
    sata_data = [d for d in data
                 if d.find("target").get("bus") == "sata"]

    if not sata_boot and not sata_data:
        print("  SKIP (no SATA disks to convert)")
        return True

    # ---- remove spare SATA controllers (keep idx 0 for CD-ROM) ----------
    for c in list(all_ctrl):
        if c.get("type") == "sata" and c.get("index") not in (None, "0"):
            a = c.find("address")
            sl = a.get("slot", "?") if a is not None else "?"
            devices.remove(c)
            print("  REMOVE sata-ctrl idx=%s slot=%s" % (
                c.get("index"), sl))

    # ---- iothread arithmetic --------------------------------------------
    max_data_iot = 0
    for d in data:
        dr = d.find("driver")
        if dr is not None and dr.get("iothread"):
            max_data_iot = max(max_data_iot, int(dr.get("iothread")))

    if max_data_iot == 0 and data:
        max_data_iot = min(4, len(data))

    if not data and not boot:
        max_data_iot = 0

    boot_iot_start = max_data_iot + 1
    total_iot = max_data_iot + len(boot)

    iot_el = root.find("iothreads")
    if iot_el is None:
        # insert after vcpu so placement is sane
        vcpu = root.find("vcpu")
        idx = list(root).index(vcpu) + 1 if vcpu is not None else 2
        iot_el = ET.Element("iothreads")
        root.insert(idx, iot_el)
    old_iot = iot_el.text or "0"
    iot_el.text = str(total_iot)
    print("  iothreads %s -> %s  (data 1-%d  boot %d-%d)" % (
        old_iot, total_iot, max_data_iot,
        boot_iot_start, boot_iot_start + len(boot) - 1))

    # ---- find free PCI slots for SATA->virtio disks ---------------------
    needed = len(sata_boot) + len(sata_data)
    slots = free_pci_slots(devices, needed) if needed else []
    si = 0  # slot index

    # ---- convert boot disks ---------------------------------------------
    for i, d in enumerate(boot):
        t = d.find("target")
        old_dev, old_bus = t.get("dev"), t.get("bus")
        t.set("dev", vd(i))
        t.set("bus", "virtio")
        iot = boot_iot_start + i
        dr = d.find("driver")
        set_virtio_driver(dr, iot)
        if old_bus == "sata":
            bus_s, slot_s = slots[si]; si += 1
            set_pci_addr(d, bus_s, slot_s)
            addr_msg = " pci %s:%s" % (bus_s, slot_s)
        else:
            addr_msg = ""
        src = os.path.basename(
            d.find("source").get("file", "?"))
        print("  BOOT  %s -> %s  iot=%d%s  %s" % (
            old_dev, vd(i), iot, addr_msg, src))

    # ---- shift + convert data disks -------------------------------------
    data_xml = [d for d in all_disk if d in data]
    for i, d in enumerate(data_xml):
        ni = len(boot) + i
        t = d.find("target")
        old_dev, old_bus = t.get("dev"), t.get("bus")
        t.set("dev", vd(ni))
        t.set("bus", "virtio")
        dr = d.find("driver")
        if old_bus == "sata" or not dr.get("iothread"):
            iot = (i % max_data_iot) + 1 if max_data_iot else 1
        else:
            iot = int(dr.get("iothread"))
        set_virtio_driver(dr, iot)
        if old_bus == "sata":
            bus_s, slot_s = slots[si]; si += 1
            set_pci_addr(d, bus_s, slot_s)
            addr_msg = " pci %s:%s" % (bus_s, slot_s)
        else:
            addr_msg = ""
        src = os.path.basename(
            d.find("source").get("file", "?"))
        print("  DATA  %s -> %s  iot=%d%s  %s" % (
            old_dev, vd(ni), iot, addr_msg, src))

    # ---- PCI collision check --------------------------------------------
    pci_keys = []
    for a in devices.iter("address"):
        if a.get("type") == "pci":
            pci_keys.append(
                (a.get("bus"), a.get("slot"), a.get("function")))
    dups = [k for k, v in Counter(pci_keys).items() if v > 1]
    if dups:
        print("  ERROR: PCI collision %s" % dups)
        return False
    print("  PCI OK  %d slots, 0 collisions" % len(pci_keys))

    # ---- write / define --------------------------------------------------
    new_xml = ("<?xml version='1.0' encoding='utf-8'?>\n"
               + ET.tostring(root, encoding="unicode") + "\n")
    suffix = ".dryrun" if dry_run else ""
    out_path = "/root/%s.xml.new-virtio.%s%s" % (name, stamp, suffix)
    with open(out_path, "w") as f:
        f.write(new_xml)

    if dry_run:
        print("  DRY RUN  wrote %s (not defined)" % out_path)
    else:
        subprocess.check_call(
            ["virsh", "define", "--validate", out_path])
        after = subprocess.check_output(
            ["virsh", "domblklist", name], text=True)
        print("  DEFINED  domblklist:")
        for ln in after.strip().split("\n"):
            print("    %s" % ln)
        print("  ROLLBACK virsh define %s" % backup)

    print()
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Convert SATA disks to virtio")
    ap.add_argument("vms", nargs="*",
                    help="VM name(s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="generate XML but do not virsh define")
    ap.add_argument("--all", action="store_true",
                    help="all shut-off VMs (respects exclude list)")
    args = ap.parse_args()

    if args.all:
        raw = subprocess.check_output(
            ["virsh", "list", "--all", "--name"], text=True)
        args.vms = [v.strip() for v in raw.strip().split("\n")
                    if v.strip()]

    if not args.vms:
        ap.error("specify VM name(s) or --all")

    ok, skip, fail = [], [], []
    for vm in sorted(set(args.vms)):
        if is_excluded(vm):
            print("EXCLUDED %s" % vm)
            skip.append(vm)
            continue
        try:
            if convert_vm(vm, dry_run=args.dry_run):
                ok.append(vm)
            else:
                fail.append(vm)
        except Exception as exc:
            print("  EXCEPTION: %s" % exc)
            import traceback; traceback.print_exc()
            fail.append(vm)

    print("=" * 60)
    print("SUMMARY  ok=%d  skip=%d  fail=%d" % (
        len(ok), len(skip), len(fail)))
    for v in ok:
        print("  OK       %s" % v)
    for v in skip:
        print("  EXCLUDED %s" % v)
    for v in fail:
        print("  FAIL     %s" % v)
    print("=" * 60)
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
