#!/bin/bash
#
# description: Encapsulate RHEL7 root into LVM2
# Comments kept small due to: https://bugzilla.redhat.com/show_bug.cgi?id=1433088
#
# Red Hat
# Vincent S. Cojot, 2017-03-08
# Original idea: Jason Woods, 2016-05-17

export PATH=/sbin:/bin:/usr/bin
set -x ; VLOG=/run/firstboot-encapsulate_rootvol.log ; exec &> >(/bin/tee -a "${VLOG}")

# Global vars
HYPERVISORS=0
rootdelay_default=15
boot_dg=rootdg					# EDITABLE
boot_lv=lv_root					# EDITABLE
# ${temp_disk} : This disk will be WIPED clean, be careful
temp_disk=/dev/sdc				# EDITABLE
temp_part="${temp_disk}1"

declare -A boot_vols
boot_vols["${boot_lv}"]="16g"			# EDITABLE
boot_vols["lv_var"]="32g"			# EDITABLE
boot_vols["lv_home"]="2g"			# EDITABLE
boot_vols["lv_tmp"]="2g"			# EDITABLE
declare -A vol_mounts
vol_mounts["${boot_lv}"]="/"
vol_mounts["lv_var"]="/var"			# EDITABLE
vol_mounts["lv_home"]="/home"			# EDITABLE
vol_mounts["lv_tmp"]="/tmp"			# EDITABLE

function init_vars() {
	if [ ! -d /var/log/ospd ]; then
		mkdir /var/log/ospd
	fi

	# Save some vars
	/usr/bin/findmnt -nr -o source / > /var/log/ospd/root_part
	sed -e 's@[0-9]*$@@' /var/log/ospd/root_part > /var/log/ospd/root_disk

	# How big?
	min_root_size=$(( $(df --output=used /|sed 1d) + 1024*1024))

	# Safety checks
	if [ "x$(cat /var/log/ospd/root_part)" = "x/dev/mapper/${boot_dg}-${boot_lv}" ]; then
		echo "(II) encapsulate_rootvol.sh already performed LVM2 encapsulation!"
		exit 0
	fi
}

function check_prereqs_and_cleanup() {
	if [ -b ${temp_disk} ]; then
		# Disable SELinux
		setenforce 0

		# Scan
		pvscan; vgscan; vgchange -a n
		# Cleanup previous attempts
		umount -f /dev/${boot_dg}/${boot_lv}
		vgreduce --remove-missing --force ${boot_dg}
		vgremove --force --noudevsync ${boot_dg}
		pvremove -ff -y ${source_part}
		pvremove -ff -y ${temp_part}
		sgdisk -Z ${temp_disk}
		sgdisk -g ${temp_disk}
		# Again
		vgreduce --remove-missing --force ${boot_dg}
		vgremove --force --noudevsync ${boot_dg}
		# Cleanup special files..
		/bin/rm -rfv /dev/${boot_dg}

		# VM stuff
		if [ -x /sbin/virt-what ]; then
			HYPERVISORS=$(/sbin/virt-what|egrep '(kvm|virtualbox|vmware)'|wc -l)
		fi
		if [ ${HYPERVISORS} -ge 1 ]; then
			echo "Virt Env. detected."
			/bin/rpm -e microcode_ctl
		fi

		# SELinux is not disabled permanently, fear not.
		/bin/cp -fax /etc/selinux/config /etc/selinux/config.before_lvmroot
		sed -i -e "s/SELINUX=enforcing/SELINUX=permissive/" /etc/selinux/config
		fixfiles -f -F restore /etc/selinux/config
	else
		# Abort silently
		exit 0
	fi
}


function inject_lvmboot_config () {

	# Inject a service
	cat > /etc/systemd/system/lvmroot-relocate.service << EOF
[Unit]
Description=LVM Root Relocate
After=local-fs.target dracut-mount.service
Conflicts=shutdown.target emergency.target
DefaultDependencies=no
Before=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/libexec/lvmroot-relocate.sh
StandardInput=null
StandardOutput=syslog
StandardError=syslog+console
KillMode=process
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
EOF

	cat > /usr/libexec/lvmroot-relocate.sh << EOF2
#!/bin/bash
export PATH=/sbin:/usr/sbin:/bin:/usr/bin
set -x ; RLOG=/var/log/ospd/firstboot-lvmroot-relocate.log ; exec &> >(/bin/tee -a "\${RLOG}")

# Sanity
if [ "x\$(/usr/bin/findmnt -nr -o source /)" != "x/dev/mapper/${boot_dg}-${boot_lv}" ]; then
	echo "(II) LVM2 encap. not detected, abort!"
	exit 127
fi

if [ -b ${temp_disk} ]; then
	# Extend VG
	parted --script ${source_disk} set ${source_part_number} lvm on
	blockdev --rereadpt ${source_disk}
	pvcreate -f -y ${source_part} || exit 0
	vgextend ${boot_dg} ${source_part}
	# Move ${boot_lv} back to original disk
	pvmove -n ${boot_lv} ${temp_part} ${source_part}
	# Move the others..
	pvmove ${temp_part} ${source_part}
	vgreduce ${boot_dg} ${temp_part}

	# Cleanup
	pvremove -y ${temp_part}
	sgdisk -Z ${temp_disk}
	sgdisk -g ${temp_disk}

	# Take backup
	vgcfgbackup ${boot_dg}

	# Get rid of the UUID references in GRUB2.
	grub2-mkconfig -o /boot/grub2/grub.cfg
	# Just in case...
	if [ ${HYPERVISORS} -ge 1 ]; then
		sed -i -e "s@root=UUID=[a-Z0-9-]* @root=/dev/mapper/${boot_dg}-${boot_lv} scsi_mod.scan=sync rootdelay=${rootdelay_default} @" /boot/grub2/grub.cfg
	else
		sed -i -e "s@root=UUID=[a-Z0-9-]* @root=/dev/mapper/${boot_dg}-${boot_lv} rootdelay=${rootdelay_default} @" /boot/grub2/grub.cfg
	fi
	dracut -f -a 'lvm dm'
	grub2-install --root-directory=/ ${source_disk}
	sync
fi

# Cleanup after myself
systemctl disable lvmroot-relocate
/bin/rm -fv /etc/systemd/system/lvmroot-relocate.service
systemctl daemon-reload

if [ -f /etc/selinux/config.before_lvmroot ]; then
	cp -fax /etc/selinux/config.before_lvmroot /etc/selinux/config
else
	sed -i -e "s/SELINUX=permissive/SELINUX=enforcing/" /etc/selinux/config
fi
fixfiles -f -F restore /

/bin/rm -fv /usr/libexec/lvmroot-relocate.sh
setenforce 1

EOF2

	# Prep for next reboot
	chmod 755 /usr/libexec/lvmroot-relocate.sh
	fixfiles -f -F restore /etc/systemd/system
	systemctl daemon-reload
	systemctl enable lvmroot-relocate
}

# TBW
function shutdown_services() {
	# Stop services prior to remounting as '/'
	systemctl stop openvswitch-network\* 
	systemctl stop systemd-journald systemd-initctl systemd-udevd-kernel virtlogd rsyslog rhel-dmesg
}

# TBW
function prepare_temp_disk() {
	pvscan; vgscan
	pvs ; vgs ; lvs
	# Create a single part
	parted --script ${temp_disk} mklabel gpt mkpart primary xfs 1 100% --script set 1 lvm on || exit 0
	blockdev --rereadpt ${temp_disk}
	pvcreate -ff -y ${temp_disk}1 || exit 0
	vgcreate -f -y ${boot_dg} ${temp_disk}1 || exit 0

	for mylv in "${!boot_vols[@]}"
	do
		lvcreate --wipesignatures y --yes -L ${boot_vols["${mylv}"]} -n ${mylv} ${boot_dg}
		mkfs.xfs -f /dev/${boot_dg}/${mylv}
	done

	# Snap again
	pvs ; vgs ; lvs
}

# TBW
function mount_temp_disk_lvm() {
	# LVM mounts
	mount /dev/${boot_dg}/${boot_lv} /mnt
	chown --reference=/ /mnt ; chmod --reference=/ /mnt
	for mylv in "${!boot_vols[@]}"
	do
		case ${mylv} in
		${boot_lv})
			NOP=1
			;;
		*)
			mkdir -p /mnt${vol_mounts["${mylv}"]}
			chown --reference=${vol_mounts["${mylv}"]} /mnt${vol_mounts["${mylv}"]}
			chmod --reference=${vol_mounts["${mylv}"]} /mnt${vol_mounts["${mylv}"]}
			mount /dev/${boot_dg}/${mylv} /mnt${vol_mounts["${mylv}"]}
			# Do it again
			chown --reference=${vol_mounts["${mylv}"]} /mnt${vol_mounts["${mylv}"]}
			chmod --reference=${vol_mounts["${mylv}"]} /mnt${vol_mounts["${mylv}"]}
			;;
		esac
	done
}

# TBW
function copy_bootdisk_to_temp_disk() {
	#
	fixfiles -f -F restore /
	/bin/cp -fax / /mnt

	# Verify SELinux contexts on target disk and prepare for next boot
	chroot /mnt systemctl enable lvmroot-relocate
	sync
}

# TBW
function update_grub() {
	if [ "x$1" != "x" ]; then
		myroot="$1"
	else
		myroot="/"
	fi
	if [ "x${myroot}" = "x/" ]; then
		grub2-mkconfig -o /boot/grub2/grub.cfg
	fi
	if [ -f ${myroot}/etc/default/grub ]; then
		if [ ! -f ${myroot}/etc/default/grub.before_lvmroot ]; then
			/bin/cp -fax ${myroot}/etc/default/grub ${myroot}/etc/default/grub.before_lvmroot
		fi
		# Remove duplicates
		sed -i -e "s@rd.lvm.lv=${boot_dg}/${boot_lv}@@g" ${myroot}/etc/default/grub
		if [ ${HYPERVISORS} -ge 1 ]; then
			sed -i -e "s@rhgb quiet@scsi_mod.scan=sync rootdelay=${rootdelay_default}@" ${myroot}/etc/default/grub
		else
			sed -i -e "s@rhgb quiet@rootdelay=${rootdelay_default}@" ${myroot}/etc/default/grub
		fi
	fi

	if [ -f ${myroot}/boot/grub2/grub.cfg ]; then
		if [ ! -f ${myroot}/boot/grub2/grub.cfg.before_lvmroot ]; then
			/bin/cp -fax ${myroot}/boot/grub2/grub.cfg ${myroot}/boot/grub2/grub.cfg.before_lvmroot
		fi
		chroot ${myroot} /sbin/grubby --update-kernel=ALL --remove-args="root rhgb quiet"
		chroot ${myroot} /sbin/grubby --update-kernel=ALL --args="root=/dev/mapper/${boot_dg}-${boot_lv} rd.lvm.lv=${boot_dg}/${boot_lv} rootdelay=${rootdelay_default} dolvm"
		if [ ${HYPERVISORS} -ge 1 ]; then
			chroot ${myroot} /sbin/grubby --update-kernel=ALL --args="scsi_mod.scan=sync"
		fi
	fi
	fixfiles -f -F restore ${myroot}/etc ${myroot}/boot
	# Old-fashioned sync
	sync
}

# TBW
function update_fstab() {
	if [ "x$1" != "x" ]; then
		myroot="$1"
	else
		myroot="/"
	fi
	myfstab="${myroot}/etc/fstab"
	if [ -f ${myfstab} ]; then
		/bin/cp -fax ${myfstab} ${myfstab}.before_lvmroot
		for mylv in "${!boot_vols[@]}"
		do
			xflags="defaults"
			case ${vol_mounts["${mylv}"]} in
				'/')
					sed -i -e "s@^LABEL=img-rootfs@/dev/${boot_dg}/${boot_lv}@" ${myfstab}
					;;
				'/tmp')
					xflags="nodev,nosuid,noexec"
					;;
				'/home')
					xflags="nodev"
					;;
				'*')
					xflags="defaults"
					;;
			esac
			grep -q "^/dev/${boot_dg}/${mylv}" ${myfstab}
			if [ $? != 0 ]; then
				sed -i -e "\@\s${vol_mounts["${mylv}"]}\s@d" ${myfstab}
				echo -e "/dev/${boot_dg}/${mylv}\t${vol_mounts["${mylv}"]}\txfs\t${xflags}\t0 2" >> ${myfstab}
			fi
		done
	fi
}

# TBW
remount_temp_disk_lvm() {
	sync

	# Perform LVM umounts
	for mylv in "${!boot_vols[@]}"
	do
		case ${mylv} in
		${boot_lv})
			#Skip unmounting root, do it at the end.
			OK=1
			;;
		*)
			umount /mnt${vol_mounts["${mylv}"]}
			;;
		esac
	done
	umount /mnt

	# Perform LVM remounts on the real '/'
	for mylv in "${!boot_vols[@]}"
	do
		case ${mylv} in
		${boot_lv})
			mount /dev/${boot_dg}/${mylv} /
			;;
		*)
			mount /dev/${boot_dg}/${mylv} /${vol_mounts["${mylv}"]}
			;;
		esac
	done
	fixfiles -f -F restore /
}

# TBW
function regenerate_initrd() {
	dracut -f -a 'lvm dm'
	fixfiles -f -F restore /boot
	sync
}

# TBW
function run_grub_install_temp_mounted() {
	if [ "x$1" != "x" ]; then
		myroot="$1"
	else
		myroot="/"
	fi
	grub2-install --root-directory=${myroot} ${source_disk}
	sync
}

# TBW
update_grub_root_spec() {
	perl -pi -e 's@set timeout=5@set timeout=10@g' /boot/grub2/grub.cfg
	# Check if root spec wasn't updated
	grep -q 'set root=.*hd0,msdos[0-9]' /boot/grub2/grub.cfg
	#
	if [ $? -eq 0 ]; then
		myvgid=$(vgs --nosuffix --noheadings -o uuid ${boot_dg}|xargs)
		mylvid=$(lvs --nosuffix --noheadings -o uuid ${boot_dg}/${boot_lv}|xargs)
		sed -i -e "s@hd0,msdos[0-9]@lvmid/${boot_dg}/${boot_lv}@" /boot/grub2/grub.cfg
		grub2-install --root-directory=/ ${source_disk}
		sync
	fi
}

# TBW
function launch_umount_and_reboot() {
	sync
	umount /
	sync
	if [ -f /proc/sys/vm/drop_caches ]; then
		echo "Flushing caches ..."
		echo 1 > /proc/sys/vm/drop_caches
	fi
	sync

	# If running on virtual H/W, wait a little while as not to
	# overload the Hypervisor if all nodes proceed at the same time..
	if [ ${HYPERVISORS} -ge 1 ]; then
		echo "Virt Env. detected, sleeping % 480..."
		sleep $(($(od -A n -t d -N 3 /dev/urandom) % 480))
	fi
	sync
	reboot
}

#
# Start
init_vars

# Load vars
my_node_role=$(cat /var/log/ospd/node_role)
source_disk=$(cat /var/log/ospd/root_disk)
source_part=$(cat /var/log/ospd/root_part)
source_part_number=$(cat /var/log/ospd/root_part|sed -e "s@${source_disk}@@")

# Start
check_prereqs_and_cleanup
inject_lvmboot_config

# Make the LVM disk, prepare volumes, copy source to destination pv
prepare_temp_disk
mount_temp_disk_lvm
update_fstab /
update_grub /
shutdown_services
copy_bootdisk_to_temp_disk
update_grub /mnt
run_grub_install_temp_mounted /mnt

# Remount ${boot_dg}/${boot_lv} to '/'
update_grub_root_spec
/bin/cp -axfv /boot/grub2/grub.cfg /mnt/boot/grub2/grub.cfg
sync
remount_temp_disk_lvm

# Update boot environement.
update_grub /
regenerate_initrd

# Needed avoid lvmid issues
run_grub_install_temp_mounted /

# 2x
update_grub_root_spec

# Save log
cat ${VLOG} > /var/log/ospd/firstboot-encapsulate_rootvol.log

# Last step
launch_umount_and_reboot
