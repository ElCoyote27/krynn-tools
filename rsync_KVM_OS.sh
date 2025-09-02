#!/bin/bash
# $Id: rsync_KVM_OS.sh,v 1.175 2025/08/26 08:16:00 root Exp root $

#
[ "$BASH" ] && function whence
{
	type -p "$@"
}
#
PATH_SCRIPT="$(cd $(/usr/bin/dirname $(whence -- $0 || echo $0));pwd)"

#
# Options:
# -c|--checksum	: force checksumming
# -f|--force	: overwrite even if files are more recent on destination..
# -h|--help	: Show help
# -p|--poweroff	: when sync is done, poweroff remote system
# -t|--test	: don't copy, only perform a check test
# -u|--update	: only update if newer files
# -s|--novxsnap	: Don't use vxfs snapshots even if supported
#

# Defaults
WAIT4=2.5s
FORCE_CHECKSUM=0
FORCE_ACTION=0
POWEROFF=0
TEST_ONLY=0
UPDATE_ONLY=0
SKIP_DEFINE=0
DEBUG=0
NR_THREADS=1
VXFS_SNAPSHOTS=1
VXSNAP_PREFIX=/run/user/$(id -u)
VXSNAP_OPTS="cachesize=1536g/autogrow=yes"

# Global defaults
KVM_CONF_SRC_DIR="/etc/libvirt/qemu"
KVM_CONF_DST_DIR="/etc/libvirt/qemu"
KVM_IMAGES_SRC_DIRS=( /shared/kvm0/images )
KVM_IMAGES_DST_DIRS=( /shared/kvm0/images )
KVM_NVRAM_SRC_DIRS=( /shared/kvm0/nvram )
KVM_NVRAM_DST_DIRS=( /shared/kvm0/nvram )

# Rsync Options
####export RSYNC_RSH='ssh -c arcfour -oCompression=no'
####export RSYNC_RSH='ssh -c aes128-ctr -oCompression=no'
export RSYNC_RSH='ssh -c aes128-gcm@openssh.com -oCompression=no'

# Common options
####### COMMON_RSYNC_OPTIONS="-avS --progress --delete"
####### COMMON_RSYNC_OPTIONS="-av --progress --delete"
####### COMMON_RSYNC_OPTIONS="-av --progress --delete --whole-file"
COMMON_RSYNC_OPTIONS="-a --info=name,progress1 --delete --whole-file --skip-compress=qcow2"
BASE_RSYNC_OPTIONS="${COMMON_RSYNC_OPTIONS}"
RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS}"
DEFAULT_KVM_TEMPLATES="/var/lib/libvirt/templates"

# Default Payload
DEFAULT_VM_LIST=""
DEFAULT_VM_LIST="${DEFAULT_VM_LIST} dc00 dc01 dc02 dc03 fedora-x64 fedora-csb-x64 win10-x64 win11-x64 unifi gitlab"
DEFAULT_VM_LIST="${DEFAULT_VM_LIST} bdc416x bdc417x bdc418x bdc419x bdc420x bdc421x bdc422x bdc423x"
DEFAULT_VM_LIST="${DEFAULT_VM_LIST} sat6 ca8 idm00 registry quay vxvom www8 kali-x64 freenas-11 ubuntu-x64 dsm7 sno4"
DEFAULT_VM_LIST="${DEFAULT_VM_LIST} rhel3-x86 rhel4-x86 rhel5-x86 rhel5-x64 rhel6-x86 rhel6-x64 rhel7-x64 rhel8-x64 rhel8-x64-eus rhel9-x64"
DEFAULT_VM_LIST="${DEFAULT_VM_LIST} coreos-sno-0 coreos-sno-1 coreos-sno-2 coreos-sno-3 cirros"

# getopt
TEMP=$(getopt -o 'cdfhpstu' --long 'checksum,debug,force,help,poweroff,novxsnap,test,update' -n 'rsync_KVM_OS.sh' -- "$@")

if [[ $? != 0 ]]; then echo "Failed parsing options." >&2 ; exit 1 ; fi

# Note the quotes around "$TEMP": they are essential!
eval set -- "$TEMP"
unset TEMP

# Find remote host from basename
REMOTE_HOST="$(/bin/basename $(whence -- $0 || echo $0)|sed -e 's/^rsync_KVM_//' -e 's/_OS\.sh$//' -e 's/.sh$//')"
[ "root" != "$USER" ] && exec sudo $0 "$@"

# Sanity Check
if [[ -z ${REMOTE_HOST} ]]; then
	echo "Unable to guess Remote host from $0! Exit!"
	exit 127
else
	/usr/bin/getent hosts ${REMOTE_HOST} > /dev/null
	if [[ $? -ne 0 ]]; then
		echo "(EE) Unable to resolve host \"$REMOTE_HOST\""
		exit 127
	fi

	if [[ "${REMOTE_HOST}" = "$(/bin/uname -n)" ]]; then
		echo "(EE) Don't run this on ${REMOTE_HOST} to push files to ${REMOTE_HOST}!!!"
		exit 127
	fi
fi

case ${REMOTE_HOST} in
	'daltigoth')
		REMOTE_HOST="daltigoth-228"
		NR_THREADS=2
		;;
	'palanthas')
		REMOTE_HOST="palanthas-228"
		NR_THREADS=2
		;;
	'ravenvale')
		REMOTE_HOST="ravenvale-228"
		NR_THREADS=1
		;;
	'solinari')
		NR_THREADS=1
		;;
	'kalaman'|'kalaman-224'|'kalaman-228')
		NR_THREADS=2
		;;
esac
echo "(II) Remote destination: ${REMOTE_HOST}"

# Special cases (The destinations..)
case ${REMOTE_HOST} in
	'kalaman'|'kalaman-224'|'kalaman-228'|'ligett')
		KVM_IMAGES_DST_DIRS=( /volume1/kvm0/images )
		KVM_NVRAM_DST_DIRS=( /volume1/kvm0/nvram )
		BASE_RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS} --rsync-path=/opt/bin/rsync"
		# Needed because of absence of /usr/bin/stat on DSM
		SKIP_DEFINE=1
		;;
	'solinari')
		DEFAULT_VM_LIST="rhel3-x86 win10-x64 win11-x64 bdc420x dc00 dc01 idm00 fedora-x64 fedora-csb-x64 cirros ca8"
		;;
	'solanthus')
		DEFAULT_VM_LIST="rhel3-x86 rhel9-x64 ca8 fedora-x64 fedora-csb-x64 win10-x64 win11-x64 dc00 dc01 bdc420x idm00 cirros"
		;;
	'lothlorien')
		DEFAULT_VM_LIST="fedora-x64 cirros"
		;;
	'ravenvale')
		VXFS_SNAPSHOTS=0
		;;
	'rh8x64'|'rh9x64')
		DEFAULT_VM_LIST="win11-x64 cirros"
		;;
	*)
		;;
esac

RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS}"

# Process args
while true; do
	case "$1" in
		'-h'|'--help')
			# Usage reminder:
			echo "(II) Usage: $0 [-c|--checksum] [-f|--force] [-h|--help] [-p|--poweroff] [-s|--novxsnap] [-t|--test] [-u|--update] [VM_LIST]"
			exit 1
		;;
		'-c'|'--checksum')
			RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS} -c"
			FORCE_CHECKSUM=1
			shift
			continue
		;;
		'-d'|'--debug')
			DEBUG=1
			shift
			continue
		;;
		'-f'|'--force')
			RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS}"
			FORCE_ACTION=1
			shift
			continue
		;;
		'-p'|'--poweroff')
			RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS}"
			POWEROFF=1
			shift
			continue
		;;
		'-s'|'--novxsnap')
			RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS}"
			VXFS_SNAPSHOTS=0
			shift
			continue
		;;
		'-t'|'--test')
			RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS} -n"
			TEST_ONLY=1
			shift
			continue
		;;
		'-u'|'--update')
			RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS} -u"
			UPDATE_ONLY=1
			shift
			continue
		;;
		'--')
			shift
			break
		;;
		*)
			RSYNC_OPTIONS="${BASE_RSYNC_OPTIONS}"
			break
		;;
	esac
done


# Sanity Check. Don't sync if not mounted.
for DST_DIR in ${KVM_IMAGES_DST_DIRS[@]:0}
do
	case ${REMOTE_HOST} in
	kalaman|kalaman-224|wayreth|ligett)
		skip=1
		;;
	*)
		check_dir="$(/usr/bin/dirname ${DST_DIR})"
		export check_dir
		rem_dir="$(/usr/bin/ssh -q ${REMOTE_HOST} df -hP ${DST_DIR}|sed 1d|awk '{ print $6}')"
		if [[ -z ${rem_dir} || "${rem_dir}" = "/" ]]; then
			echo "(EE) Directory ${DST_DIR} does not have a matching remote mount point! Exit!"
			exit 127
		else
			echo "(II) Found remote mount point for ${DST_DIR} : ${REMOTE_HOST}:${rem_dir}"
		fi
		;;
	esac

done

# Next args
if [[ "$#" -eq 0 ]]; then
		VM_LIST=${DEFAULT_VM_LIST}
else
	tmp_str="$*"
	VM_LIST=""
	# Process $1
	for SRC_DIR in ${KVM_IMAGES_SRC_DIRS[@]:0}
	do
		if [[ -d ${KVM_CONF_SRC_DIR} ]]; then
			cd ${KVM_CONF_SRC_DIR} || exit 1
			for myvm in $(echo ${tmp_str}|sed -e 's@\.xml@@g'|xargs)
			do
				xmlfile="$( eval ls -d ${myvm}.xml 2> /dev/null|sed -e 's@\.xml@@g'|xargs)"
				if [[ ! -z ${xmlfile} ]]; then
					echo "(II) Found Domain: ${myvm} (${KVM_CONF_SRC_DIR}/${myvm}.xml"
					VM_LIST+="${myvm}"
					VM_LIST+=" "
				fi
			done
			VM_LIST=$(echo ${VM_LIST}|xargs -n1|sort -u|xargs)
		fi
	done
#	VM_LIST=$*
fi
echo -e "(II) Default VM List: ${DEFAULT_VM_LIST}"
echo -e "(II) Temp. VM List: ${VM_LIST}"

# Actual process
i=0
for SRC_DIR in ${KVM_IMAGES_SRC_DIRS[@]:0}
do
	if [[ -d ${SRC_DIR} ]]; then
		# Empty vmdsk file List
		OS_LIST=""
		DISK_LIST=""
		NVRAM_LIST=""

		# Build the vmdk/vmx file list
		for vmos in ${VM_LIST}
		do
			if [[ -d ${KVM_CONF_SRC_DIR} ]]; then
				cd ${KVM_CONF_SRC_DIR} || exit 1
				# This is the place where we compare the first qcow2 file times
				# to make sure we're not overwriting something newer than what we have
				domain_file="$(/bin/ls ${vmos}.xml 2>/dev/null|sed -e 's@\.xml@@g')"
				long_file="${KVM_CONF_SRC_DIR}/${domain_file}.xml"
				if [[ -f "${long_file}" ]]; then
					# QCOW2 files
					for mydisk in $(grep '<source.file=' ${long_file} |sed -e 's@^ *<source file=@@' -e 's@/>@@g'|xargs)
					do
						skip=0
						src_disk_file="${mydisk}"
						dst_disk_file="${KVM_IMAGES_DST_DIRS[$i]}/$(basename ${src_disk_file})"

						# Allow -f to override, else do the checks
						if [[ ${FORCE_ACTION} -eq 0 ]]; then
							# Verify if domain is active locally or not
							skip1=0
							domstate=$(PATH=/bin:/opt/bin virsh domstate ${vmos} 2>/dev/null)
							case ${domstate} in
								'running')
									echo "(**) Domain ${vmos} is running!! Skipping..."
									skip1=1
									;;
								'shut off')
									skip1=0
									;;
								*)
									#echo "(**) Domain ${vmos} neither up nor shut off, Syncing anyway..."
									skip1=0
									;;
							esac
							# Verify if domain is active remotely or not, force skip if active or other error..
							skip2=0
							domstate=$(ssh -q ${REMOTE_HOST} PATH=/bin:/opt/bin virsh domstate ${vmos} 2>/dev/null)
							case ${domstate} in
								'running')
									echo "(**) Domain ${vmos} is running on ${REMOTE_HOST}!! Skipping..."
									skip2=1
									;;
								'shut off')
									noop2=1
									;;
								*)
									#echo "(**) Domain ${vmos} neither up nor shut off on ${REMOTE_HOST}, Syncing anyway..."
									skip2=0
									;;
							esac
							skip=$((${skip1} + ${skip2}))
						fi
						if [[ ${skip} -eq 0 ]]; then
							# echo "(II) Found Disk File: ${domain_file} ( ${src_disk_file} )"
							if [[ -f "${long_file}" ]]; then
								if [[ ${FORCE_ACTION} -eq 1 ]]; then
									OS_LIST="$(echo ${OS_LIST} ${vmos}|xargs -n1|sort -u|xargs)"
									DISK_LIST="${DISK_LIST} ${src_disk_file}"
								else
									sfile=$(/usr/bin/stat -L -c %Y ${src_disk_file})
									rfile=$(ssh -q ${REMOTE_HOST} /usr/bin/stat -L -c %Y ${dst_disk_file} 2>/dev/null)
									# If Remote file is not present, assume epoch and allow syncing
									if [[ -z ${rfile} ]]; then
										rfile=0
									fi
									if [[ ! -z ${sfile} && ! -z ${rfile} ]]; then
										if [[ ${sfile} -gt ${rfile} ]]; then
											echo "(II) *** Will rsync (${vmos}) ${src_disk_file} to ${REMOTE_HOST}:${KVM_IMAGES_DST_DIRS[$i]}"
											OS_LIST="$(echo ${OS_LIST} ${vmos}|xargs -n1|sort -u|xargs)"
											DISK_LIST="${DISK_LIST} ${src_disk_file}"
										else
											if [[ ${sfile} -eq ${rfile} ]]; then
												echo "(II) stat() times on ${domain_file} ( ${src_disk_file} ) are identical, skipping..."
											fi
										fi
									fi
								fi
							fi
						else
							# skip is not zero, do not bother checking the other qcow2s
							break
						fi
					done

					# NVRAM files
					for mynvram in $(grep '<nvram' ${long_file} |sed -e 's@^ *<nvram.*>/@/@' -e 's@</nvram>@@g'|xargs)
					do
						skip=0
						src_nvram_file="${mynvram}"
						dst_nvram_file="${KVM_NVRAM_DST_DIRS[$i]}/$(basename ${src_nvram_file})"

						# Allow -f to override, else do the checks
						if [[ ${FORCE_ACTION} -eq 0 ]]; then
							# Verify if domain is active locally or not
							skip1=0
							domstate=$(PATH=/bin:/opt/bin virsh domstate ${vmos} 2>/dev/null)
							case ${domstate} in
								'running')
									echo "(**) Domain ${vmos} is running!! Skipping..."
									skip1=1
									;;
								'shut off')
									skip1=0
									;;
								*)
									#echo "(**) Domain ${vmos} neither up nor shut off, Syncing anyway..."
									skip1=0
									;;
							esac
							# Verify if domain is active remotely or not, force skip if active or other error..
							skip2=0
							domstate=$(ssh -q ${REMOTE_HOST} PATH=/bin:/opt/bin virsh domstate ${vmos} 2>/dev/null)
							case ${domstate} in
								'running')
									echo "(**) Domain ${vmos} is running on ${REMOTE_HOST}!! Skipping..."
									skip2=1
									;;
								'shut off')
									noop2=1
									;;
								*)
									#echo "(**) Domain ${vmos} neither up nor shut off on ${REMOTE_HOST}, Syncing anyway..."
									skip2=0
									;;
							esac
							skip=$((${skip1} + ${skip2}))
						fi
						if [[ ${skip} -eq 0 ]]; then
							# echo "(II) Found nvram File: ${domain_file} ( ${src_nvram_file} )"
							if [[ -f "${long_file}" ]]; then
								if [[ ${FORCE_ACTION} -eq 1 ]]; then
									OS_LIST="$(echo ${OS_LIST} ${vmos}|xargs -n1|sort -u|xargs)"
									NVRAM_LIST="${NVRAM_LIST} ${src_nvram_file}"
								else
									sfile=$(/usr/bin/stat -L -c %Y ${src_nvram_file})
									rfile=$(ssh -q ${REMOTE_HOST} /usr/bin/stat -L -c %Y ${dst_nvram_file} 2>/dev/null)
									# If Remote file is not present, assume epoch and allow syncing
									if [[ -z ${rfile} ]]; then
										rfile=0
									fi
									if [[ ! -z ${sfile} && ! -z ${rfile} ]]; then
										if [[ ${sfile} -gt ${rfile} ]]; then
											echo "(II) *** Will rsync (${vmos}) ${src_nvram_file} to ${REMOTE_HOST}:${KVM_NVRAM_DST_DIRS[$i]}"
											OS_LIST="$(echo ${OS_LIST} ${vmos}|xargs -n1|sort -u|xargs)"
											NVRAM_LIST="${NVRAM_LIST} ${src_nvram_file}"
										else
											if [[ ${sfile} -eq ${rfile} ]]; then
												echo "(II) stat() times on ${domain_file} ( ${src_nvram_file} ) are identical, skipping..."
											fi
										fi
									fi
								fi
							fi
						else
							# skip is not zero, do not bother checking the other NVRAM files
							break
						fi
					done
				fi
			fi
		done

		OS_LIST=$(echo $OS_LIST|xargs)
		echo -e "(II) Final VM List: ${OS_LIST}"
		if [[ ! -z ${DISK_LIST} ]]; then
			echo -e "(II) Final Disk List: ${DISK_LIST}"
		fi
		if [[ ! -z ${NVRAM_LIST} ]]; then
			echo -e "(II) Final NVRAM List: ${NVRAM_LIST}"
		fi

		# Do the actual rsync (Guest XMLs)
		if [[ ! -z ${OS_LIST} ]]; then
			echo -e "(II) Waiting ${WAIT4} seconds before push to ${REMOTE_HOST} ..."
			sleep ${WAIT4}
			cd ${SRC_DIR} || exit 127
			#find ${OS_LIST} -type f -name "core.*" -o -name "vmcores*gz" |xargs -r /bin/rm -fv
			for myvm in ${OS_LIST}
			do
				# if there is a templated XML for that VM, use that instead of the local copy
				if [[ -f ${DEFAULT_KVM_TEMPLATES}/${myvm}.xml ]]; then
					XML_SRC="${DEFAULT_KVM_TEMPLATES}/${myvm}.xml"
					##### echo "(II) Will create ${REMOTE_HOST}:${KVM_CONF_DST_DIR}/${myvm}.xml from templated XML at $(uname -n):${DEFAULT_KVM_TEMPLATES}/${myvm}.xml "
				else
					XML_SRC="${KVM_CONF_SRC_DIR}/${myvm}.xml"
				fi
				if [[ ${DEBUG} -eq 0 ]]; then
					rsync -q ${RSYNC_OPTIONS} ${XML_SRC} ${REMOTE_HOST}:${KVM_CONF_DST_DIR}/${myvm}.xml
				else
					echo rsync -q ${RSYNC_OPTIONS} ${XML_SRC} ${REMOTE_HOST}:${KVM_CONF_DST_DIR}/${myvm}.xml
				fi
				if [[ ${SKIP_DEFINE} -eq 0 ]]; then
					# Check if file exists or not
					ssh -q ${REMOTE_HOST} PATH=/bin:/opt/bin test -f ${KVM_CONF_SRC_DIR}/${myvm}.xml
					if [[ $? -ne 0 ]]; then
						echo "(II) Remote XML does not exist, copying ${KVM_CONF_SRC_DIR}/${myvm}.xml..."
						rsync ${RSYNC_OPTIONS} ${KVM_CONF_SRC_DIR}/${myvm}.xml ${REMOTE_HOST}:${KVM_CONF_DST_DIR}/${myvm}.xml
					fi
					# Edit remote file before define..
					ssh -q ${REMOTE_HOST} sed -i \
						-e 's@pc-i440fx-rhel7.6.0@pc@' \
						-e 's@pc-q35-rhel[789].[0-9].0@q35@' \
						${KVM_CONF_DST_DIR}/${myvm}.xml
					# define guest on the remote machine
					ssh -q ${REMOTE_HOST} PATH=/bin:/opt/bin virsh define ${KVM_CONF_DST_DIR}/${myvm}.xml > /dev/null
				fi
			done
		fi

		if [[ ! -z ${DISK_LIST} || ! -z ${NVRAM_LIST} ]]; then
			cd ${SRC_DIR} || exit 127
			if [[ ${VXFS_SNAPSHOTS} -eq 1 ]]; then
				# VXFS snapshot steps, is source mnt a vxfs FS?
				KVM_FS_MNT=$(df --output=target ${KVM_IMAGES_SRC_DIRS[$i]}|sed 1d)
				if [[ $(findmnt -o FSTYPE ${KVM_FS_MNT}|sed 1d) == vxfs && -x /usr/sbin/vxsnap ]]; then
					VXDG=$(/usr/bin/findmnt -n -o SOURCE ${KVM_FS_MNT}|cut -d/ -f5)
					VXLV=$(/usr/bin/findmnt -n -o SOURCE ${KVM_FS_MNT}|cut -d/ -f6)
					VXSNAP_LV=${VXLV}_snapshot
					VXSNAP_MNT=${VXSNAP_PREFIX}/${VXSNAP_LV}

					# Start snapshot process
					echo "(II) Creating VXFS snapshot for ${VXDG}/${VXLV}..."
					/usr/sbin/vxsnap -g ${VXDG} prepare ${VXLV}
					/usr/sbin/vxsnap -g ${VXDG} make source=${VXLV}/newvol=${VXSNAP_LV}/${VXSNAP_OPTS}

					# Create snapshot mount dir
					if [[ ! -d ${VXSNAP_MNT} ]]; then
						mkdir -p ${VXSNAP_MNT}
					fi
					if [[ $(findmnt -o FSTYPE ${VXSNAP_MNT}|sed 1d) == vxfs ]]; then
						echo "(II) ${VXSNAP_MNT} already mounted, skipping..."
					else
						mount -t vxfs -o ro,noatime,largefiles /dev/vx/dsk/${VXDG}/${VXSNAP_LV} ${VXSNAP_MNT} || exit 125
					fi
					if [[ ! -z ${DISK_LIST} ]]; then
						DISK_LIST=$(echo ${DISK_LIST}|sed -e "s@${KVM_FS_MNT}@${VXSNAP_MNT}@g")
					fi
					if [[ ! -z ${NVRAM_LIST} ]]; then
						NVRAM_LIST=$(echo ${NVRAM_LIST}|sed -e "s@${KVM_FS_MNT}@${VXSNAP_MNT}@g")
					fi
				fi
			fi

			# Do the actual sync (Disks)
			if [[ ! -z ${DISK_LIST} ]]; then
				if [[ ${DEBUG} -eq 0 ]]; then
					echo ${DISK_LIST}| xargs -n1 |\
					xargs --replace -n1 -I% -P${NR_THREADS} rsync ${RSYNC_OPTIONS} % ${REMOTE_HOST}:${KVM_IMAGES_DST_DIRS[$i]}
					#rsync ${RSYNC_OPTIONS} ${DISK_LIST} ${REMOTE_HOST}:${KVM_IMAGES_DST_DIRS[$i]}
				else
					echo ${DISK_LIST}| xargs -n1 |\
					xargs --replace -n1 -I% -P${NR_THREADS} echo rsync ${RSYNC_OPTIONS} % ${REMOTE_HOST}:${KVM_IMAGES_DST_DIRS[$i]}
					#echo rsync ${RSYNC_OPTIONS} ${DISK_LIST} ${REMOTE_HOST}:${KVM_IMAGES_DST_DIRS[$i]}
				fi
			fi
			# Do the actual sync (NVRAMs)
			if [[ ! -z ${NVRAM_LIST} ]]; then
				if [[ ${DEBUG} -eq 0 ]]; then
					rsync ${RSYNC_OPTIONS} ${NVRAM_LIST} ${REMOTE_HOST}:${KVM_NVRAM_DST_DIRS[$i]}
				else
					echo rsync ${RSYNC_OPTIONS} ${NVRAM_LIST} ${REMOTE_HOST}:${KVM_NVRAM_DST_DIRS[$i]}
				fi
			fi

			# Destroy the snapshot
			if [[ ${VXFS_SNAPSHOTS} -eq 1 ]]; then
				if [[ $(findmnt -o FSTYPE ${VXSNAP_MNT}|sed 1d) == vxfs ]]; then
					umount ${VXSNAP_MNT}
					if [[ $? -eq 0 ]]; then
						echo "(II) Destroying VXFS snapshot for ${VXDG}/${VXLV}..."
						/usr/sbin/vxsnap -g ${VXDG} dis ${VXSNAP_LV}
						/usr/sbin/vxedit -g ${VXDG} -fr rm ${VXSNAP_LV}
						/usr/sbin/vxsnap -g ${VXDG} unprepare ${VXLV}
					else
						echo "(**) Failed umounting ${VXSNAP_MNT}, skipping snapshot deletion..."
					fi
				fi
			fi
		fi

		# Push the tools
		echo "(II) Copying tools to ${REMOTE_HOST}:$(dirname ${KVM_IMAGES_DST_DIRS[$i]})..."
		if [[ ${DEBUG} -eq 0 ]]; then
			rsync ${RSYNC_OPTIONS} ${PATH_SCRIPT} ${REMOTE_HOST}:$(dirname ${KVM_IMAGES_DST_DIRS[$i]})
		else
			echo rsync ${RSYNC_OPTIONS} ${PATH_SCRIPT} ${REMOTE_HOST}:$(dirname ${KVM_IMAGES_DST_DIRS[$i]})
		fi
		i=$((i+1))
	else
		echo "*** VM Directory: ${SRC_DIR} not found!"
	fi
done

if [[ ${POWEROFF} -eq 1 ]]; then
	sleep 1.0
	echo "(II) running hastop -local on remote host ${REMOTE_HOST}"
	ssh -t ${REMOTE_HOST} "sync;/opt/VRTSvcs/bin/hastop -local 2>/dev/null"
	echo "(II) running /sbin/poweroff on remote host ${REMOTE_HOST}"
	ssh -t ${REMOTE_HOST} "sync;/sbin/poweroff"
fi
