#!/bin/sh
# $Id: FixPerms.sh,v 1.1 2006/11/27 10:17:55 root Exp $
#
CHMOD_CMD=gchmod
CHOWN_CMD=gchown

if [ "$1" = "" ]; then
	echo "Using \".\" as argument."
	${CHOWN_CMD} root:root -R .
	${CHMOD_CMD} -R a+Xr,og-w .
else
	${CHOWN_CMD} root:root -R $*
	${CHMOD_CMD} -R a+Xr,og-w $*
fi
