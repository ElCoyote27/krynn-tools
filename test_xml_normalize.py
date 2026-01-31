#!/usr/bin/env python3
"""
Test script for XML normalization in rsync_KVM_OS.py

Tests that machine type variants are properly normalized so that
RHEL and Fedora XMLs compare as equal when only machine type differs.

Run as: python3 test_xml_normalize.py
(Does not require root - just tests string manipulation)
"""

import re
import sys

def normalize_xml_content(xml_content: str) -> str:
    """
    Normalize XML content by applying machine type transformations.
    (Same function as in rsync_KVM_OS.py)
    """
    normalized = re.sub(r'pc-i440fx-[a-zA-Z0-9._-]*', 'pc', xml_content)
    normalized = re.sub(r'pc-q35-[a-zA-Z0-9._-]*', 'q35', normalized)
    return normalized


def test_machine_type_normalization():
    """Test that various machine type strings normalize correctly."""
    print("=" * 60)
    print("Testing machine type normalization")
    print("=" * 60)
    
    test_cases = [
        # (input, expected_output, description)
        ("pc-q35-rhel9.2.0", "q35", "RHEL 9 Q35"),
        ("pc-q35-rhel8.6.0", "q35", "RHEL 8 Q35"),
        ("pc-q35-rhel7.6.0", "q35", "RHEL 7 Q35"),
        ("pc-q35-8.2", "q35", "Fedora/upstream Q35 numeric"),
        ("pc-q35-fedora39", "q35", "Fedora Q35 named"),
        ("pc-q35-6.2", "q35", "Older QEMU Q35"),
        ("pc-i440fx-rhel7.6.0", "pc", "RHEL 7 i440fx"),
        ("pc-i440fx-8.2", "pc", "Fedora/upstream i440fx"),
        ("pc-i440fx-2.11", "pc", "Older QEMU i440fx"),
        ("q35", "q35", "Already normalized Q35"),
        ("pc", "pc", "Already normalized PC"),
    ]
    
    all_passed = True
    for input_str, expected, description in test_cases:
        result = normalize_xml_content(input_str)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            all_passed = False
        print(f"  [{status}] {description}: '{input_str}' -> '{result}' (expected '{expected}')")
    
    return all_passed


def test_xml_snippet_comparison():
    """Test that XML snippets with different machine types compare equal after normalization."""
    print("\n" + "=" * 60)
    print("Testing XML snippet comparison")
    print("=" * 60)
    
    # Simulate a RHEL source XML
    rhel_xml = '''<domain type='kvm'>
  <name>test-vm</name>
  <memory unit='KiB'>4194304</memory>
  <os>
    <type arch='x86_64' machine='pc-q35-rhel9.2.0'>hvm</type>
    <boot dev='hd'/>
  </os>
  <devices>
    <emulator>/usr/libexec/qemu-kvm</emulator>
  </devices>
</domain>'''

    # Simulate what Fedora might produce after virsh define
    fedora_xml = '''<domain type='kvm'>
  <name>test-vm</name>
  <memory unit='KiB'>4194304</memory>
  <os>
    <type arch='x86_64' machine='pc-q35-8.2'>hvm</type>
    <boot dev='hd'/>
  </os>
  <devices>
    <emulator>/usr/libexec/qemu-kvm</emulator>
  </devices>
</domain>'''

    rhel_normalized = normalize_xml_content(rhel_xml)
    fedora_normalized = normalize_xml_content(fedora_xml)
    
    print(f"\n  RHEL XML machine type:   'pc-q35-rhel9.2.0'")
    print(f"  Fedora XML machine type: 'pc-q35-8.2'")
    print(f"  After normalization:     both become 'q35'")
    
    if rhel_normalized == fedora_normalized:
        print(f"\n  [PASS] XMLs compare EQUAL after normalization (no unnecessary sync)")
        return True
    else:
        print(f"\n  [FAIL] XMLs still differ after normalization!")
        print(f"\n  --- RHEL normalized ---")
        print(rhel_normalized)
        print(f"\n  --- Fedora normalized ---")
        print(fedora_normalized)
        return False


def test_real_change_detected():
    """Test that real changes (not machine type) are still detected."""
    print("\n" + "=" * 60)
    print("Testing that real changes ARE detected")
    print("=" * 60)
    
    # Original XML
    original_xml = '''<domain type='kvm'>
  <name>test-vm</name>
  <memory unit='KiB'>4194304</memory>
  <os>
    <type arch='x86_64' machine='pc-q35-rhel9.2.0'>hvm</type>
  </os>
</domain>'''

    # Changed XML (memory increased)
    changed_xml = '''<domain type='kvm'>
  <name>test-vm</name>
  <memory unit='KiB'>8388608</memory>
  <os>
    <type arch='x86_64' machine='pc-q35-8.2'>hvm</type>
  </os>
</domain>'''

    original_normalized = normalize_xml_content(original_xml)
    changed_normalized = normalize_xml_content(changed_xml)
    
    print(f"\n  Original memory: 4194304 KiB")
    print(f"  Changed memory:  8388608 KiB")
    print(f"  (Machine types also differ but should be ignored)")
    
    if original_normalized != changed_normalized:
        print(f"\n  [PASS] Real change detected (sync will happen)")
        return True
    else:
        print(f"\n  [FAIL] Real change NOT detected (would skip sync incorrectly!)")
        return False


def test_with_real_vm_xml():
    """Test with a real VM XML file if available."""
    print("\n" + "=" * 60)
    print("Testing with real VM XML files (if available)")
    print("=" * 60)
    
    import os
    test_files = [
        "/etc/libvirt/qemu/cirros.xml",
        "/etc/libvirt/qemu/dc00.xml",
        "/etc/libvirt/qemu/fedora-x64.xml",
    ]
    
    found_any = False
    for xml_path in test_files:
        if os.path.exists(xml_path):
            found_any = True
            try:
                with open(xml_path, 'r') as f:
                    content = f.read()
                
                # Find machine type in original
                import re
                match = re.search(r"machine='([^']+)'", content)
                original_machine = match.group(1) if match else "(not found)"
                
                # Normalize
                normalized = normalize_xml_content(content)
                match = re.search(r"machine='([^']+)'", normalized)
                normalized_machine = match.group(1) if match else "(not found)"
                
                print(f"\n  File: {xml_path}")
                print(f"    Original machine type:   '{original_machine}'")
                print(f"    Normalized machine type: '{normalized_machine}'")
                
            except PermissionError:
                print(f"\n  File: {xml_path}")
                print(f"    (Permission denied - run as root to test)")
            except Exception as e:
                print(f"\n  File: {xml_path}")
                print(f"    Error: {e}")
    
    if not found_any:
        print("\n  No test XML files found (this is OK)")
    
    return True


def main():
    print("\nrsync_KVM_OS.py XML Normalization Test Suite")
    print("=" * 60)
    
    results = []
    results.append(("Machine type normalization", test_machine_type_normalization()))
    results.append(("XML snippet comparison", test_xml_snippet_comparison()))
    results.append(("Real change detection", test_real_change_detected()))
    results.append(("Real VM XML files", test_with_real_vm_xml()))
    
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    
    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"  [{status}] {name}")
    
    print("\n" + "=" * 60)
    if all_passed:
        print("All tests PASSED!")
        return 0
    else:
        print("Some tests FAILED!")
        return 1


if __name__ == '__main__':
    sys.exit(main())
