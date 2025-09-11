#!/usr/bin/env python3
"""
TunedReconfig - Simple script to switch between tuned profiles

A utility to quickly switch between common tuned profiles:
- powersave
- virtual-host intel-sst
"""

# $Id: TunedReconfig.py 1.02 2025/09/11 00:00:00 add-throughput-profile Exp $
__version__ = "TunedReconfig.py 1.02 2025/09/11 00:00:00 add-throughput-profile Exp"

#
# VERSION HISTORY:
# ================
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
    'intel': 'virtual-host intel-sst',
    't': 'throughput-performance',
    'throughput': 'throughput-performance',
    'throughput-performance': 'throughput-performance',
    'perf': 'throughput-performance'
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
  v, virtual, intel       -> virtual-host intel-sst
  t, throughput, perf     -> throughput-performance

Examples:
  TunedReconfig.py p      Switch to powersave
  TunedReconfig.py v      Switch to virtual-host intel-sst
  TunedReconfig.py t      Switch to throughput-performance
  TunedReconfig.py -q p   Switch to powersave silently
  TunedReconfig.py        Show current profile and toggle options

Cron usage:
  15 8 * * * /usr/local/sbin/TunedReconfig.py -q v  # Virtual-host at 8:15 AM
  05 23 * * * /usr/local/sbin/TunedReconfig.py -q p # Powersave at 11:05 PM
  30 9 * * 1-5 /usr/local/sbin/TunedReconfig.py -q t # Throughput on weekdays at 9:30 AM
        """)
    parser.add_argument('profile', nargs='?', 
                       help='Profile to switch to (p/v or full name)')
    parser.add_argument('-s', '--status', action='store_true',
                       help='Show current profile only')
    parser.add_argument('-q', '--quiet', action='store_true',
                       help='Suppress all output except critical errors')
    parser.add_argument('--version', action='version', version=__version__,
                       help='Show version information')

    args = parser.parse_args()

    current = get_current_profile()

    if args.status:
        if not args.quiet:
            print(f"Current profile: {current}")
        return

    if not args.profile:
        if not args.quiet:
            print(f"Current profile: {current}")
            print("\nAvailable options:")
            print("  p/power     -> powersave")
            print("  v/virtual   -> virtual-host intel-sst")
            print("  t/throughput -> throughput-performance")

            # Auto-suggest alternatives
            if current == 'powersave':
                print(f"\nSuggestions:")
                print(f"  TunedReconfig.py v  (switch to virtual-host intel-sst)")
                print(f"  TunedReconfig.py t  (switch to throughput-performance)")
            elif current == 'virtual-host intel-sst':
                print(f"\nSuggestions:")
                print(f"  TunedReconfig.py p  (switch to powersave)")
                print(f"  TunedReconfig.py t  (switch to throughput-performance)")
            elif current == 'throughput-performance':
                print(f"\nSuggestions:")
                print(f"  TunedReconfig.py p  (switch to powersave)")
                print(f"  TunedReconfig.py v  (switch to virtual-host intel-sst)")
        return

    # Look up the profile
    profile_key = args.profile.lower()
    if profile_key not in PROFILES:
        print(f"Unknown profile: {args.profile}")
        print("Valid options: p, power, powersave, v, virtual, intel, t, throughput, perf")
        sys.exit(1)

    target_profile = PROFILES[profile_key]

    if current == target_profile:
        if not args.quiet:
            print(f"Already using profile: {current}")
    else:
        if not args.quiet:
            print(f"Switching from '{current}' to '{target_profile}'")
        set_profile(target_profile, quiet=args.quiet)

if __name__ == '__main__':
    main()
