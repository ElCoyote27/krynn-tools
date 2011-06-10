#!/bin/bash
# $Id: Find_Deleted_Inodes.sh,v 1.1 2011/06/10 10:24:12 root Exp $
#
# Find process that have deleted inodes still open 
# Author : Franck JOUVANCEAU

function finddeleted
{
[ -f /bin/nawk ] && { OPT="";AWK=nawk; } || { OPT="-maxdepth 1 -follow";AWK=awk; }
find /proc/*/fd $OPT -name "[0-9]*" -links 0 -type f -ls 2>/dev/null |$AWK '
	BEGIN{
		format="%-17s %12s %10s %-20s\t %-9s %-5s %s\n";
		printf(format,"FileDescriptor","Size(k)","Inum","FileSystem","User","pid"," Process");
	}
	{
		file=$NF;
		inum=$1;
		size=$(NF-4);
		split(file,f,"/");
		pid=f[3];
		cmd="df -k "file" 2>/dev/null";
		cmd |getline;
		cmd |getline;
		close(cmd);
		fs=$NF;
		cmd="ps -p "pid" -o user,args";
		cmd |getline;
		cmd |getline;
		close(cmd);
		user=$1;$1="";
		if(user=="USER") next;
		process=$0;
		printf(format,file,int(size/1024),inum,fs,user,pid,process);
		if(inums[fs,inum]=="") {
			totalsize[fs]+=size;
			inums[fs,inum]=1;
		}
	}
	END{
		print   "-----------------";
		for (fs in totalsize)
			printf(format,"TOTAL",int(totalsize[fs]/1024),"",fs,"","","");
	}
	'
}

finddeleted $*
