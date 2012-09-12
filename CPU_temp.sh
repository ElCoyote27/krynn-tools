#!/bin/bash
# $Id: CPU_temp.sh,v 1.1 2012/09/12 14:55:11 root Exp $
if [ -x /usr/bin/sensors ]; then
	TEMPS=`/usr/bin/sensors|grep Core|(while read a core temp scale max; do echo $temp; done)|sort -u`
else
	echo "/usr/bin/sensors not found!"
fi
for mytemp in $TEMPS
do
	CPU_CORES=`/usr/bin/sensors|grep "Core.*${mytemp} C"|awk '{ print $2}'|sed -e 's/://'|xargs|sed -e 's/ /,/g'`
	MAX_TEMP=`/usr/bin/sensors|grep "Core.*${mytemp} C"|awk '{ print $5,$6,$7,$8}'|sort -u`
	echo "Temp: $mytemp C $MAX_TEMP, CPU Cores: ${CPU_CORES}"
done
