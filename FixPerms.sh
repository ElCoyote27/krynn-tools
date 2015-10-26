#!/bin/sh
# $Id: FixPerms.sh,v 1.3 2012/01/04 18:12:45 root Exp $
#
CHMOD_CMD=gchmod
CHOWN_CMD=gchown

if [ "$1" = "" ]; then
	echo "Using \".\" as argument."
	${CHOWN_CMD} root:root -R .
	${CHMOD_CMD} -R a+Xr,og-w,u+w .
else
	${CHOWN_CMD} root:root -R "$@"
	${CHMOD_CMD} -R a+Xr,og-w,u+w "$@"
fi
