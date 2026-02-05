#!/usr/bin/env python3
#
# Converted from the original VMware_Find_Next_MACs.sh.
# Finds the next free MACs within a KVM OUI/prefix while avoiding
# collisions with VMware and KVM guests.
#

import argparse
import glob
import os
import re
import shutil
import sys
from fnmatch import fnmatchcase
from xml.etree import ElementTree


DEFAULT_VMWARE_DIR = "/shared/vmware0/OS"
DEFAULT_KVM_DIR = "/etc/libvirt/qemu"
DEFAULT_PREFIX = "52:54:00"
DEFAULT_VM_JUMP = 100
DEFAULT_VM_STEP = 10
DEFAULT_VM_COUNT = 1
DEFAULT_IF_COUNT = 4
DEFAULT_EXCLUDE_NAMES = ["instack*"]


VMWARE_MAC_RE = re.compile(
    r'ethernet\d+\.(?:generatedAddress|address)\s*=\s*"([^"]+)"',
    re.IGNORECASE,
)
VMWARE_NAME_RE = re.compile(r'^\s*displayName\s*=\s*"([^"]+)"', re.IGNORECASE)


def normalize_mac(value):
    cleaned = value.strip().replace(":", "").replace("-", "")
    if len(cleaned) != 12:
        return None
    try:
        int(cleaned, 16)
    except ValueError:
        return None
    return cleaned.upper()


def format_mac(hex12):
    return ":".join(hex12[i : i + 2] for i in range(0, 12, 2))


def parse_prefix(prefix):
    parts = [p for p in prefix.split(":") if p]
    if not parts:
        raise ValueError("Prefix is empty")
    for part in parts:
        if len(part) != 2:
            raise ValueError("Prefix must be bytes like 52:54 or 52:54:00")
    prefix_bytes = [int(p, 16) for p in parts]
    prefix_bits = 8 * len(prefix_bytes)
    if prefix_bits >= 48:
        raise ValueError("Prefix must be fewer than 6 bytes")
    prefix_value = 0
    for b in prefix_bytes:
        prefix_value = (prefix_value << 8) | b
    prefix_value_shifted = prefix_value << (48 - prefix_bits)
    prefix_mask = ((1 << prefix_bits) - 1) << (48 - prefix_bits)
    prefix_max = prefix_value_shifted | ((1 << (48 - prefix_bits)) - 1)
    return prefix_value_shifted, prefix_mask, prefix_max, prefix_bits


def matches_prefix(mac_int, prefix_value, prefix_mask):
    return (mac_int & prefix_mask) == prefix_value


def extract_vmware_macs(vmx_path):
    macs = []
    display_name = None
    try:
        with open(vmx_path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if display_name is None:
                    name_match = VMWARE_NAME_RE.search(line)
                    if name_match:
                        display_name = name_match.group(1).strip()
                match = VMWARE_MAC_RE.search(line)
                if not match:
                    continue
                normalized = normalize_mac(match.group(1))
                if normalized:
                    macs.append(
                        (
                            normalized,
                            f"vmware:{display_name or os.path.basename(vmx_path)}:{vmx_path}",
                            display_name or os.path.basename(vmx_path),
                        )
                    )
    except PermissionError:
        raise
    except OSError as exc:
        print(f"Warning: failed to read {vmx_path}: {exc}", file=sys.stderr)
    return macs


def extract_kvm_macs(xml_path):
    macs = []
    try:
        tree = ElementTree.parse(xml_path)
    except PermissionError:
        raise
    except (ElementTree.ParseError, OSError) as exc:
        print(f"Warning: failed to parse {xml_path}: {exc}", file=sys.stderr)
        return macs
    root = tree.getroot()
    name_node = root.find("name")
    guest_name = name_node.text.strip() if name_node is not None and name_node.text else None
    for mac_node in root.iter("mac"):
        address = mac_node.get("address")
        if not address:
            continue
        normalized = normalize_mac(address)
        if normalized:
            macs.append(
                (
                    normalized,
                    f"kvm:{guest_name or os.path.basename(xml_path)}:{xml_path}",
                    guest_name or os.path.basename(xml_path),
                )
            )
    return macs


def gather_macs(vmware_dir, kvm_dir):
    vmware_macs = []
    kvm_macs = []

    try:
        os.listdir(vmware_dir)
    except PermissionError:
        raise
    except FileNotFoundError:
        pass

    vmware_glob = os.path.join(vmware_dir, "**", "*.vmx")
    for vmx in glob.glob(vmware_glob, recursive=True):
        if os.path.isfile(vmx):
            vmware_macs.extend(extract_vmware_macs(vmx))

    try:
        os.listdir(kvm_dir)
    except PermissionError:
        raise
    except FileNotFoundError:
        pass

    kvm_glob = os.path.join(kvm_dir, "*.xml")
    for xml in glob.glob(kvm_glob):
        if os.path.isfile(xml):
            kvm_macs.extend(extract_kvm_macs(xml))

    return vmware_macs, kvm_macs


def find_next_block(start, used, if_count, vm_step, prefix_value, prefix_mask, prefix_max):
    base = start
    while True:
        last = base + (if_count - 1) * vm_step
        if last > prefix_max:
            raise RuntimeError("No space left within the requested prefix.")
        if not matches_prefix(base, prefix_value, prefix_mask):
            base = prefix_value
            continue
        if not matches_prefix(last, prefix_value, prefix_mask):
            base = prefix_value
            continue
        ok = True
        for index in range(if_count):
            candidate = base + index * vm_step
            if candidate in used:
                ok = False
                break
        if ok:
            return base
        base += 1


def find_duplicate_sources(mac_entries):
    sources = {}
    for mac, source, _name in mac_entries:
        sources.setdefault(mac, []).append(source)
    return {mac: items for mac, items in sources.items() if len(items) > 1}


def should_exclude_name(name, patterns):
    if not name:
        return False
    for pattern in patterns:
        if fnmatchcase(name, pattern):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Find next free MACs within a KVM OUI/prefix while avoiding "
            "VMware and KVM collisions."
        )
    )
    parser.add_argument("vmx_file", nargs="?", default=None, help="Optional vmx file")
    parser.add_argument("--vmware-dir", default=DEFAULT_VMWARE_DIR)
    parser.add_argument("--kvm-dir", default=DEFAULT_KVM_DIR)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--vm-count", type=int, default=DEFAULT_VM_COUNT)
    parser.add_argument("--if-count", type=int, default=DEFAULT_IF_COUNT)
    parser.add_argument("--vm-jump", type=int, default=DEFAULT_VM_JUMP)
    parser.add_argument("--vm-step", type=int, default=DEFAULT_VM_STEP)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--exclude-name",
        action="append",
        default=list(DEFAULT_EXCLUDE_NAMES),
        help="Glob pattern of guest names to ignore for duplicate detection",
    )
    args = parser.parse_args()

    try:
        prefix_value, prefix_mask, prefix_max, _prefix_bits = parse_prefix(args.prefix)
    except ValueError as exc:
        print(f"Invalid prefix: {exc}", file=sys.stderr)
        return 2

    try:
        vmware_macs, kvm_macs = gather_macs(args.vmware_dir, args.kvm_dir)
    except PermissionError:
        if os.geteuid() != 0:
            sudo_path = shutil.which("sudo")
            if sudo_path:
                os.execv(sudo_path, ["sudo", sys.executable] + sys.argv)
            print("Error: permission denied (sudo not found).", file=sys.stderr)
            return 1
        print("Error: permission denied while reading VM files.", file=sys.stderr)
        return 1
    all_entries = vmware_macs + kvm_macs
    filtered_entries = [
        entry
        for entry in all_entries
        if not should_exclude_name(entry[2], args.exclude_name)
    ]
    duplicates = find_duplicate_sources(filtered_entries)
    all_macs_list = [mac for mac, _source, _name in all_entries]
    all_macs = set(all_macs_list)
    used = set(int(mac, 16) for mac in all_macs)
    prefix_used = [mac for mac in used if matches_prefix(mac, prefix_value, prefix_mask)]
    start = max(prefix_used) + 1 if prefix_used else prefix_value

    if not args.quiet:
        print(f"# VMware MACs: {len(vmware_macs)}")
        print(f"# KVM MACs: {len(kvm_macs)}")
        print(f"# Using prefix: {args.prefix}")
        if args.exclude_name:
            print(
                "# Guest names excluded from duplicate detection: "
                f"{', '.join(args.exclude_name)}"
            )
        if duplicates:
            print("# Warning: duplicate MACs detected:")
            for mac in sorted(duplicates):
                print(f"  {format_mac(mac)} x{len(duplicates[mac])}")
                for source in sorted(duplicates[mac]):
                    print(f"    - {source}")

    current_start = start
    for vm_index in range(1, args.vm_count + 1):
        base = find_next_block(
            current_start,
            used,
            args.if_count,
            args.vm_step,
            prefix_value,
            prefix_mask,
            prefix_max,
        )
        print(f"# VM{vm_index}:")
        for if_index in range(args.if_count):
            candidate = base + if_index * args.vm_step
            mac_hex = f"{candidate:012X}"
            mac = format_mac(mac_hex)
            print(f"  iface{if_index}: {mac}")
            if args.vmx_file:
                print(
                    f"  sed -i -e 's@ethernet{if_index}.address =.*@"
                    f"ethernet{if_index}.address = \"{mac}\"@' {args.vmx_file}"
                )
        used.update(base + i * args.vm_step for i in range(args.if_count))
        current_start = base + args.vm_jump
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
