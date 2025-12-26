#!/usr/bin/env python3
"""
sysctl_manager - Manage and compare sysctl kernel tunables

Features:
- Compare current values against a desired configuration file or profile
- Show values modified from kernel defaults
- Apply a set of sysctls from a configuration file or profile
- Concise, color-coded output
- Built-in editable profiles for common configurations
"""

# $Id: sysctl_manager.py 1.01 2025/12/26 00:00:00 add-profiles Exp $
__version__ = "sysctl_manager.py 1.01 2025/12/26 00:00:00 add-profiles Exp"

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


# =============================================================================
# EDITABLE PROFILES - Customize these to match your environment
# =============================================================================
# Each profile is a dictionary of sysctl key -> value
# Use 'list' command to see available profiles
# Reference with @profile_name (e.g., sysctl_manager.py compare @server)

PROFILES = {
    # Basic server profile - common production settings
    'server': {
        'net.ipv4.ip_forward': '1',
        'net.ipv4.conf.all.forwarding': '1',
        'net.ipv6.conf.all.forwarding': '1',
        'net.ipv4.tcp_syncookies': '1',
        'net.ipv4.conf.default.rp_filter': '2',
        'net.ipv4.conf.all.rp_filter': '2',
        'net.ipv4.conf.default.accept_source_route': '0',
        'net.ipv4.icmp_echo_ignore_broadcasts': '1',
        'kernel.sysrq': '1',
        'kernel.core_uses_pid': '1',
        'fs.file-max': '8388608',
        'fs.aio-max-nr': '1048576',
        'vm.swappiness': '10',
        'vm.dirty_ratio': '40',
        'vm.dirty_background_ratio': '5',
        'net.core.somaxconn': '32768',
        'net.core.netdev_max_backlog': '16384',
        'net.ipv4.tcp_max_syn_backlog': '8192',
    },

    # HVM/Hypervisor profile - for KVM/libvirt hosts
    # Based on /etc/sysctl.d/99-krynn.conf from HVM systems
    # Excludes: interface-specific (bond*, docker*), machine-specific (domainname, hugepages)
    'hvm': {
        # Filesystem
        'fs.aio-max-nr': '1048576',
        'fs.file-max': '8388608',
        'fs.inotify.max_queued_events': '16384',
        'fs.inotify.max_user_instances': '2048',
        'fs.inotify.max_user_watches': '4194302',
        # Kernel
        'kernel.core_uses_pid': '1',
        'kernel.hung_task_timeout_secs': '0',
        'kernel.msgmax': '65536',
        'kernel.msgmnb': '65536',
        'kernel.nmi_watchdog': '0',
        'kernel.numa_balancing': '0',
        'kernel.printk': '2 4 1 7',
        'kernel.pty.max': '32768',
        'kernel.sched_autogroup_enabled': '1',
        'kernel.sem': '250 512000 128 16384',
        'kernel.shmall': '4294967296',
        'kernel.shmmax': '549755813888',
        'kernel.shmmni': '4096',
        'kernel.sysrq': '1',
        # Network - bridge
        'net.bridge.bridge-nf-call-arptables': '1',
        'net.bridge.bridge-nf-call-ip6tables': '1',
        'net.bridge.bridge-nf-call-iptables': '1',
        # Network - core
        'net.core.netdev_budget': '1200',
        'net.core.netdev_max_backlog': '16384',
        'net.core.optmem_max': '134217728',
        'net.core.rmem_max': '134217728',
        'net.core.somaxconn': '32768',
        'net.core.wmem_max': '134217728',
        # Network - IPv4 conf.all
        'net.ipv4.conf.all.arp_accept': '1',
        'net.ipv4.conf.all.arp_announce': '2',
        'net.ipv4.conf.all.arp_ignore': '2',
        'net.ipv4.conf.all.arp_notify': '1',
        'net.ipv4.conf.all.forwarding': '1',
        'net.ipv4.conf.all.log_martians': '0',
        'net.ipv4.conf.all.promote_secondaries': '1',
        'net.ipv4.conf.all.rp_filter': '2',
        # Network - IPv4 conf.default
        'net.ipv4.conf.default.accept_source_route': '0',
        'net.ipv4.conf.default.arp_accept': '1',
        'net.ipv4.conf.default.arp_announce': '2',
        'net.ipv4.conf.default.arp_ignore': '2',
        'net.ipv4.conf.default.arp_notify': '1',
        'net.ipv4.conf.default.log_martians': '0',
        'net.ipv4.conf.default.promote_secondaries': '1',
        'net.ipv4.conf.default.rp_filter': '2',
        # Network - IPv4 bond interfaces
        # bond0/bond2/bond3 = trusted local NICs, bond1 = internet-facing (untrusted)
        'net.ipv4.conf.bond0.accept_redirects': '0',
        'net.ipv4.conf.bond0.send_redirects': '0',
        'net.ipv4.conf.bond1.accept_redirects': '0',
        'net.ipv4.conf.bond1.forwarding': '1',
        'net.ipv4.conf.bond1.log_martians': '1',  # Log suspicious packets from internet
        'net.ipv4.conf.bond1.send_redirects': '0',
        'net.ipv4.conf.bond2.arp_accept': '0',
        'net.ipv4.conf.bond3.arp_accept': '0',
        # Network - IPv4 misc
        'net.ipv4.icmp_echo_ignore_broadcasts': '1',
        'net.ipv4.igmp_max_memberships': '128',
        'net.ipv4.ip_forward': '1',
        'net.ipv4.ip_local_port_range': '9000 64501',
        'net.ipv4.ip_nonlocal_bind': '1',
        # Network - IPv4 neighbor cache
        'net.ipv4.neigh.default.gc_thresh1': '4096',
        'net.ipv4.neigh.default.gc_thresh2': '16384',
        'net.ipv4.neigh.default.gc_thresh3': '32768',
        # Network - TCP
        'net.ipv4.tcp_fin_timeout': '30',
        'net.ipv4.tcp_keepalive_intvl': '60',
        'net.ipv4.tcp_keepalive_probes': '5',
        'net.ipv4.tcp_keepalive_time': '600',
        'net.ipv4.tcp_low_latency': '0',
        'net.ipv4.tcp_max_orphans': '65536',
        'net.ipv4.tcp_max_syn_backlog': '8192',
        'net.ipv4.tcp_max_tw_buckets': '262144',
        'net.ipv4.tcp_no_metrics_save': '0',
        'net.ipv4.tcp_orphan_retries': '0',
        'net.ipv4.tcp_rfc1337': '1',
        'net.ipv4.tcp_slow_start_after_idle': '0',
        'net.ipv4.tcp_syncookies': '1',
        'net.ipv4.tcp_tw_reuse': '1',
        # Network - IPv6
        'net.ipv6.conf.all.forwarding': '1',
        'net.ipv6.conf.bond1.forwarding': '1',
        'net.ipv6.conf.default.forwarding': '1',
        'net.ipv6.conf.lo.disable_ipv6': '0',
        'net.ipv6.ip_nonlocal_bind': '1',
        'net.ipv6.neigh.default.gc_thresh1': '4096',
        'net.ipv6.neigh.default.gc_thresh2': '16384',
        'net.ipv6.neigh.default.gc_thresh3': '32768',
        # Network - netfilter
        'net.netfilter.nf_conntrack_buckets': '262144',
        'net.netfilter.nf_conntrack_max': '2097152',
        'net.netfilter.nf_conntrack_tcp_timeout_established': '3600',
        'net.netfilter.nf_conntrack_tcp_timeout_time_wait': '20',
        # Network - UNIX
        'net.unix.max_dgram_qlen': '512',
        # SunRPC (NFS)
        'sunrpc.tcp_slot_table_entries': '1024',
        'sunrpc.udp_slot_table_entries': '1024',
        # Virtual memory
        'vm.dirty_background_ratio': '5',
        'vm.dirty_expire_centisecs': '500',
        'vm.dirty_ratio': '20',
        'vm.dirty_writeback_centisecs': '100',
        'vm.min_free_kbytes': '4194304',
        'vm.overcommit_memory': '0',
        'vm.overcommit_ratio': '50',
        'vm.swappiness': '0',
        'vm.vfs_cache_pressure': '0',
        # Hugepage tuning for static 2MB hugepages
        'vm.extfrag_threshold': '100',
        'vm.compaction_proactiveness': '0',  # EL9+ only (kernel 5.9+)
    },

    # Minimal network forwarding profile
    'forward': {
        'net.ipv4.ip_forward': '1',
        'net.ipv4.conf.all.forwarding': '1',
        'net.ipv6.conf.all.forwarding': '1',
        'net.ipv6.conf.default.forwarding': '1',
    },

    # Database server profile (Oracle/PostgreSQL)
    'database': {
        'kernel.shmmax': '549755813888',
        'kernel.shmall': '4294967296',
        'kernel.shmmni': '4096',
        'kernel.sem': '250 512000 128 16384',
        'kernel.msgmnb': '65536',
        'kernel.msgmax': '65536',
        'fs.file-max': '8388608',
        'fs.aio-max-nr': '1048576',
        'vm.swappiness': '10',
        'vm.dirty_ratio': '40',
        'vm.dirty_background_ratio': '5',
        'vm.overcommit_memory': '0',
        'net.core.somaxconn': '32768',
        'net.ipv4.ip_local_port_range': '9000 65500',
    },
}

# =============================================================================
# END OF EDITABLE PROFILES
# =============================================================================


# ANSI color codes
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

    @classmethod
    def disable(cls):
        """Disable colors for non-tty output"""
        cls.RED = cls.GREEN = cls.YELLOW = cls.BLUE = cls.CYAN = cls.BOLD = cls.RESET = ''


def is_root():
    """Check if running as root"""
    return os.getuid() == 0


def parse_sysctl_file(filepath):
    """Parse a sysctl configuration file, return dict of key->value"""
    tunables = {}
    try:
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith('#'):
                    continue
                # Parse key = value or key=value
                match = re.match(r'^([^=]+?)\s*=\s*(.+)$', line)
                if match:
                    key = match.group(1).strip()
                    value = match.group(2).strip()
                    tunables[key] = value
    except FileNotFoundError:
        print("{0}Error: File not found: {1}{2}".format(
            Colors.RED, filepath, Colors.RESET), file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print("{0}Error: Permission denied: {1}{2}".format(
            Colors.RED, filepath, Colors.RESET), file=sys.stderr)
        sys.exit(1)
    return tunables


def get_tunables(source):
    """
    Get tunables from a file path or profile name.
    Profile names are prefixed with @ (e.g., @server, @hvm)
    Returns (tunables_dict, source_description)
    """
    if source.startswith('@'):
        profile_name = source[1:]
        if profile_name not in PROFILES:
            print("{0}Error: Unknown profile '{1}'{2}".format(
                Colors.RED, profile_name, Colors.RESET), file=sys.stderr)
            print("Available profiles: {0}".format(', '.join(sorted(PROFILES.keys()))))
            sys.exit(1)
        return PROFILES[profile_name], "profile:{0}".format(profile_name)
    else:
        return parse_sysctl_file(source), source


def get_sysctl_value(key):
    """Get current kernel value for a sysctl key, returns None if not found"""
    try:
        result = subprocess.run(
            ['sysctl', '-n', key],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=5
        )
        if result.returncode == 0:
            # Normalize whitespace (some values have tabs)
            return ' '.join(result.stdout.strip().split())
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def get_default_value(key):
    """
    Try to determine the kernel default value for a sysctl.
    Note: True defaults are only available at boot time before sysctl.d is applied.
    Defaults may vary by kernel version - these are based on RHEL8/9 and Fedora.
    """
    # For some well-known defaults, we can provide them
    # This is a subset - actual defaults vary by kernel version
    known_defaults = {
        # Kernel / scheduler
        'kernel.sysrq': '16',
        'kernel.core_uses_pid': '1',
        'kernel.nmi_watchdog': '1',
        'kernel.sched_autogroup_enabled': '1',
        'kernel.hung_task_timeout_secs': '120',
        'kernel.numa_balancing': '1',
        'kernel.printk': '4 4 1 7',
        # Kernel IPC (SysV shared memory / semaphores)
        'kernel.shmmax': '18446744073692774399',  # ULONG_MAX - 2^24
        'kernel.shmall': '18446744073692774399',
        'kernel.shmmni': '4096',
        'kernel.sem': '32000 1024000000 500 32000',
        'kernel.msgmax': '8192',
        'kernel.msgmnb': '16384',
        # Filesystem
        'fs.file-max': '9223372036854775807',  # Varies by RAM
        'fs.aio-max-nr': '65536',
        'fs.inotify.max_user_watches': '524288',
        'fs.inotify.max_user_instances': '128',
        'fs.inotify.max_queued_events': '16384',
        # Virtual memory
        'vm.swappiness': '60',
        'vm.dirty_ratio': '20',
        'vm.dirty_background_ratio': '10',
        'vm.dirty_expire_centisecs': '3000',
        'vm.dirty_writeback_centisecs': '500',
        'vm.vfs_cache_pressure': '100',
        'vm.overcommit_memory': '0',
        'vm.overcommit_ratio': '50',
        'vm.min_free_kbytes': '67584',  # Varies by RAM
        # Network - IP forwarding
        'net.ipv4.ip_forward': '0',
        'net.ipv4.conf.all.forwarding': '0',
        'net.ipv4.conf.default.forwarding': '0',
        'net.ipv6.conf.all.forwarding': '0',
        'net.ipv6.conf.default.forwarding': '0',
        # Network - TCP
        'net.ipv4.tcp_syncookies': '1',
        'net.ipv4.tcp_tw_reuse': '2',
        'net.ipv4.tcp_max_syn_backlog': '1024',
        'net.ipv4.tcp_max_tw_buckets': '262144',
        'net.ipv4.tcp_max_orphans': '65536',
        'net.ipv4.tcp_orphan_retries': '0',
        'net.ipv4.tcp_rfc1337': '0',
        'net.ipv4.tcp_low_latency': '0',
        'net.ipv4.tcp_slow_start_after_idle': '1',
        'net.ipv4.tcp_no_metrics_save': '0',
        'net.ipv4.tcp_keepalive_time': '7200',
        'net.ipv4.tcp_keepalive_intvl': '75',
        'net.ipv4.tcp_keepalive_probes': '9',
        'net.ipv4.tcp_fin_timeout': '60',
        # Network - IP misc
        'net.ipv4.ip_nonlocal_bind': '0',
        'net.ipv6.ip_nonlocal_bind': '0',
        'net.ipv4.ip_local_port_range': '32768 60999',
        'net.ipv4.icmp_echo_ignore_broadcasts': '1',
        'net.ipv4.igmp_max_memberships': '20',
        # Network - routing / rp_filter
        'net.ipv4.conf.default.rp_filter': '2',
        'net.ipv4.conf.all.rp_filter': '0',
        'net.ipv4.conf.default.accept_source_route': '0',
        'net.ipv4.conf.all.accept_source_route': '0',
        'net.ipv4.conf.default.send_redirects': '1',
        'net.ipv4.conf.all.send_redirects': '1',
        'net.ipv4.conf.default.accept_redirects': '1',
        'net.ipv4.conf.all.accept_redirects': '0',
        'net.ipv4.conf.default.log_martians': '0',
        'net.ipv4.conf.all.log_martians': '0',
        'net.ipv4.conf.default.promote_secondaries': '1',
        'net.ipv4.conf.all.promote_secondaries': '1',
        # Network - ARP
        'net.ipv4.conf.default.arp_ignore': '0',
        'net.ipv4.conf.all.arp_ignore': '0',
        'net.ipv4.conf.default.arp_announce': '0',
        'net.ipv4.conf.all.arp_announce': '0',
        'net.ipv4.conf.default.arp_notify': '1',
        'net.ipv4.conf.all.arp_notify': '1',
        'net.ipv4.conf.default.arp_accept': '0',
        'net.ipv4.conf.all.arp_accept': '0',
        # Network - neighbor (ARP/NDP cache)
        'net.ipv4.neigh.default.gc_thresh1': '128',
        'net.ipv4.neigh.default.gc_thresh2': '512',
        'net.ipv4.neigh.default.gc_thresh3': '1024',
        'net.ipv6.neigh.default.gc_thresh1': '128',
        'net.ipv6.neigh.default.gc_thresh2': '512',
        'net.ipv6.neigh.default.gc_thresh3': '1024',
        # Network - core buffers
        'net.core.somaxconn': '4096',  # Changed from 128 in kernel 5.4
        'net.core.netdev_max_backlog': '1000',
        'net.core.netdev_budget': '300',
        'net.core.optmem_max': '20480',
        'net.core.rmem_max': '212992',
        'net.core.wmem_max': '212992',
        'net.core.rmem_default': '212992',
        'net.core.wmem_default': '212992',
        # Network - netfilter
        'net.netfilter.nf_conntrack_max': '262144',
        'net.netfilter.nf_conntrack_buckets': '65536',
        'net.netfilter.nf_conntrack_tcp_timeout_established': '432000',
        'net.netfilter.nf_conntrack_tcp_timeout_time_wait': '120',
        # Network - bridge
        'net.bridge.bridge-nf-call-iptables': '1',
        'net.bridge.bridge-nf-call-ip6tables': '1',
        'net.bridge.bridge-nf-call-arptables': '1',
        # Network - UNIX sockets
        'net.unix.max_dgram_qlen': '512',
        # SunRPC (NFS)
        'sunrpc.tcp_slot_table_entries': '2',
        'sunrpc.udp_slot_table_entries': '2',
        # Misc kernel
        'kernel.core_pattern': 'core',  # systemd changes this
        'kernel.domainname': '(none)',
        'kernel.pty.max': '4096',
        # IPv6 generic defaults (interface-specific inherit from default)
        'net.ipv6.conf.default.disable_ipv6': '0',
        'net.ipv6.conf.all.disable_ipv6': '0',
        'net.ipv6.conf.lo.disable_ipv6': '0',
        'net.ipv6.conf.default.accept_ra': '1',
        'net.ipv6.conf.all.accept_ra': '1',
        'net.ipv6.conf.default.autoconf': '1',
        'net.ipv6.conf.all.autoconf': '1',
        # VM misc
        'vm.lowmem_reserve_ratio': '256 256 32 0 0',
        # TCP/UDP memory (pages) - these vary by system RAM, values below are typical
        # Format: min pressure max
        'net.ipv4.tcp_mem': '188457 251278 376914',  # Varies by RAM
        'net.ipv4.udp_mem': '188457 251278 376914',  # Varies by RAM
        # TCP buffer sizes (bytes) - Format: min default max
        'net.ipv4.tcp_rmem': '4096 131072 6291456',
        'net.ipv4.tcp_wmem': '4096 16384 4194304',
        'net.ipv4.udp_rmem_min': '4096',
        'net.ipv4.udp_wmem_min': '4096',
    }
    return known_defaults.get(key)


def set_sysctl_value(key, value, dry_run=False):
    """Set a sysctl value, returns (success, message)"""
    if dry_run:
        return True, "Would set {0} = {1}".format(key, value)
    
    try:
        cmd = ['sysctl', '-w', '{0}={1}'.format(key, value)]
        if not is_root():
            cmd = ['sudo'] + cmd
        
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=10
        )
        if result.returncode == 0:
            return True, "Set {0} = {1}".format(key, value)
        else:
            error = result.stderr.strip() or result.stdout.strip()
            return False, "Failed to set {0}: {1}".format(key, error)
    except subprocess.TimeoutExpired:
        return False, "Timeout setting {0}".format(key)
    except FileNotFoundError:
        return False, "sysctl command not found"


def normalize_value(value):
    """Normalize a sysctl value for comparison (handle whitespace variations)"""
    if value is None:
        return None
    return ' '.join(str(value).split())


def compare_tunables(tunables, show_all=False):
    """Compare desired tunables against current kernel values"""
    results = []
    max_key_len = max(len(k) for k in tunables.keys()) if tunables else 40
    
    for key, desired in sorted(tunables.items()):
        current = get_sysctl_value(key)
        desired_norm = normalize_value(desired)
        current_norm = normalize_value(current)
        
        if current is None:
            results.append((key, desired, None, 'missing'))
        elif desired_norm != current_norm:
            results.append((key, desired, current, 'mismatch'))
        elif show_all:
            results.append((key, desired, current, 'match'))
    
    return results, max_key_len


def show_modified_from_defaults(tunables):
    """Show which tunables differ from known kernel defaults"""
    print(f"\n{Colors.BOLD}Tunables Modified from Kernel Defaults:{Colors.RESET}\n")
    
    header = f"{'TUNABLE':<56} {'DESIRED':<24} {'DEFAULT':<24}"
    print(f"{Colors.CYAN}{header}{Colors.RESET}")
    print("-" * 104)
    
    modified_count = 0
    unknown_count = 0
    
    for key, desired in sorted(tunables.items()):
        default = get_default_value(key)
        desired_norm = normalize_value(desired)
        
        if default is None:
            unknown_count += 1
            continue
        
        default_norm = normalize_value(default)
        if desired_norm != default_norm:
            modified_count += 1
            print(f"{Colors.YELLOW}{key:<56}{Colors.RESET} {desired:<24} {Colors.BLUE}{default:<24}{Colors.RESET}")
    
    print()
    print(f"Modified from defaults: {Colors.YELLOW}{modified_count}{Colors.RESET}")
    if unknown_count:
        print(f"Unknown defaults (not checked): {unknown_count}")


def cmd_list(args):
    """List available profiles"""
    print("\n{0}Available Profiles:{1}\n".format(Colors.BOLD, Colors.RESET))
    
    for name in sorted(PROFILES.keys()):
        profile = PROFILES[name]
        print("{0}@{1}{2} ({3} tunables)".format(
            Colors.CYAN, name, Colors.RESET, len(profile)))
        if args.verbose:
            for key in sorted(profile.keys())[:5]:
                print("    {0} = {1}".format(key, profile[key]))
            if len(profile) > 5:
                print("    ... and {0} more".format(len(profile) - 5))
            print()
    
    if not args.verbose:
        print("\nUse -v/--verbose to see tunable details")
    print("\nUsage: {0} compare @profile_name".format(sys.argv[0]))
    return 0


def cmd_compare(args):
    """Compare command: show current vs desired values"""
    tunables, source_desc = get_tunables(args.source)
    
    if not tunables:
        print("No tunables found in source.")
        return 0
    
    results, max_key_len = compare_tunables(tunables, show_all=args.all)
    
    print("\n{0}Comparing: {1}{2}\n".format(Colors.BOLD, source_desc, Colors.RESET))
    
    header = f"{'TUNABLE':<56} {'DESIRED':<24} {'CURRENT':<24}"
    print(f"{Colors.CYAN}{header}{Colors.RESET}")
    print("-" * 104)
    
    mismatches = 0
    missing = 0
    
    for key, desired, current, status in results:
        if status == 'missing':
            missing += 1
            print(f"{Colors.RED}{key:<56}{Colors.RESET} {desired:<24} {'(not found)':<24}")
        elif status == 'mismatch':
            mismatches += 1
            print(f"{Colors.YELLOW}{key:<56}{Colors.RESET} {desired:<24} {Colors.RED}{current:<24}{Colors.RESET}")
        else:  # match
            print(f"{Colors.GREEN}{key:<56}{Colors.RESET} {desired:<24} {current:<24}")
    
    total = len(tunables)
    matches = total - mismatches - missing
    
    print()
    print(f"Total: {total}  |  ", end='')
    print(f"{Colors.GREEN}Match: {matches}{Colors.RESET}  |  ", end='')
    print(f"{Colors.YELLOW}Mismatch: {mismatches}{Colors.RESET}  |  ", end='')
    print(f"{Colors.RED}Missing: {missing}{Colors.RESET}")
    
    return 1 if mismatches > 0 or missing > 0 else 0


def cmd_defaults(args):
    """Show tunables that differ from kernel defaults"""
    tunables, source_desc = get_tunables(args.source)
    
    if not tunables:
        print("No tunables found in source.")
        return 0
    
    show_modified_from_defaults(tunables)
    return 0


def cmd_apply(args):
    """Apply tunables from configuration file or profile"""
    tunables, source_desc = get_tunables(args.source)
    
    if not tunables:
        print("No tunables found in source.")
        return 0
    
    if not args.dry_run and not is_root():
        print("{0}Note: Running as non-root, will use sudo{1}\n".format(
            Colors.YELLOW, Colors.RESET))
    
    action = "Would apply" if args.dry_run else "Applying"
    print("{0}{1} tunables from: {2}{3}\n".format(
        Colors.BOLD, action, source_desc, Colors.RESET))
    
    success_count = 0
    fail_count = 0
    skip_count = 0
    
    for key, desired in sorted(tunables.items()):
        current = get_sysctl_value(key)
        current_norm = normalize_value(current)
        desired_norm = normalize_value(desired)
        
        # Skip if already at desired value (unless --force)
        if current_norm == desired_norm and not args.force:
            skip_count += 1
            if args.verbose:
                print(f"{Colors.GREEN}[SKIP]{Colors.RESET} {key} (already {current})")
            continue
        
        if current is None and not args.force:
            # Tunable doesn't exist
            fail_count += 1
            print(f"{Colors.RED}[MISS]{Colors.RESET} {key} (tunable not found)")
            continue
        
        success, msg = set_sysctl_value(key, desired, dry_run=args.dry_run)
        
        if success:
            success_count += 1
            if args.dry_run:
                print(f"{Colors.BLUE}[DRY]{Colors.RESET}  {key} = {desired}")
            else:
                print(f"{Colors.GREEN}[OK]{Colors.RESET}   {key} = {desired}")
        else:
            fail_count += 1
            print(f"{Colors.RED}[FAIL]{Colors.RESET} {msg}")
    
    print()
    print(f"Applied: {Colors.GREEN}{success_count}{Colors.RESET}  |  ", end='')
    print(f"Skipped: {skip_count}  |  ", end='')
    print(f"Failed: {Colors.RED}{fail_count}{Colors.RESET}")
    
    return 1 if fail_count > 0 else 0


def cmd_status(args):
    """Show current values for tunables in the file or profile"""
    tunables, source_desc = get_tunables(args.source)
    
    if not tunables:
        print("No tunables found in source.")
        return 0
    
    print("\n{0}Current kernel values for: {1}{2}\n".format(
        Colors.BOLD, source_desc, Colors.RESET))
    
    header = "{0:<56} {1:<30}".format('TUNABLE', 'CURRENT VALUE')
    print("{0}{1}{2}".format(Colors.CYAN, header, Colors.RESET))
    print("-" * 86)
    
    for key in sorted(tunables.keys()):
        current = get_sysctl_value(key)
        if current is None:
            print("{0:<56} {1}(not found){2}".format(key, Colors.RED, Colors.RESET))
        else:
            print("{0:<56} {1}".format(key, current))
    
    return 0


def main():
    # Disable colors if not a tty
    if not sys.stdout.isatty():
        Colors.disable()
    
    parser = argparse.ArgumentParser(
        description='Manage and compare sysctl kernel tunables',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  list      List available built-in profiles
  compare   Compare current kernel values against desired configuration
  defaults  Show which tunables differ from kernel defaults
  apply     Apply tunables from configuration file or profile
  status    Show current kernel values for tunables

Sources:
  Use a file path or @profile_name (e.g., @server, @hvm, @database)

Examples:
  %(prog)s list                                       # Show available profiles
  %(prog)s list -v                                    # Show profiles with details
  %(prog)s compare @hvm                               # Compare against HVM profile
  %(prog)s compare /etc/sysctl.d/99-krynn.conf        # Compare against file
  %(prog)s compare -a @server                         # Show all, including matches
  %(prog)s defaults @database                         # Show modified from defaults
  %(prog)s apply @server                              # Apply profile (requires root)
  %(prog)s apply --dry-run /etc/sysctl.d/99-krynn.conf
  %(prog)s status @hvm                                # Show current values
        """)
    
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--no-color', action='store_true', help='Disable colored output')
    
    subparsers = parser.add_subparsers(dest='command', help='Command to run')
    
    # List command
    list_parser = subparsers.add_parser('list', aliases=['l'],
                                        help='List available profiles')
    list_parser.add_argument('-v', '--verbose', action='store_true',
                            help='Show tunable details for each profile')
    list_parser.set_defaults(func=cmd_list)
    
    # Compare command
    compare_parser = subparsers.add_parser('compare', aliases=['cmp', 'c'],
                                           help='Compare current vs desired values')
    compare_parser.add_argument('source', help='File path or @profile_name')
    compare_parser.add_argument('-a', '--all', action='store_true',
                               help='Show all tunables including matches')
    compare_parser.set_defaults(func=cmd_compare)
    
    # Defaults command
    defaults_parser = subparsers.add_parser('defaults', aliases=['def', 'd'],
                                            help='Show tunables modified from defaults')
    defaults_parser.add_argument('source', help='File path or @profile_name')
    defaults_parser.set_defaults(func=cmd_defaults)
    
    # Apply command
    apply_parser = subparsers.add_parser('apply', aliases=['a'],
                                         help='Apply tunables from file or profile')
    apply_parser.add_argument('source', help='File path or @profile_name')
    apply_parser.add_argument('-n', '--dry-run', action='store_true',
                             help='Show what would be done without applying')
    apply_parser.add_argument('-f', '--force', action='store_true',
                             help='Apply even if current value matches desired')
    apply_parser.add_argument('-v', '--verbose', action='store_true',
                             help='Show skipped tunables')
    apply_parser.set_defaults(func=cmd_apply)
    
    # Status command
    status_parser = subparsers.add_parser('status', aliases=['s'],
                                          help='Show current kernel values')
    status_parser.add_argument('source', help='File path or @profile_name')
    status_parser.set_defaults(func=cmd_status)
    
    args = parser.parse_args()
    
    if args.no_color:
        Colors.disable()
    
    if not args.command:
        parser.print_help()
        sys.exit(0)
    
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()

