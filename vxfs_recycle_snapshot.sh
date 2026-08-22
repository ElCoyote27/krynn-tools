#!/bin/bash
# $Id: vxfs_recycle_snapshot.sh,v 1.7 2026/08/22 12:08:00 root Exp $

set -x

export PATH=/usr/sbin:/usr/bin:/opt/VRTSvcs/bin:/usr/lib/vxvm/bin:/opt/VRTSvxfs/sbin:/opt/VRTS/bin

VXSNAP_PREFIX=/run/user/$(id -u)
VXSNAP_OPTS="cachesize=1536g/autogrow=yes"

KVM_FS_MNT=/shared/kvm0
VXDG=$(/usr/bin/findmnt -n -o SOURCE ${KVM_FS_MNT}|cut -d/ -f5)
VXLV=$(/usr/bin/findmnt -n -o SOURCE ${KVM_FS_MNT}|cut -d/ -f6)
VXSNAP_LV=${VXLV}_snapshot
VXDG=$(vxprint -r ${VXLV}|grep '^Disk group:'|awk '{ print $3}')
VXSNAP_MNT=${VXSNAP_PREFIX}/${VXSNAP_LV}

# Start snapshot process
vxsnap -g ${VXDG} prepare ${VXLV}
if ! vxsnap -g ${VXDG} make source=${VXLV}/newvol=${VXSNAP_LV}/${VXSNAP_OPTS}
then
	echo "(**) vxsnap make failed, unpreparing ${VXDG}/${VXLV}..."
	vxsnap -g ${VXDG} -f unprepare ${VXLV}
	exit 1
fi

if [[ ! -d ${VXSNAP_MNT} ]]; then
	mkdir -p ${VXSNAP_MNT}
fi
if [[ $(findmnt -o FSTYPE ${VXSNAP_MNT}|sed 1d) == vxfs ]]; then
	echo "(II) ${VXSNAP_MNT} already mounted, skipping..."
else
	mount -t vxfs -o ro,noatime,largefiles /dev/vx/dsk/${VXDG}/${VXSNAP_LV} ${VXSNAP_MNT}
fi
sync
sleep 5s
umount ${VXSNAP_MNT}
vxsnap -g ${VXDG} dis ${VXSNAP_LV}
vxedit -g ${VXDG} -fr rm ${VXSNAP_LV}
vxsnap -g ${VXDG} -f unprepare ${VXLV}
