#!/usr/bin/env python3
# $Id: lsinotify.py,v 1.0 2026/08/19 00:00:00 Exp $

# List inotify instances/watches per process and map them to paths.
# Stdlib only (Python 3.6+): no pip modules. Copy one file onto a node:
#   scp lsinotify core@worker:
#   ssh -t core@worker 'sudo python3 ./lsinotify -p $(pgrep -x kubelet)'
# RHCOS/RHEL if python3 is not in PATH:
#   sudo /usr/libexec/platform-python ./lsinotify -p $(pgrep -x kubelet)
# RHEL 7: python3 or python36 from the extras/SCL python36 package:
#   sudo python36 ./lsinotify -p $(pgrep lsyncd)

import argparse
import ctypes
import ctypes.util
import errno
import os
import pwd
import re
import stat
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

if sys.version_info < (3, 6):
    sys.stderr.write('lsinotify requires Python 3.6+ (stdlib only)\n')
    sys.exit(127)

INOTIFY_FD_NAMES = {
    'anon_inode:inotify',
    'anon_inode:[inotify]',
    'inotify',
}

# Skip these when walking a mount looking for inodes.
VIRTUAL_FS = {
    'proc', 'sysfs', 'cgroup', 'cgroup2', 'devpts', 'devtmpfs',
    'debugfs', 'tracefs', 'securityfs', 'pstore', 'bpf', 'nsfs',
    'autofs', 'rpc_pipefs', 'fusectl', 'configfs', 'hugetlbfs',
    'mqueue', 'binfmt_misc', 'selinuxfs', 'efivarfs',
}

# inotify event bits (linux/inotify.h)
INOTIFY_MASKS = (
    (0x00000001, 'ACCESS'),
    (0x00000002, 'MODIFY'),
    (0x00000004, 'ATTRIB'),
    (0x00000008, 'CLOSE_WRITE'),
    (0x00000010, 'CLOSE_NOWRITE'),
    (0x00000020, 'OPEN'),
    (0x00000040, 'MOVED_FROM'),
    (0x00000080, 'MOVED_TO'),
    (0x00000100, 'CREATE'),
    (0x00000200, 'DELETE'),
    (0x00000400, 'DELETE_SELF'),
    (0x00000800, 'MOVE_SELF'),
    (0x00002000, 'UNMOUNT'),
    (0x00004000, 'Q_OVERFLOW'),
    (0x00008000, 'IGNORED'),
    (0x01000000, 'ONLYDIR'),
    (0x02000000, 'DONT_FOLLOW'),
    (0x04000000, 'EXCL_UNLINK'),
    (0x10000000, 'MASK_CREATE'),
    (0x20000000, 'MASK_ADD'),
    (0x40000000, 'ISDIR'),
    (0x80000000, 'ONESHOT'),
)

WATCH_RE = re.compile(
    r'^inotify wd:([0-9a-fA-F]+) ino:([0-9a-fA-F]+) sdev:([0-9a-fA-F]+) '
    r'mask:([0-9a-fA-F]+)'
    r'(?: ignored_mask:([0-9a-fA-F]+))?'
    r'(?: fhandle-bytes:([0-9a-fA-F]+) fhandle-type:([0-9a-fA-F]+) '
    r'f_handle:([0-9a-fA-F]+))?'
)

# Cheap sdev extract for summary mode (do not build per-watch records).
# wd is hex on current kernels (e.g. wd:d); digits-only \\d+ dropped those.
SDEV_RE = re.compile(
    r'^inotify wd:[0-9a-fA-F]+ ino:[0-9a-fA-F]+ sdev:([0-9a-fA-F]+)'
)

OSTREE_DEPLOY_RE = re.compile(
    r'/ostree/deploy/[^/]+/deploy/[^/]+(?:\.0)?(/.*)$'
)

# Auto-map paths for small inode sets (kubelet). A single filesystem
# with this many watches is treated as a recursive / whole-FS tree
# (lsyncd, IDE file watchers): count it, do not map paths, move on.
AUTO_RESOLVE_MAX = 512
BULK_FS_MIN = 512

MAX_HANDLE_SZ = 128
O_PATH = getattr(os, 'O_PATH', 0o10000000)
O_CLOEXEC = getattr(os, 'O_CLOEXEC', 0o2000000)


class FileHandle(ctypes.Structure):
    _fields_ = [
        ('handle_bytes', ctypes.c_uint),
        ('handle_type', ctypes.c_int),
        ('f_handle', ctypes.c_ubyte * MAX_HANDLE_SZ),
    ]


def decode_mask(mask: int) -> str:
    """Return compact inotify mask names."""
    names = [name for bit, name in INOTIFY_MASKS if mask & bit]
    return '|'.join(names) if names else f'0x{mask:x}'


def decode_sdev(sdev: int) -> Tuple[int, int]:
    """Decode fdinfo sdev (kernel dev_t: major<<20 | minor)."""
    return sdev >> 20, sdev & 0xFFFFF


def detect_wd_base(lines) -> int:
    """Older kernels print wd in decimal (%d); current ones use hex (wd:d)."""
    for line in lines:
        if not line.startswith('inotify '):
            continue
        m = re.match(r'^inotify wd:([0-9a-fA-F]+)', line)
        if m and re.search(r'[a-fA-F]', m.group(1)):
            return 16
    return 10


def unescape_mount(path: str) -> str:
    """Unescape octal sequences from /proc mountinfo paths."""
    return (path.replace('\\040', ' ')
                .replace('\\011', '\t')
                .replace('\\012', '\n')
                .replace('\\134', '\\'))


def file_type_from_mode(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return 'dir'
    if stat.S_ISREG(mode):
        return 'file'
    if stat.S_ISLNK(mode):
        return 'link'
    if stat.S_ISSOCK(mode):
        return 'sock'
    if stat.S_ISFIFO(mode):
        return 'fifo'
    if stat.S_ISCHR(mode):
        return 'chr'
    if stat.S_ISBLK(mode):
        return 'blk'
    return 'other'


def read_sysctl(path: str, default: int = 0) -> int:
    try:
        with open(path, 'r') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return default


class InotifyLister:
    def __init__(self):
        self.debug = False
        self.resolve = False
        self.no_resolve = False
        self.walk = False
        self.search_dirs: List[str] = []
        self.limit = 100
        self.show_events = False
        self.version = '1.0'

        self._libc = None
        self._handles_ok = None  # None=unknown, True/False
        self._mount_fd_cache: Dict[Tuple[int, int, int], Optional[int]] = {}
        self._mountinfo_cache: Dict[int, List[dict]] = {}
        self._mntns_cache: Dict[int, int] = {}
        self._inode_path_cache: Dict[Tuple[int, int, int], str] = {}

    def debug_print(self, message: str):
        if self.debug:
            print(f'# DEBUG: {message}')

    def _libc_so(self):
        if self._libc is None:
            libname = ctypes.util.find_library('c') or 'libc.so.6'
            self._libc = ctypes.CDLL(libname, use_errno=True)
            self._libc.open_by_handle_at.argtypes = [
                ctypes.c_int, ctypes.c_void_p, ctypes.c_int
            ]
            self._libc.open_by_handle_at.restype = ctypes.c_int
        return self._libc

    def get_user(self, uid: int) -> str:
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return str(uid)

    def get_comm(self, pid: int) -> str:
        try:
            with open(f'/proc/{pid}/comm', 'r') as f:
                return f.read().strip()
        except OSError:
            return '?'

    def get_cmdline(self, pid: int, maxlen: int = 80) -> str:
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                raw = f.read().replace(b'\0', b' ').decode('utf-8', 'replace')
            cmd = raw.strip()
            if not cmd:
                return f'[{self.get_comm(pid)}]'
            if maxlen and len(cmd) > maxlen:
                return cmd[:maxlen - 3] + '...'
            return cmd
        except OSError:
            return f'[{self.get_comm(pid)}]'

    def list_pids(self) -> List[int]:
        pids = []
        try:
            for name in os.listdir('/proc'):
                if name.isdigit():
                    pids.append(int(name))
        except OSError:
            pass
        return sorted(pids)

    def parse_pids(self, pid_groups: List[List[str]]) -> List[int]:
        """Parse -p values: 123, '123,456', or $(pgrep ...) words/newlines."""
        tokens: List[str] = []
        for group in pid_groups:
            for item in group:
                tokens.extend(re.split(r'[, \t\n]+', str(item).strip()))

        pids: List[int] = []
        seen: Set[int] = set()
        for tok in tokens:
            if not tok:
                continue
            if not tok.isdigit():
                print(f'Warning: not a PID: {tok}', file=sys.stderr)
                continue
            pid = int(tok)
            if pid in seen:
                continue
            if not os.path.exists(f'/proc/{pid}'):
                print(f'Warning: PID {pid} does not exist', file=sys.stderr)
                continue
            seen.add(pid)
            pids.append(pid)
        return pids

    def match_names(self, names: List[str]) -> List[int]:
        """Match -n/--name against /proc/PID/comm (exact, then substring)."""
        if not names:
            return []
        exact = set(names)
        sub = [n.lower() for n in names]
        found: List[int] = []
        seen: Set[int] = set()
        for pid in self.list_pids():
            comm = self.get_comm(pid)
            if comm in exact or comm.lower() in sub or any(
                    s in comm.lower() for s in sub):
                if pid not in seen:
                    seen.add(pid)
                    found.append(pid)
        return found

    def iter_inotify_fds(self, pid: int) -> List[int]:
        fd_dir = f'/proc/{pid}/fd'
        fds = []
        try:
            for fd_name in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd_name))
                except OSError:
                    continue
                if target in INOTIFY_FD_NAMES:
                    fds.append(int(fd_name))
        except OSError:
            return []
        return sorted(fds)

    def parse_watch_line(self, line: str, wd_base: int = 16) -> Optional[dict]:
        m = WATCH_RE.match(line.strip())
        if not m:
            return None
        wd, ino_h, sdev_h, mask_h, ign_h, fh_bytes, fh_type, fh = m.groups()
        sdev = int(sdev_h, 16)
        major, minor = decode_sdev(sdev)
        watch = {
            'wd': int(wd, wd_base),
            'wd_hex': wd.lower(),
            'ino': int(ino_h, 16),
            'ino_hex': ino_h.lower(),
            'sdev': sdev,
            'sdev_hex': sdev_h.lower(),
            'major': major,
            'minor': minor,
            'mask': int(mask_h, 16),
            'ignored_mask': int(ign_h, 16) if ign_h else 0,
            'fhandle_type': int(fh_type, 16) if fh_type else None,
            'f_handle': fh.lower() if fh else None,
            'path': None,
            'ftype': None,
        }
        return watch

    def parse_fdinfo(self, pid: int, fd: int,
                     skip_devs: Optional[Set[Tuple[int, int]]] = None
                     ) -> List[dict]:
        watches = []
        skip_devs = skip_devs or set()
        path = f'/proc/{pid}/fdinfo/{fd}'
        try:
            with open(path, 'r') as f:
                lines = f.readlines()
        except OSError as e:
            self.debug_print(f'cannot read {path}: {e}')
            return watches

        wd_base = detect_wd_base(lines)
        for line in lines:
            if not line.startswith('inotify '):
                continue
            if skip_devs:
                m = SDEV_RE.match(line)
                if m:
                    major, minor = decode_sdev(int(m.group(1), 16))
                    if (major, minor) in skip_devs:
                        continue
            w = self.parse_watch_line(line, wd_base=wd_base)
            if w:
                w['fd'] = fd
                watches.append(w)
        return watches

    def summarize_fdinfo(self, pid: int, fd: int) -> Tuple[int, dict]:
        """Count watches and sdev hits without storing each watch."""
        counts: Dict[Tuple[int, int], int] = defaultdict(int)
        n = 0
        path = f'/proc/{pid}/fdinfo/{fd}'
        try:
            with open(path, 'r') as f:
                for line in f:
                    m = SDEV_RE.match(line)
                    if not m:
                        continue
                    n += 1
                    major, minor = decode_sdev(int(m.group(1), 16))
                    counts[(major, minor)] += 1
        except OSError as e:
            self.debug_print(f'cannot read {path}: {e}')
        return n, counts

    def mntns_id(self, pid: int) -> int:
        if pid in self._mntns_cache:
            return self._mntns_cache[pid]
        try:
            ns = os.stat(f'/proc/{pid}/ns/mnt').st_ino
        except OSError:
            ns = 0
        self._mntns_cache[pid] = ns
        return ns

    def parse_mountinfo(self, pid: int) -> List[dict]:
        if pid in self._mountinfo_cache:
            return self._mountinfo_cache[pid]
        mounts = []
        try:
            with open(f'/proc/{pid}/mountinfo', 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    try:
                        sep = parts.index('-')
                    except ValueError:
                        continue
                    maj_min = parts[2]
                    try:
                        maj_s, min_s = maj_min.split(':')
                        major, minor = int(maj_s), int(min_s)
                    except ValueError:
                        continue
                    mounts.append({
                        'mnt_id': int(parts[0]),
                        'major': major,
                        'minor': minor,
                        'root': unescape_mount(parts[3]),
                        'mountpoint': unescape_mount(parts[4]),
                        'fstype': parts[sep + 1] if sep + 1 < len(parts) else '',
                        'source': parts[sep + 2] if sep + 2 < len(parts) else '',
                    })
        except OSError:
            pass
        self._mountinfo_cache[pid] = mounts
        return mounts

    def mounts_for_dev(self, pid: int, major: int, minor: int) -> List[dict]:
        matches = [m for m in self.parse_mountinfo(pid)
                   if m['major'] == major and m['minor'] == minor]
        # Prefer the real filesystem root over bind-mounts of subdirs.
        matches.sort(key=lambda m: (0 if m['root'] == '/' else 1,
                                    len(m['mountpoint'])))
        return matches

    def describe_dev(self, pid: int, major: int, minor: int) -> str:
        mounts = self.mounts_for_dev(pid, major, minor)
        if not mounts:
            return f'{major}:{minor}'
        m = mounts[0]
        src = m['source']
        mp = m['mountpoint']
        fs = m['fstype']
        extra = f' {src}' if src and src != 'none' else ''
        return f'{major}:{minor} {mp} ({fs}{extra})'

    def proc_root_path(self, pid: int, path: str) -> str:
        """Map a path from pid's mount ns into ours via /proc/pid/root."""
        if path == '/':
            return f'/proc/{pid}/root'
        if path.startswith('/'):
            return f'/proc/{pid}/root{path}'
        return f'/proc/{pid}/root/{path}'

    def open_mount_fd(self, pid: int, major: int, minor: int) -> Optional[int]:
        ns = self.mntns_id(pid)
        key = (ns, major, minor)
        if key in self._mount_fd_cache:
            return self._mount_fd_cache[key]

        fd = None
        for mnt in self.mounts_for_dev(pid, major, minor):
            if mnt['fstype'] in VIRTUAL_FS:
                continue
            candidates = [
                self.proc_root_path(pid, mnt['mountpoint']),
                mnt['mountpoint'],
            ]
            for path in candidates:
                opened = None
                for flags in (
                    os.O_RDONLY | O_CLOEXEC,
                    os.O_RDONLY | O_CLOEXEC | O_PATH,
                ):
                    try:
                        opened = os.open(path, flags)
                        self.debug_print(
                            f'open mount fd for {major}:{minor} via {path}'
                        )
                        break
                    except OSError as e:
                        self.debug_print(
                            f'open {path} flags={oct(flags)}: {e}'
                        )
                if opened is not None:
                    if fd is not None:
                        os.close(fd)
                    fd = opened
                    break
            if fd is not None:
                break

        self._mount_fd_cache[key] = fd
        return fd

    def resolve_handle(self, pid: int, watch: dict) -> Optional[str]:
        """O(1) inode->path via open_by_handle_at (needs CAP_DAC_READ_SEARCH)."""
        if self._handles_ok is False:
            return None
        if not watch.get('f_handle') or watch.get('fhandle_type') is None:
            return None

        try:
            handle_bytes = bytes.fromhex(watch['f_handle'])
        except ValueError:
            return None
        if len(handle_bytes) > MAX_HANDLE_SZ:
            return None

        mount_fd = self.open_mount_fd(pid, watch['major'], watch['minor'])
        if mount_fd is None:
            return None

        fh = FileHandle()
        fh.handle_bytes = len(handle_bytes)
        fh.handle_type = watch['fhandle_type']
        ctypes.memmove(fh.f_handle, handle_bytes, len(handle_bytes))

        libc = self._libc_so()
        flags = os.O_RDONLY | O_PATH | O_CLOEXEC
        result = libc.open_by_handle_at(mount_fd, ctypes.byref(fh), flags)
        if result < 0:
            err = ctypes.get_errno()
            if err in (errno.EPERM, errno.EACCES):
                self._handles_ok = False
                self.debug_print(
                    'open_by_handle_at denied (need root / '
                    'CAP_DAC_READ_SEARCH)'
                )
            else:
                self.debug_print(
                    f'open_by_handle_at ino={watch["ino_hex"]} '
                    f'failed: {errno.errorcode.get(err, err)}'
                )
            return None

        self._handles_ok = True
        try:
            path = os.readlink(f'/proc/self/fd/{result}')
            try:
                st = os.fstat(result)
                watch['ftype'] = file_type_from_mode(st.st_mode)
            except OSError:
                pass
        except OSError:
            path = None
        finally:
            os.close(result)

        if path:
            prefix = f'/proc/{pid}/root'
            if path.startswith(prefix):
                stripped = path[len(prefix):]
                path = stripped if stripped else '/'
            path = self.prefer_visible_path(pid, path)
        return path

    def prefer_visible_path(self, pid: int, path: str) -> str:
        """Prefer /etc and /var over ostree canonical paths on RHCOS."""
        m = OSTREE_DEPLOY_RE.search(path)
        if m:
            cand = m.group(1)
            try:
                if os.path.exists(self.proc_root_path(pid, cand)):
                    return cand
            except OSError:
                pass
        m = re.search(r'/ostree/deploy/[^/]+(/var/.*)$', path)
        if m:
            cand = m.group(1)
            try:
                if os.path.exists(self.proc_root_path(pid, cand)):
                    return cand
            except OSError:
                pass
        return path

    def display_path(self, pid: int, path: str) -> str:
        """Strip /proc/<pid>/root and prefer the path the process sees."""
        prefix = f'/proc/{pid}/root'
        if path == prefix:
            path = '/'
        elif path.startswith(prefix + '/'):
            path = path[len(prefix):]
        return self.prefer_visible_path(pid, path)

    def cache_key(self, watch: dict) -> Tuple[int, int, int]:
        return (watch['major'], watch['minor'], watch['ino'])

    @staticmethod
    def stat_dev_key(st) -> Tuple[int, int]:
        """major:minor from a stat() result (glibc-decoded st_dev)."""
        return os.major(st.st_dev), os.minor(st.st_dev)

    def resolve_handles(self, pid: int, watches: List[dict]) -> int:
        """Resolve as many watches as possible via file handles."""
        resolved = 0
        for w in watches:
            key = self.cache_key(w)
            if key in self._inode_path_cache:
                w['path'] = self._inode_path_cache[key]
                resolved += 1
                continue
            path = self.resolve_handle(pid, w)
            if path:
                w['path'] = path
                self._inode_path_cache[key] = path
                resolved += 1
        return resolved

    def walk_for_inodes(self, roots: List[str],
                        inode_keys: Set[Tuple[int, int, int]],
                        stay_dev: Optional[int] = None) -> Dict[
                            Tuple[int, int, int], str]:
        """One-pass directory walk looking only for a known inode set."""
        wanted = set(inode_keys)
        found: Dict[Tuple[int, int, int], str] = {}
        inodes_only = {k[2] for k in inode_keys}

        def maybe_hit(path: str, st) -> None:
            if st.st_ino not in inodes_only:
                return
            maj, minr = self.stat_dev_key(st)
            key = (maj, minr, st.st_ino)
            matched = None
            if key in wanted:
                matched = key
            elif stay_dev is not None and st.st_dev == stay_dev:
                # btrfs and similar: mountinfo s_dev != stat().st_dev
                for wkey in wanted:
                    if wkey[2] == st.st_ino:
                        matched = wkey
                        break
            if matched is None:
                return
            found[matched] = path
            wanted.discard(matched)
            self._inode_path_cache[matched] = path
            self.debug_print(f'walk hit ino={st.st_ino:x} {path}')

        def scan(root: str):
            stack = [root]
            while stack and wanted:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        entries = list(it)
                except OSError:
                    continue
                for entry in entries:
                    if not wanted:
                        break
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stay_dev is not None and st.st_dev != stay_dev:
                        continue
                    maybe_hit(entry.path, st)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)

        for root in roots:
            try:
                st = os.lstat(root)
            except OSError:
                continue
            maybe_hit(root, st)
            if os.path.isdir(root):
                scan(root)
            if not wanted:
                break
        return found

    def resolve_by_walk(self, pid: int, watches: List[dict]):
        """Find remaining inodes with one walk per mount or --search dir."""
        unresolved = [w for w in watches if not w.get('path')]
        if not unresolved:
            return

        by_dev: Dict[Tuple[int, int], List[dict]] = defaultdict(list)
        for w in unresolved:
            by_dev[(w['major'], w['minor'])].append(w)

        for (major, minor), group in by_dev.items():
            keys = {self.cache_key(w) for w in group}
            # Reuse anything already cached
            still = set()
            for key in keys:
                if key in self._inode_path_cache:
                    continue
                still.add(key)
            if not still:
                continue

            roots = []
            if self.search_dirs:
                roots = list(self.search_dirs)
            else:
                for mnt in self.mounts_for_dev(pid, major, minor):
                    if mnt['fstype'] in VIRTUAL_FS:
                        self.debug_print(
                            f'skip virtual fs {mnt["fstype"]} '
                            f'{mnt["mountpoint"]}'
                        )
                        continue
                    roots.append(self.proc_root_path(pid, mnt['mountpoint']))
                if not roots:
                    continue

            self.debug_print(
                f'walking {roots} for {len(still)} inodes on {major}:{minor}'
            )
            found = {}
            seen_dev = set()
            for root in roots:
                try:
                    stay_dev = os.stat(root).st_dev
                except OSError:
                    stay_dev = None
                if stay_dev is not None:
                    if stay_dev in seen_dev:
                        continue
                    seen_dev.add(stay_dev)
                hit = self.walk_for_inodes([root], still, stay_dev=stay_dev)
                found.update(hit)
                still -= set(hit)
                if not still:
                    break
            self.debug_print(f'walk found {len(found)} inodes')

        for w in unresolved:
            key = self.cache_key(w)
            if key in self._inode_path_cache:
                w['path'] = self._inode_path_cache[key]
                if not w.get('ftype'):
                    try:
                        st = os.lstat(w['path'])
                        w['ftype'] = file_type_from_mode(st.st_mode)
                    except OSError:
                        pass

    @staticmethod
    def bulk_devs(sdev_counts: dict) -> Set[Tuple[int, int]]:
        """Filesystems that look like a recursive / whole-tree watch."""
        return {dev for dev, n in sdev_counts.items() if n >= BULK_FS_MIN}

    def virtual_devs(self, pid: int,
                     sdev_counts: dict) -> Set[Tuple[int, int]]:
        """Devices whose mountinfo fstype is a virtual FS (cgroup, proc...)."""
        virt = set()
        for maj, minr in sdev_counts:
            for mnt in self.mounts_for_dev(pid, maj, minr):
                if mnt['fstype'] in VIRTUAL_FS:
                    virt.add((maj, minr))
                    break
        return virt

    def skip_devs_for(self, pid: int, sdev_counts: dict) -> Set[Tuple[int, int]]:
        return self.bulk_devs(sdev_counts) | self.virtual_devs(pid, sdev_counts)

    def mappable_watch_count(self, pid: int, sdev_counts: dict) -> int:
        skip = self.skip_devs_for(pid, sdev_counts)
        return sum(n for dev, n in sdev_counts.items() if dev not in skip)

    def scan_process(self, pid: int, collect: bool,
                     skip_devs: Optional[Set[Tuple[int, int]]] = None
                     ) -> Optional[dict]:
        fds = self.iter_inotify_fds(pid)
        if not fds:
            return None

        try:
            uid = os.stat(f'/proc/{pid}').st_uid
        except OSError:
            return None

        watches: List[dict] = []
        watch_count = 0
        sdev_counts: Dict[Tuple[int, int], int] = defaultdict(int)
        fd_stats: List[dict] = []
        skip_devs = skip_devs or set()

        for fd in fds:
            if collect:
                parsed = self.parse_fdinfo(pid, fd, skip_devs=skip_devs)
                for w in parsed:
                    sdev_counts[(w['major'], w['minor'])] += 1
                watches.extend(parsed)
            else:
                n, counts = self.summarize_fdinfo(pid, fd)
                watch_count += n
                for k, v in counts.items():
                    sdev_counts[k] += v
                fd_stats.append({
                    'fd': fd,
                    'watches': n,
                    'sdevs': dict(counts),
                })

        if collect:
            watch_count = sum(sdev_counts.values())

        info = {
            'pid': pid,
            'uid': uid,
            'user': self.get_user(uid),
            'comm': self.get_comm(pid),
            'cmdline': self.get_cmdline(pid),
            'instances': len(fds),
            'fds': fds,
            'watches': watch_count,
            'sdev_counts': dict(sdev_counts),
            'watch_list': watches,
            'fd_stats': fd_stats,
            'bulk_devs': self.bulk_devs(dict(sdev_counts)),
        }
        return info

    def apply_paths(self, info: dict):
        watches = info.get('watch_list') or []
        if not watches or not self.resolve:
            return

        n = self.resolve_handles(info['pid'], watches)
        self.debug_print(
            f'PID {info["pid"]}: resolved {n}/{len(watches)} via handles'
        )

        need_walk = (self.walk or self.search_dirs) and any(
            not w.get('path') for w in watches
        )
        if need_walk:
            self.resolve_by_walk(info['pid'], watches)

    def print_limits(self):
        max_w = read_sysctl('/proc/sys/fs/inotify/max_user_watches')
        max_i = read_sysctl('/proc/sys/fs/inotify/max_user_instances')
        max_q = read_sysctl('/proc/sys/fs/inotify/max_queued_events')
        print('INotify Limits:')
        print(f'  max_user_watches:   {max_w}')
        print(f'  max_user_instances: {max_i}')
        print(f'  max_queued_events:  {max_q}')
        print()
        return max_w, max_i

    def print_summary(self, results: List[dict], max_w: int, max_i: int):
        if not results:
            print('No processes with inotify instances found.')
            return

        results = sorted(results, key=lambda r: (-r['watches'], r['pid']))
        rows = []
        for r in results:
            fs_parts = []
            for (maj, minr), count in sorted(
                    r['sdev_counts'].items(), key=lambda x: -x[1]):
                mounts = self.mounts_for_dev(r['pid'], maj, minr)
                mp = mounts[0]['mountpoint'] if mounts else f'{maj}:{minr}'
                mark = '*' if count >= BULK_FS_MIN else ''
                fs_parts.append(f'{mp}:{count}{mark}')
            fs = ','.join(fs_parts[:3])
            if len(fs_parts) > 3:
                fs += f',+{len(fs_parts) - 3}'
            rows.append({
                'pid': str(r['pid']),
                'user': r['user'],
                'comm': r['comm'],
                'inst': str(r['instances']),
                'watches': str(r['watches']),
                'fs': fs,
                'cmd': r['cmdline'],
            })

        headers = {
            'pid': 'PID', 'user': 'USER', 'comm': 'COMM',
            'inst': 'INST', 'watches': 'WATCHES', 'fs': 'FS:COUNT',
            'cmd': 'COMMAND',
        }
        widths = {k: len(headers[k]) for k in headers}
        for row in rows:
            for k in widths:
                widths[k] = max(widths[k], len(row[k]))

        fmt = (f"{{pid:>{widths['pid']}}}  {{user:<{widths['user']}}}  "
               f"{{comm:<{widths['comm']}}}  {{inst:>{widths['inst']}}}  "
               f"{{watches:>{widths['watches']}}}  {{fs:<{widths['fs']}}}  "
               f"{{cmd}}")
        print(fmt.format(**headers))
        for row in rows:
            print(fmt.format(**row))

        total_w = sum(r['watches'] for r in results)
        total_i = sum(r['instances'] for r in results)
        print()
        wpct = (100.0 * total_w / max_w) if max_w else 0.0
        ipct = (100.0 * total_i / max_i) if max_i else 0.0
        print(f'Total instances: {total_i}  ({ipct:.1f}% of max_user_instances)')
        print(f'Total watches:   {total_w}  ({wpct:.1f}% of max_user_watches)')
        if any(n >= BULK_FS_MIN for r in results
               for n in r['sdev_counts'].values()):
            print(f'* {BULK_FS_MIN}+ watches on one filesystem: '
                  f'recursive / whole-FS tree (path mapping skipped)')

        # Per-UID totals — inotify limits are per real UID
        by_uid: Dict[int, int] = defaultdict(int)
        for r in results:
            by_uid[r['uid']] += r['watches']
        if len(by_uid) > 1:
            print()
            print('Watches per UID (limit is per real user ID):')
            for uid, count in sorted(by_uid.items(), key=lambda x: -x[1]):
                pct = (100.0 * count / max_w) if max_w else 0.0
                print(f'  {self.get_user(uid):<16} {count:8d}  ({pct:.1f}%)')

    def print_details(self, info: dict):
        pid = info['pid']
        print(f"PID {pid}  {info['user']}  {info['comm']}  "
              f"instances={info['instances']}  watches={info['watches']}")
        print(f"  command: {info['cmdline']}")

        print('  filesystems:')
        bulk = info.get('bulk_devs') or self.bulk_devs(info['sdev_counts'])
        virt = self.virtual_devs(pid, info['sdev_counts'])
        skip = bulk | virt
        for (maj, minr), count in sorted(
                info['sdev_counts'].items(), key=lambda x: -x[1]):
            tag = ''
            if (maj, minr) in virt:
                tag = '  [skip: virtual FS]'
            elif (maj, minr) in bulk:
                tag = '  [skip: whole-FS / recursive]'
            print(f'    {count:6d}  {self.describe_dev(pid, maj, minr)}{tag}')

        fd_stats = info.get('fd_stats') or []
        if fd_stats:
            print()
            print('  instances (one inotify fd = one instance; '
                  'watches are listed in fdinfo):')
            print(f"    {'FD':>4}  {'WATCHES':>7}  DEVICE")
            for stt in fd_stats:
                sdevs = stt.get('sdevs') or {}
                if not sdevs:
                    print(f"    {stt['fd']:4d}  {0:7d}  (no watches)")
                    continue
                for (maj, minr), n in sorted(sdevs.items(),
                                             key=lambda x: -x[1]):
                    extra = ''
                    if (maj, minr) in skip:
                        extra = ' [skip]'
                    print(f"    {stt['fd']:4d}  {n:7d}  "
                          f"{self.describe_dev(pid, maj, minr)}{extra}")

        watches = info.get('watch_list') or []

        if not watches:
            return

        unresolved = sum(1 for w in watches if not w.get('path'))
        print()
        shown = 0
        limit = self.limit if self.limit > 0 else len(watches)
        print(f"    {'FD':>4}  {'WD':>5}  {'INO':<12}  {'DEV':<9}  "
              f"{'TYPE':<4}  PATH")
        for w in watches:
            if shown >= limit:
                break
            path = w.get('path') or f"[ino=0x{w['ino_hex']} unresolved]"
            if not path.startswith('[ino='):
                path = self.display_path(pid, path)
            ftype = w.get('ftype') or '?'
            extra = ''
            if self.show_events:
                extra = f"  {decode_mask(w['mask'])}"
            print(f"    {w['fd']:4d}  {w['wd']:5d}  "
                  f"0x{w['ino_hex']:<10}  "
                  f"{w['major']}:{w['minor']:<6}  {ftype:<4}  "
                  f"{path}{extra}")
            shown += 1
        remaining = len(watches) - shown
        if remaining > 0:
            print(f'    ... {remaining} more watches '
                  f'(use --limit 0 to show all)')
        if unresolved:
            how = 'try --walk or --search DIR'
            if self._handles_ok is False:
                how = 'run as root for file-handle lookup, or ' + how
            print(f'    ({unresolved} watches unresolved; {how})')

    def should_resolve(self, info: dict) -> bool:
        if self.no_resolve:
            return False
        # Virtual FS (cgroup2, proc, ...) and whole-FS trees are counted,
        # never path-mapped.
        mappable = self.mappable_watch_count(info['pid'], info['sdev_counts'])
        if mappable == 0:
            return False
        if self.resolve or self.walk or self.search_dirs:
            return True
        return mappable <= AUTO_RESOLVE_MAX

    def run(self, pids: List[int], details: bool) -> int:
        if os.uname().sysname != 'Linux':
            print('Not supported on this OS.', file=sys.stderr)
            return 125

        if not pids:
            pids = self.list_pids()

        results = []
        for pid in pids:
            info = self.scan_process(pid, collect=False)
            if not info:
                continue
            bulk = self.bulk_devs(info['sdev_counts'])
            virt = self.virtual_devs(pid, info['sdev_counts'])
            skip = bulk | virt
            info['bulk_devs'] = bulk
            if details and self.should_resolve(info):
                full = self.scan_process(pid, collect=True, skip_devs=skip)
                if full:
                    saved = self.resolve
                    self.resolve = True
                    self.apply_paths(full)
                    self.resolve = saved
                    full['sdev_counts'] = info['sdev_counts']
                    full['watches'] = info['watches']
                    full['bulk_devs'] = bulk
                    full['fd_stats'] = info.get('fd_stats') or []
                    info = full
            results.append(info)

        max_w, max_i = self.print_limits()

        if not details:
            self.print_summary(results, max_w, max_i)
            return 0

        if not results:
            print('Matched processes have no inotify instances.')
            return 0

        if len(results) > 1:
            self.print_summary(results, max_w, max_i)
            print()

        for i, info in enumerate(results):
            if i:
                print()
            self.print_details(info)
        return 0


def main():
    parser = argparse.ArgumentParser(
        prog='lsinotify',
        description='List inotify watches per process and map them to paths',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Default (no -p) is a cheap summary: instances, watch counts, and which
filesystems those watches sit on. Inodes come from /proc/PID/fdinfo.

A filesystem with 512+ watches is treated as a recursive / whole-FS
tree (lsyncd, large IDE watchers). Those are counted and skipped for
path mapping; remaining small watches on other filesystems are mapped.

Path mapping (small inode sets only):
  1. open_by_handle_at() using the fdinfo file handle (needs root)
  2. optional: --search DIR (like find DIR -inum ...)
  3. --walk is a last-resort mount walk for leftover small sets

Examples:
  %(prog)s                           # all processes, counts only
  %(prog)s -p $(pgrep -x kubelet)
  %(prog)s -p $(pgrep lsyncd)        # one or more PIDs from pgrep
  %(prog)s -p 1311049,4153072        # comma-separated PIDs
  %(prog)s -p 1311049 -p 4153072     # repeated -p
  %(prog)s -p $(pgrep -x kubelet) --search /etc/kubernetes

Copy to an OpenShift node (stdlib only; sudo as core -> root):
  scp lsinotify core@<node>:
  ssh -t core@<node> 'sudo python3 ./lsinotify'
  ssh -t core@<node> 'sudo python3 ./lsinotify -p $(pgrep -x kubelet)'
  # RHCOS / RHEL 8+ without python3 in PATH:
  sudo /usr/libexec/platform-python ./lsinotify -p $(pgrep -x kubelet)
  # RHEL 7 (python3 or python36 from extras / SCL):
  sudo python3 ./lsinotify
  sudo python36 ./lsinotify -p $(pgrep lsyncd)
        """
    )

    parser.add_argument('-p', '--pid', action='append', nargs='+',
                        metavar='PID', default=[],
                        help='PID(s): -p PID, -p PID1,PID2, or '
                             '-p $(pgrep lsyncd)')
    parser.add_argument('-n', '--name', action='append', default=[],
                        metavar='NAME',
                        help='Process name (comm); repeatable')
    parser.add_argument('--resolve', '-r', action='store_true',
                        help='Map remaining small inode sets to paths')
    parser.add_argument('--no-resolve', action='store_true',
                        help='Never map inodes to paths')
    parser.add_argument('--walk', action='store_true',
                        help='If handles fail, walk non-bulk filesystems '
                             'once looking only for those inodes')
    parser.add_argument('--search', action='append', default=[],
                        metavar='DIR',
                        help='Walk only this directory for unresolved inodes '
                             '(may be repeated; like find DIR -inum ...)')
    parser.add_argument('--limit', type=int, default=100, metavar='N',
                        help='Max watch paths to print per process '
                             '(0 = unlimited, default: 100)')
    parser.add_argument('--events', '-e', action='store_true',
                        help='Decode inotify event mask names')
    parser.add_argument('--debug', action='store_true',
                        help='Debug output prefixed with #')

    args = parser.parse_args()

    lister = InotifyLister()
    lister.debug = args.debug
    lister.resolve = args.resolve or bool(args.search) or args.walk
    lister.no_resolve = args.no_resolve
    lister.walk = args.walk
    lister.search_dirs = args.search
    lister.limit = args.limit
    lister.show_events = args.events

    pids = lister.parse_pids(args.pid)
    if args.name:
        for pid in lister.match_names(args.name):
            if pid not in pids:
                pids.append(pid)

    had_filter = bool(args.pid or args.name)
    if had_filter and not pids:
        print('No matching processes found.', file=sys.stderr)
        return 1

    details = had_filter
    if args.resolve or args.walk or args.search:
        details = True

    try:
        return lister.run(pids, details=details)
    except KeyboardInterrupt:
        print('\nInterrupted by user')
        return 130
    except Exception as e:
        if lister.debug:
            raise
        print(f'Error: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
