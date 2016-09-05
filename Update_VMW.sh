#!/bin/bash
FCX="fedoralib"
VTEMP_DIR=$(mktemp -p /tmp -d VMWFedora232425XXXXXXX)
MODS_SRC_DIR=/usr/lib/vmware/modules/source

# Check for root
if [ "x$(id -u)" != "x0" ]; then
	echo "(**) Run this tool as root!"; exit 1
fi

# Check distro
KVER=$(uname -r|grep fc2[34])
if [ "x${KVER}" = "x" ];then
	echo "(**) Fedora 23 not detected, Exit!"
	exit 1
fi

# Check presence of VMW
if [ -f /usr/lib/vmware/lib/libvmwareui.so/libvmwareui.so ]; then
	echo "(II) /usr/lib/vmware/lib/libvmwareui.so/libvmwareui.so present, continuing..."
else
	echo "(**) VMWare Workstation not detected, exit!"; exit 1
fi

# Force use of VMWare bundled libs
if [ -f /etc/vmware/bootstrap ]; then
	grep -q VMWARE_USE_SHIPPED_LIBS /etc/vmware/bootstrap
	if [ $? -eq 0 ]; then
		echo "(II) /etc/vmware/bootstrap already has VMWARE_USE_SHIPPED_LIBS, skipping.."
	else
		echo "(II) Patching /etc/vmware/bootstrap..."
		echo "export VMWARE_USE_SHIPPED_LIBS=force" >> /etc/vmware/bootstrap
	fi
fi

#

for mylib in $(rpm -ql glib2|grep '/usr/lib64/libg.*so\.0$')
do
	tgtlib="/usr/lib/vmware/lib/$(basename $mylib)/$(basename $mylib)"
	if [ ! -f "${tgtlib}.${FCX}" ]; then
		echo "(II) Backing up to ${tgtlib}.${FCX}..."
		/bin/cp -Lfv ${tgtlib} ${tgtlib}.${FCX}
	fi
	echo "(II) Replacing ${tgtlib} ..."
 	/bin/cp -Lfv ${mylib} ${tgtlib}
done

# Look for GNU patch..
if [ ! -x /usr/bin/patch ]; then
	echo "(EE) Please install GNU patch (/usr/bin/patch) to patch the VMWare kernel modules.."
	exit 127
fi

# Get ready to patch the sources..
if [ "x${VTEMP_DIR}" != "x" ]; then
	cd ${VTEMP_DIR} || exit 127
fi

cat <<'EOF1' > modules.patch
diff -ur vmw_mods.orig/vmblock-only/linux/control.c vmw_mods/vmblock-only/linux/control.c
--- vmw_mods.orig/vmblock-only/linux/control.c	2016-04-14 19:31:30.000000000 -0400
+++ vmw_mods/vmblock-only/linux/control.c	2016-09-05 15:13:34.526217167 -0400
@@ -208,9 +208,11 @@
    VMBlockSetProcEntryOwner(controlProcMountpoint);
 
    /* Create /proc/fs/vmblock/dev */
-   controlProcEntry = create_proc_entry(VMBLOCK_CONTROL_DEVNAME,
-                                        VMBLOCK_CONTROL_MODE,
-                                        controlProcDirEntry);
+   controlProcEntry = proc_create(VMBLOCK_CONTROL_DEVNAME, 
+				  VMBLOCK_CONTROL_MODE,
+                                  controlProcDirEntry, 
+				  &ControlFileOps);
+
    if (!controlProcEntry) {
       Warning("SetupProcDevice: could not create " VMBLOCK_DEVICE "\n");
       remove_proc_entry(VMBLOCK_CONTROL_MOUNTPOINT, controlProcDirEntry);
@@ -218,7 +220,10 @@
       return -EINVAL;
    }
 
+#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 10, 0)
    controlProcEntry->proc_fops = &ControlFileOps;
+#endif
+
    return 0;
 }
 
@@ -282,18 +287,24 @@
    int i;
    int retval;
 
-   name = getname(buf);
+   name = __getname();
    if (IS_ERR(name)) {
       return PTR_ERR(name);
    }
 
+   i = strncpy_from_user(name, buf, PATH_MAX);
+   if (i < 0 || i == PATH_MAX) {
+      __putname(name);
+      return -EINVAL;
+   }
+
    for (i = strlen(name) - 1; i >= 0 && name[i] == '/'; i--) {
       name[i] = '\0';
    }
 
    retval = i < 0 ? -EINVAL : blockOp(name, blocker);
 
-   putname(name);
+   __putname(name);
 
    return retval;
 }
diff -ur vmw_mods.orig/vmblock-only/linux/dentry.c vmw_mods/vmblock-only/linux/dentry.c
--- vmw_mods.orig/vmblock-only/linux/dentry.c	2016-04-14 19:31:30.000000000 -0400
+++ vmw_mods/vmblock-only/linux/dentry.c	2016-09-05 15:13:34.528217197 -0400
@@ -63,7 +63,7 @@
                    struct nameidata *nd)   // IN: lookup flags & intent
 {
    VMBlockInodeInfo *iinfo;
-   struct nameidata actualNd;
+   struct path actualNd;
    struct dentry *actualDentry;
    int ret;
 
diff -ur vmw_mods.orig/vmblock-only/linux/file.c vmw_mods/vmblock-only/linux/file.c
--- vmw_mods.orig/vmblock-only/linux/file.c	2016-04-14 19:31:30.000000000 -0400
+++ vmw_mods/vmblock-only/linux/file.c	2016-09-05 15:13:34.528217197 -0400
@@ -132,7 +132,7 @@
     * and that would try to acquire the inode's semaphore; if the two inodes
     * are the same we'll deadlock.
     */
-   if (actualFile->f_dentry && inode == actualFile->f_dentry->d_inode) {
+   if (actualFile->f_path.dentry && inode == actualFile->f_path.dentry->d_inode) {
       Warning("FileOpOpen: identical inode encountered, open cannot succeed.\n");
       if (filp_close(actualFile, current->files) < 0) {
          Warning("FileOpOpen: unable to close opened file.\n");
@@ -166,11 +166,9 @@
 
 static int
 FileOpReaddir(struct file *file,  // IN
-              void *dirent,       // IN
-              filldir_t filldir)  // IN
+		struct dir_context *ctx)
 {
    int ret;
-   FilldirInfo info;
    struct file *actualFile;
 
    if (!file) {
@@ -184,11 +182,8 @@
       return -EINVAL;
    }
 
-   info.filldir = filldir;
-   info.dirent = dirent;
-
    actualFile->f_pos = file->f_pos;
-   ret = vfs_readdir(actualFile, Filldir, &info);
+   ret = iterate_dir(actualFile, ctx);
    file->f_pos = actualFile->f_pos;
 
    return ret;
@@ -237,7 +232,7 @@
 
 
 struct file_operations RootFileOps = {
-   .readdir = FileOpReaddir,
+   .iterate = FileOpReaddir,
    .open    = FileOpOpen,
    .release = FileOpRelease,
 };
diff -ur vmw_mods.orig/vmblock-only/linux/filesystem.c vmw_mods/vmblock-only/linux/filesystem.c
--- vmw_mods.orig/vmblock-only/linux/filesystem.c	2016-04-14 19:31:30.000000000 -0400
+++ vmw_mods/vmblock-only/linux/filesystem.c	2016-09-05 15:13:34.529217212 -0400
@@ -322,7 +322,7 @@
 {
    VMBlockInodeInfo *iinfo;
    struct inode *inode;
-   struct nameidata actualNd;
+   struct path actualNd;
 
    ASSERT(sb);
 
diff -ur vmw_mods.orig/vmblock-only/linux/inode.c vmw_mods/vmblock-only/linux/inode.c
--- vmw_mods.orig/vmblock-only/linux/inode.c	2016-04-14 19:31:30.000000000 -0400
+++ vmw_mods/vmblock-only/linux/inode.c	2016-09-05 15:13:34.530217227 -0400
@@ -35,10 +35,18 @@
 
 
 /* Inode operations */
-static struct dentry *InodeOpLookup(struct inode *dir,
-                                    struct dentry *dentry, struct nameidata *nd);
+
+#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 10, 0)
+static struct dentry *InodeOpLookup(struct inode *dir, struct dentry *dentry, struct nameidata *nd);
 static int InodeOpReadlink(struct dentry *dentry, char __user *buffer, int buflen);
-#if LINUX_VERSION_CODE >= KERNEL_VERSION(2, 6, 13)
+#else
+static struct dentry *InodeOpLookup(struct inode *, struct dentry *, unsigned int);
+static int InodeOpReadlink(struct dentry *, char __user *, int);
+#endif
+
+#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 2, 0)
+static const char *InodeOpFollowlink(struct dentry *dentry, void **cookie);
+#elif LINUX_VERSION_CODE >= KERNEL_VERSION(2, 6, 13)
 static void *InodeOpFollowlink(struct dentry *dentry, struct nameidata *nd);
 #else
 static int InodeOpFollowlink(struct dentry *dentry, struct nameidata *nd);
@@ -49,12 +57,15 @@
    .lookup = InodeOpLookup,
 };
 
+#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 13, 0)
 static struct inode_operations LinkInodeOps = {
+#else
+struct inode_operations LinkInodeOps = {
+#endif
    .readlink    = InodeOpReadlink,
    .follow_link = InodeOpFollowlink,
 };
 
-
 /*
  *----------------------------------------------------------------------------
  *
@@ -75,7 +86,11 @@
 static struct dentry *
 InodeOpLookup(struct inode *dir,      // IN: parent directory's inode
               struct dentry *dentry,  // IN: dentry to lookup
-              struct nameidata *nd)   // IN: lookup intent and information
+#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 10, 0)
+	      struct nameidata *nd)   // IN: lookup intent and information
+#else
+              unsigned int flags)
+#endif
 {
    char *filename;
    struct inode *inode;
@@ -135,7 +150,12 @@
    inode->i_size = INODE_TO_IINFO(inode)->nameLen;
    inode->i_version = 1;
    inode->i_atime = inode->i_mtime = inode->i_ctime = CURRENT_TIME;
+#if LINUX_VERSION_CODE < KERNEL_VERSION(3, 10, 0)
    inode->i_uid = inode->i_gid = 0;
+#else
+   inode->i_gid = make_kgid(current_user_ns(), 0);
+   inode->i_uid = make_kuid(current_user_ns(), 0);
+#endif
    inode->i_op = &LinkInodeOps;
 
    d_add(dentry, inode);
@@ -177,7 +197,12 @@
       return -EINVAL;
    }
 
-   return vfs_readlink(dentry, buffer, buflen, iinfo->name);
+#if LINUX_VERSION_CODE <= KERNEL_VERSION(3, 14, 99)
+	return vfs_readlink(dentry, buffer, buflen, iinfo->name);
+#else
+       return readlink_copy(buffer, buflen, iinfo->name);
+#endif
+
 }
 
 
@@ -198,13 +223,20 @@
  *----------------------------------------------------------------------------
  */
 
-#if LINUX_VERSION_CODE >= KERNEL_VERSION(2, 6, 13)
+#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 2, 0)
+static const char *
+#elif LINUX_VERSION_CODE >= KERNEL_VERSION(2, 6, 13)
 static void *
 #else
 static int
 #endif
 InodeOpFollowlink(struct dentry *dentry,  // IN : dentry of symlink
-                  struct nameidata *nd)   // OUT: stores result
+#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 2, 0)
+		  void **cookie
+#else
+		  struct nameidata *nd
+#endif
+		  )   // OUT: stores result
 {
    int ret;
    VMBlockInodeInfo *iinfo;
@@ -221,7 +253,11 @@
       goto out;
    }
 
-   ret = vfs_follow_link(nd, iinfo->name);
+#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 2, 0)
+   return *cookie = (char *)(iinfo->name);
+#else
+   nd_set_link(nd, iinfo->name);
+#endif
 
 out:
 #if LINUX_VERSION_CODE >= KERNEL_VERSION(2, 6, 13)
@@ -230,3 +266,4 @@
    return ret;
 #endif
 }
+
diff -ur vmw_mods.orig/vmblock-only/shared/compat_namei.h vmw_mods/vmblock-only/shared/compat_namei.h
--- vmw_mods.orig/vmblock-only/shared/compat_namei.h	2016-04-14 19:31:30.000000000 -0400
+++ vmw_mods/vmblock-only/shared/compat_namei.h	2016-09-05 15:13:34.530217227 -0400
@@ -26,21 +26,21 @@
  * struct. They were both replaced with a struct path.
  */
 #if LINUX_VERSION_CODE >= KERNEL_VERSION(2, 6, 25)
-#define compat_vmw_nd_to_dentry(nd) (nd).path.dentry
+#define compat_vmw_nd_to_dentry(nd) (nd).dentry
 #else
 #define compat_vmw_nd_to_dentry(nd) (nd).dentry
 #endif
 
 /* In 2.6.25-rc2, path_release(&nd) was replaced with path_put(&nd.path). */
 #if LINUX_VERSION_CODE >= KERNEL_VERSION(2, 6, 25)
-#define compat_path_release(nd) path_put(&(nd)->path)
+#define compat_path_release(nd) path_put(nd)
 #else
 #define compat_path_release(nd) path_release(nd)
 #endif
 
 /* path_lookup was removed in 2.6.39 merge window VFS merge */
 #if LINUX_VERSION_CODE >= KERNEL_VERSION(2, 6, 38)
-#define compat_path_lookup(name, flags, nd)     kern_path(name, flags, &((nd)->path))
+#define compat_path_lookup(name, flags, nd)     kern_path(name, flags, nd)
 #else
 #define compat_path_lookup(name, flags, nd)     path_lookup(name, flags, nd)
 #endif
diff -ur vmw_mods.orig/vmmon-only/linux/hostif.c vmw_mods/vmmon-only/linux/hostif.c
--- vmw_mods.orig/vmmon-only/linux/hostif.c	2016-04-14 19:48:44.000000000 -0400
+++ vmw_mods/vmmon-only/linux/hostif.c	2016-09-05 15:33:03.514131550 -0400
@@ -1162,8 +1162,13 @@
    int retval;
 
    down_read(&current->mm->mmap_sem);
+#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 6, 0)
+   retval = get_user_pages_remote(current, current->mm, (unsigned long)uvAddr,
+   numPages, 0, 0, ppages, NULL);
+#else
    retval = get_user_pages(current, current->mm, (unsigned long)uvAddr,
-                           numPages, 0, 0, ppages, NULL);
+   numPages, 0, 0, ppages, NULL);
+#endif
    up_read(&current->mm->mmap_sem);
 
    return retval != numPages;
diff -ur vmw_mods.orig/vmnet-only/netif.c vmw_mods/vmnet-only/netif.c
--- vmw_mods.orig/vmnet-only/netif.c	2016-04-14 19:48:47.000000000 -0400
+++ vmw_mods/vmnet-only/netif.c	2016-09-05 15:25:32.794994960 -0400
@@ -39,8 +39,8 @@
 #include <linux/proc_fs.h>
 #include <linux/file.h>
 
-#include "vnetInt.h"
 #include "compat_netdevice.h"
+#include "vnetInt.h"
 #include "vmnetInt.h"
 
 
@@ -465,7 +465,11 @@
    VNetSend(&netIf->port.jack, skb);
 
    netIf->stats.tx_packets++;
+   #if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 7, 0)
+   netif_trans_update(dev);
+   #else
    dev->trans_start = jiffies;
+   #endif
 
    return 0;
 }
diff -ur vmw_mods.orig/vmnet-only/userif.c vmw_mods/vmnet-only/userif.c
--- vmw_mods.orig/vmnet-only/userif.c	2016-04-14 19:48:47.000000000 -0400
+++ vmw_mods/vmnet-only/userif.c	2016-09-05 15:33:55.618911804 -0400
@@ -113,8 +113,12 @@
    int retval;
 
    down_read(&current->mm->mmap_sem);
+#if LINUX_VERSION_CODE >= KERNEL_VERSION(4, 6, 0)
+   retval = get_user_pages_remote(current, current->mm, addr, 1, 1, 0, &page, NULL);
+#else
    retval = get_user_pages(current, current->mm, addr,
-			   1, 1, 0, &page, NULL);
+   1, 1, 0, &page, NULL);
+#endif
    up_read(&current->mm->mmap_sem);
 
    if (retval != 1) {
diff -ur vmw_mods.orig/vmnet-only/vm_device_version.h vmw_mods/vmnet-only/vm_device_version.h
--- vmw_mods.orig/vmnet-only/vm_device_version.h	2016-04-14 19:48:47.000000000 -0400
+++ vmw_mods/vmnet-only/vm_device_version.h	2016-09-05 15:13:34.532217257 -0400
@@ -53,7 +53,9 @@
  *    VMware HD Audio codec
  *    VMware HD Audio controller
  */
+#ifndef PCI_VENDOR_ID_VMWARE
 #define PCI_VENDOR_ID_VMWARE                    0x15AD
+#endif
 #define PCI_DEVICE_ID_VMWARE_SVGA2              0x0405
 #define PCI_DEVICE_ID_VMWARE_SVGA               0x0710
 #define PCI_DEVICE_ID_VMWARE_VGA                0x0711
EOF1

cat <<'EOF2' > util.patch
--- scripts/util.sh.orig	2015-12-09 16:07:06.000000000 -0500
+++ scripts/util.sh	2016-03-02 14:35:04.000000000 -0500
@@ -362,7 +362,7 @@
     echo no
   else
     if [ "$vmdb_VMBLOCK_CONFED" = 'yes' ]; then
-      echo yes
+      echo no
     else
       echo no
     fi
EOF2

if [ "x${VTEMP_DIR}" != "x" ]; then
	cd ${VTEMP_DIR} || exit 127
	for mymod in vmmon vmnet vmblock
	do
	if [ -f ${MODS_SRC_DIR}/${mymod}.tar.orig ]; then
		echo "(WW) Backup ${MODS_SRC_DIR}/${mymod}.tar.orig already exists! Restoring original tar..."
		/bin/cp -fv ${MODS_SRC_DIR}/${mymod}.tar.orig  ${MODS_SRC_DIR}/${mymod}.tar
	fi
	done

	for mymod in vmmon vmnet vmblock
	do
		if [ -f ${MODS_SRC_DIR}/${mymod}.tar ]; then
			echo "(II) Extracting  ${MODS_SRC_DIR}/${mymod}.tar  into ${VTEMP_DIR}..."
			/usr/bin/tar xf ${MODS_SRC_DIR}/${mymod}.tar  || exit 127
		fi
	done
	echo "(II) Patching modules..."
	patch -p1 <  modules.patch

	for mymod in vmmon vmnet vmblock
	do
		if [ -f ${MODS_SRC_DIR}/${mymod}.tar.orig ]; then
			echo "(WW) Backup ${MODS_SRC_DIR}/${mymod}.tar.orig already exists! Not replacing original tar backup..."
		else
			echo "(II) Rebuilding ${MODS_SRC_DIR}/${mymod}.tar from ${VTEMP_DIR}/${mymod}-only ..."
			/bin/cp -Lfv ${MODS_SRC_DIR}/${mymod}.tar{,.orig}
		fi
		/usr/bin/tar cf ${MODS_SRC_DIR}/${mymod}.tar ${mymod}-only || exit 127
	done

	# on recent systems VMW uses vmware-fuse-block, no need for vmblock anymore..
	if [ -f /usr/lib/vmware/scripts/util.sh.orig ]; then
		echo "(WW) /usr/lib/vmware/scripts/util.sh.orig already exists! Not patching..."
	else
		echo "(II) Patching util.sh..."
		/bin/cp -Lfv /usr/lib/vmware/scripts/util.sh{,.orig}
		patch -p1 < util.patch /usr/lib/vmware/scripts/util.sh
	fi
fi

# End
echo "(II) Now run: vmware-modconfig --console --install-all"
