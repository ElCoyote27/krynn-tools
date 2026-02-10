#!/usr/bin/env python3
"""
TunedReconfig - Simple script to switch between tuned profiles

A utility to quickly switch between common tuned profiles:
- powersave
- virtual-host intel-sst

Skips profile changes on virtual machines unless --force is used.
"""

# $Id: TunedReconfig.py 1.06 2026/02/10 00:00:00 skip-virtual-systems Exp $
__version__ = "TunedReconfig.py 1.06 2026/02/10 00:00:00 skip-virtual-systems Exp"

#
# VERSION HISTORY:
# ================
#
# v1.06 (2026-02-10): Skip profile changes on virtual systems
#   - Added virtual machine detection via systemd-detect-virt
#   - Profile changes are skipped by default on virtual systems
#   - Added -f/--force flag to override virtual system detection
#   - Status display shows virtualization type when detected
#
# v1.05 (2025-09-11): Added accelerator-performance profile
#   - Added support for accelerator-performance profile with 'a' alias
#   - Enhanced auto-suggestion logic to handle five profiles
#   - Updated help text and examples with accelerator-performance
#   - Added cron usage example for accelerator-performance
#
# v1.04 (2025-09-11): Added latency-performance profile
#   - Added support for latency-performance profile with 'l' alias
#   - Removed 'intel' shortcut from virtual-host intel-sst mappings
#   - Enhanced auto-suggestion logic to handle four profiles
#   - Updated help text and examples
#
# v1.02 (2025-09-11): Added throughput-performance profile
#   - Added support for throughput-performance profile with 't' alias
#   - Enhanced auto-suggestion logic to handle three profiles
#   - Updated help text and examples
#
# v1.01 (2025-09-04): Cron compatibility fixes
#   - Fixed PATH issues by using full path detection for tuned-adm
#   - Conditional sudo usage - skip sudo when already running as root
#   - Enhanced error messages with path information
#   - Improved cron environment compatibility
#
# v1.00 (2025-09-04): Initial release
#   - Simple profile switching between powersave and virtual-host intel-sst
#   - Support for short aliases (p/v) and full profile names
#   - Uses sudo for privilege escalation with tuned-adm
#   - Status display and toggle suggestions
#   - Command line argument support with help text
#

import subprocess
import sys
import argparse
import os
import shutil

PROFILES = {
    'p': 'powersave',
    'power': 'powersave',
    'powersave': 'powersave',
    'v': 'virtual-host intel-sst',
    'virtual': 'virtual-host intel-sst',
    'virtual-host': 'virtual-host intel-sst',
    't': 'throughput-performance',
    'throughput': 'throughput-performance',
    'throughput-performance': 'throughput-performance',
    'perf': 'throughput-performance',
    'l': 'latency-performance',
    'latency': 'latency-performance',
    'latency-performance': 'latency-performance',
    'a': 'accelerator-performance',
    'accelerator': 'accelerator-performance',
    'accelerator-performance': 'accelerator-performance'
}

def find_tuned_adm():
    """Find the full path to tuned-adm binary"""
    # First try shutil.which with current PATH
    tuned_path = shutil.which('tuned-adm')
    if tuned_path:
        return tuned_path

    # If not found, check common locations
    common_paths = ['/usr/sbin/tuned-adm', '/sbin/tuned-adm', '/usr/bin/tuned-adm']
    for path in common_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    # Last resort: return the name and hope it's in PATH
    return 'tuned-adm'

def is_root():
    """Check if running as root"""
    return os.getuid() == 0

def is_virtual():
    """Detect if running on a virtual machine.

    Uses systemd-detect-virt which returns the virtualization
    technology name (kvm, vmware, xen, etc.) with exit code 0
    if virtualized, or 'none' with non-zero exit code if
    running on bare metal.

    Returns a tuple of (is_vm, virt_type) where is_vm is a
    boolean and virt_type is the detected virtualization type
    string (or None if bare metal).
    """
    detect_virt = shutil.which('systemd-detect-virt')
    if not detect_virt:
        # Check common locations
        for path in ['/usr/bin/systemd-detect-virt',
                     '/bin/systemd-detect-virt']:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                detect_virt = path
                break

    if not detect_virt:
        # Cannot detect, assume physical
        return (False, None)

    try:
        result = subprocess.run(
            [detect_virt], capture_output=True, text=True
        )
        virt_type = result.stdout.strip()
        if result.returncode == 0 and virt_type != 'none':
            return (True, virt_type)
        return (False, None)
    except (subprocess.CalledProcessError, FileNotFoundError,
            OSError):
        return (False, None)

def setup_cron_environment():
    """Set up minimal environment for cron execution"""
    # Ensure basic PATH is available for cron
    if 'PATH' not in os.environ or os.environ['PATH'] == '':
        os.environ['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'

    # Add common sbin directories if not present
    current_path = os.environ.get('PATH', '')
    sbin_paths = ['/usr/sbin', '/sbin', '/usr/local/sbin']
    for sbin_path in sbin_paths:
        if sbin_path not in current_path:
            os.environ['PATH'] = f"{sbin_path}:{os.environ['PATH']}"

def get_current_profile():
    """Get the currently active tuned profile"""
    tuned_adm = find_tuned_adm()
    try:
        result = subprocess.run([tuned_adm, 'active'], 
                              capture_output=True, text=True, check=True)
        # Output format: "Current active profile: <profile>"
        if 'Current active profile:' in result.stdout:
            return result.stdout.split('Current active profile:')[1].strip()
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return "unknown"
    except FileNotFoundError:
        print(f"Error: tuned-adm not found at {tuned_adm}. Is tuned installed?")
        sys.exit(1)

def set_profile(profile, quiet=False):
    """Set the tuned profile, using sudo only if not already root"""
    tuned_adm = find_tuned_adm()

    # Build command - use sudo only if not already root
    if is_root():
        cmd = [tuned_adm, 'profile', profile]
    else:
        cmd = ['sudo', tuned_adm, 'profile', profile]

    try:
        subprocess.run(cmd, check=True)
        if not quiet:
            print(f"Successfully switched to profile: {profile}")
    except subprocess.CalledProcessError as e:
        print(f"Error switching to profile '{profile}': {e}")
        sys.exit(1)
    except FileNotFoundError:
        missing_cmd = 'sudo' if not is_root() else tuned_adm
        print(f"Error: {missing_cmd} not found")
        sys.exit(1)

def main():
    # Set up environment for cron compatibility
    setup_cron_environment()

    parser = argparse.ArgumentParser(
        description='Switch between tuned profiles (cron-compatible)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available profiles:
  p, power, powersave     -> powersave
  v, virtual              -> virtual-host intel-sst
  t, throughput, perf     -> throughput-performance
  l, latency              -> latency-performance
  a, accelerator          -> accelerator-performance

Examples:
  TunedReconfig.py p      Switch to powersave
  TunedReconfig.py v      Switch to virtual-host intel-sst
  TunedReconfig.py t      Switch to throughput-performance
  TunedReconfig.py l      Switch to latency-performance
  TunedReconfig.py a      Switch to accelerator-performance
  TunedReconfig.py -q p   Switch to powersave silently
  TunedReconfig.py -f v   Force profile change on a virtual system
  TunedReconfig.py        Show current profile and toggle options

Note:
  On virtual machines (detected via systemd-detect-virt), profile
  changes are skipped by default. Use -f/--force to override.

Cron usage:
  15 8 * * * /usr/local/sbin/TunedReconfig.py -q v  # Virtual-host at 8:15 AM
  05 23 * * * /usr/local/sbin/TunedReconfig.py -q p # Powersave at 11:05 PM
  30 9 * * 1-5 /usr/local/sbin/TunedReconfig.py -q t # Throughput on weekdays at 9:30 AM
  45 7 * * * /usr/local/sbin/TunedReconfig.py -q l # Latency at 7:45 AM
  00 10 * * * /usr/local/sbin/TunedReconfig.py -q a # Accelerator at 10:00 AM
        """)
    parser.add_argument('profile', nargs='?', 
                       help='Profile to switch to (p/v or full name)')
    parser.add_argument('-s', '--status', action='store_true',
                       help='Show current profile only')
    parser.add_argument('-q', '--quiet', action='store_true',
                       help='Suppress all output except critical errors')
    parser.add_argument('-f', '--force', action='store_true',
                       help='Force profile change even on virtual systems')
    parser.add_argument('--version', action='version', version=__version__,
                       help='Show version information')

    args = parser.parse_args()

    current = get_current_profile()
    vm_detected, virt_type = is_virtual()

    if args.status:
        if not args.quiet:
            print(f"Current profile: {current}")
            if vm_detected:
                print(f"Virtual system detected: {virt_type}")
        return

    if not args.profile:
        if not args.quiet:
            print(f"Current profile: {current}")
            if vm_detected:
                print(f"Virtual system detected: {virt_type}"
                      " (use -f to force profile changes)")
            print("\nAvailable options:")
            print("  p/power      -> powersave")
            print("  v/virtual    -> virtual-host intel-sst")
            print("  t/throughput -> throughput-performance")
            print("  l/latency    -> latency-performance")
            print("  a/accelerator -> accelerator-performance")

            # Auto-suggest alternatives
            if current == 'powersave':
                print(f"\nSuggestions:")
                print(f"  TunedReconfig.py v  (switch to virtual-host intel-sst)")
                print(f"  TunedReconfig.py t  (switch to throughput-performance)")
                print(f"  TunedReconfig.py l  (switch to latency-performance)")
                print(f"  TunedReconfig.py a  (switch to accelerator-performance)")
            elif current == 'virtual-host intel-sst':
                print(f"\nSuggestions:")
                print(f"  TunedReconfig.py p  (switch to powersave)")
                print(f"  TunedReconfig.py t  (switch to throughput-performance)")
                print(f"  TunedReconfig.py l  (switch to latency-performance)")
                print(f"  TunedReconfig.py a  (switch to accelerator-performance)")
            elif current == 'throughput-performance':
                print(f"\nSuggestions:")
                print(f"  TunedReconfig.py p  (switch to powersave)")
                print(f"  TunedReconfig.py v  (switch to virtual-host intel-sst)")
                print(f"  TunedReconfig.py l  (switch to latency-performance)")
                print(f"  TunedReconfig.py a  (switch to accelerator-performance)")
            elif current == 'latency-performance':
                print(f"\nSuggestions:")
                print(f"  TunedReconfig.py p  (switch to powersave)")
                print(f"  TunedReconfig.py v  (switch to virtual-host intel-sst)")
                print(f"  TunedReconfig.py t  (switch to throughput-performance)")
                print(f"  TunedReconfig.py a  (switch to accelerator-performance)")
            elif current == 'accelerator-performance':
                print(f"\nSuggestions:")
                print(f"  TunedReconfig.py p  (switch to powersave)")
                print(f"  TunedReconfig.py v  (switch to virtual-host intel-sst)")
                print(f"  TunedReconfig.py t  (switch to throughput-performance)")
                print(f"  TunedReconfig.py l  (switch to latency-performance)")
        return

    # Look up the profile
    profile_key = args.profile.lower()
    if profile_key not in PROFILES:
        print(f"Unknown profile: {args.profile}")
        print("Valid options: p, power, powersave, v, virtual, t, throughput, perf, l, latency, a, accelerator")
        sys.exit(1)

    target_profile = PROFILES[profile_key]

    # Skip profile changes on virtual systems unless --force
    if vm_detected and not args.force:
        if not args.quiet:
            print(f"Virtual system detected ({virt_type})"
                  " - skipping profile change.")
            print("Use -f/--force to override.")
        return

    if current == target_profile:
        if not args.quiet:
            print(f"Already using profile: {current}")
    else:
        if not args.quiet:
            print(f"Switching from '{current}' to '{target_profile}'")
        set_profile(target_profile, quiet=args.quiet)

if __name__ == '__main__':
    main()
