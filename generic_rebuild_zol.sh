#!/bin/bash
# Rebuild OpenZFS RPMs for this host and copy them into the Krynn repos.
#
#   ./generic_rebuild_zol.sh -r 2.4.4
#   ./generic_rebuild_zol.sh --release 2.3.9 -s
#
# Fedora: rpm-utils + rpm-dkms (no kmod; Fedora has no RHEL kABI/KMP stack).
# EL:     rpm-utils + rpm-kmod + rpm-dkms -> RHEL${major}/kmods/el${major}_${minor}

set -euo pipefail

if (( EUID != 0 )); then
	echo "${0##*/}: must run as root" >&2
	exit 1
fi

ZFS_GIT="/usr/src/zfs"
ZFS_ORIGIN="https://github.com/zfsonlinux/zfs.git"
#ZFS_ORIGIN="https://github.com/tonyhutter/zfs.git"
PATCH=""
#PATCH="/net/imladris/export/home/raistlin/World/Vincent/Code/GIT/zfs/zfs-akmod-2.3.z-DUAL-SPEC.patch"
REPO_ROOT="/net/imladris/export/shared/data/install/RedHat"
MAKE_JOBS="${MAKE_JOBS:-$(nproc)}"
SIGN=0
ZFS_REL=""

usage() {
	echo "Usage: ${0##*/} -r|--release X.Y.Z [-s|--sign]" >&2
	exit 2
}

need_arg() {
	if [[ $# -lt 2 || "$2" == -* ]]; then
		echo "${0##*/}: option $1 requires an argument" >&2
		usage
	fi
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		-s|--sign)
			SIGN=1
			shift
			;;
		-r|--release)
			need_arg "$@"
			ZFS_REL="$2"
			shift 2
			;;
		--release=*)
			ZFS_REL="${1#*=}"
			shift
			;;
		-h|--help)
			usage
			;;
		-*)
			echo "${0##*/}: unknown option: $1" >&2
			usage
			;;
		*)
			echo "${0##*/}: unexpected argument: $1 (use -r|--release)" >&2
			usage
			;;
	esac
done

if [[ -z "${ZFS_REL}" ]]; then
	echo "${0##*/}: --release is required" >&2
	usage
fi

if [[ -x /bin/lsb_release ]]; then
	VERSION_ID="$(/bin/lsb_release -rs)"
	distro="$(lsb_release -is)"
elif [[ -r /etc/os-release ]]; then
	# shellcheck source=/dev/null
	. /etc/os-release
	case "${ID}" in
		fedora) distro="Fedora" ;;
		rhel|centos|rocky|almalinux) distro="RedHatEnterprise" ;;
		*) distro="${ID}" ;;
	esac
else
	echo "Need lsb_release or /etc/os-release" >&2
	exit 1
fi

el_major="${VERSION_ID%%.*}"
el_minor="${VERSION_ID#*.}"

echo "ZFS_REL=${ZFS_REL} tag=zfs-${ZFS_REL} distro=${distro} ${VERSION_ID}"

if [[ ! -d "${ZFS_GIT}/.git" ]]; then
	mkdir -p "$(dirname "${ZFS_GIT}")"
	git clone "${ZFS_ORIGIN}" "${ZFS_GIT}"
fi
test -d "${ZFS_GIT}/.git" || exit 1
cd "${ZFS_GIT}"
if [[ -f Makefile ]]; then
	gmake distclean-generic || true
fi
git reset --hard
git fetch origin
git checkout "zfs-${ZFS_REL}"
git reset --hard "zfs-${ZFS_REL}"

shopt -s nullglob
/bin/rm -fv ./*.{x86_64,noarch,src}.rpm

if [[ -n "${PATCH}" ]]; then
	patch -p1 < "${PATCH}"
fi

if [[ -n "${el_minor}" ]]; then
	sed -i -e "/Release:.*1/s/\$/\.${el_minor}/" META
	if [[ "${distro}" != Fedora ]]; then
		sed -i -e 's@BuildRequires:  %kernel_module_package_buildreqs@BuildRequires: kernel-devel@' \
			rpm/redhat/zfs-kmod.spec.in
	fi
fi

test -f ./autogen.sh || exit 2
./autogen.sh
./configure --with-spec=redhat

make_rc=0
case "${distro}" in
	Fedora) rpm_targets=(rpm-utils rpm-dkms) ;;
	*)      rpm_targets=(rpm-utils rpm-kmod rpm-dkms) ;;
esac
for target in "${rpm_targets[@]}"; do
	if make -j"${MAKE_JOBS}" "${target}"; then
		echo "make ${target}: ok"
	else
		echo "make ${target}: failed (continuing)" >&2
		make_rc=1
	fi
done

built_rpms=(*.x86_64.rpm *.noarch.rpm *.src.rpm)
if (( ${#built_rpms[@]} == 0 )); then
	echo "No RPMs produced" >&2
	exit 1
fi

# Copy RPMs into the Krynn repo tree (only on lasthome.solace.krynn hosts).
krynn_fqdn="$(hostname -f 2>/dev/null || true)"
if [[ "${krynn_fqdn}" == *.lasthome.solace.krynn ]]; then
	if [[ "${SIGN}" -eq 1 ]]; then
		GPG_TTY="${GPG_TTY:-$(tty 2>/dev/null || true)}"
		export GPG_TTY
		if ! rpmsign --addsign "${built_rpms[@]}"; then
			echo "rpmsign failed; unsigned RPMs will still be copied" >&2
			make_rc=1
		fi
	fi

	case "${distro}" in
		Fedora)
			DST_DIR="${REPO_ROOT}/Fedora_${el_minor}/contrib"
			mkdir -p "${DST_DIR}/x86_64" "${DST_DIR}/noarch" "${DST_DIR}/SRPMS"
			/bin/mv -fv *.x86_64.rpm "${DST_DIR}/x86_64/"
			/bin/mv -fv *.noarch.rpm "${DST_DIR}/noarch/"
			/bin/mv -fv *.src.rpm    "${DST_DIR}/SRPMS/"
			;;
		RedHatEnterprise|RedHatEnterpriseServer)
			DST_DIR="${REPO_ROOT}/RHEL${el_major}/kmods/el${el_major}_${el_minor}"
			mkdir -p "${DST_DIR}"
			/bin/mv -fv *.rpm "${DST_DIR}/"
			;;
		*)
			echo "Unsupported distro: ${distro}" >&2
			exit 1
			;;
	esac
else
	echo "Not on lasthome.solace.krynn; RPMs left in ${ZFS_GIT}/"
	ls -1 "${built_rpms[@]}"
fi

gmake clean || true
exit "${make_rc}"
