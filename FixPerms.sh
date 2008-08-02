#!/bin/sh
# $Id: FixPerms.sh,v 1.2 2008/08/02 17:57:46 root Exp $
#
CHMOD_CMD=gchmod
CHOWN_CMD=gchown

if [ "$1" = "" ]; then
	echo "Using \".\" as argument."
	${CHOWN_CMD} root:root -R .
	${CHMOD_CMD} -R a+Xr,og-w,u+w .
else
	${CHOWN_CMD} root:root -R $*
	${CHMOD_CMD} -R a+Xr,og-w,u+w $*
fi
