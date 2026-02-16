# Waits for system to become online
function wait_system_online() {
    while getopts ":t:h:p:" o; do
        case "${o}" in
            t)
                TIMEOUT=${OPTARG}
                ;;
            h)
                NC_HOST=${OPTARG}
                ;;
            p)
                NC_PORT=${OPTARG}
                ;;
            *)
                echo "Usage $0 [-t <timeout>] [-h <hostname>] [-p <port>]" 1>&2; exit 1;
                ;;
        esac;
    done;
    shift $((OPTIND-1));

    # Sanity check
    if [ -z "${TIMEOUT}" ] || [ -z "${NC_HOST}" ] || [ -z "${NC_PORT}" ]; then
      exit 1;
    fi;

    # Main loop
    until nc ${NC_HOST} ${NC_PORT} -w 1 || [ ${TIMEOUT} -eq 0 ];
    do
      echo "System ${NC_HOST} not yet ready, sleeping... (${TIMEOUT} attempts remaining)";
      sleep 2;
      TIMEOUT=$(( ${TIMEOUT} - 1 ));
    done;

    # Prevents 'system is booting'
    sleep 0.5

    if [ ${TIMEOUT} -eq 0 ]; then exit 1 ; fi;
}

# Waits for system to become offline
function wait_system_offline() {
    while getopts ":t:h:p:" o; do
        case "${o}" in
            t)
                TIMEOUT=${OPTARG}
                ;;
            h)
                NC_HOST=${OPTARG}
                ;;
            p)
                NC_PORT=${OPTARG}
                ;;
            *)
                echo "Usage $0 [-t <timeout>] [-h <hostname>] [-p <port>]" 1>&2; exit 1;
                ;;
        esac;
    done;
    shift $((OPTIND-1));

    # Sanity check
    if [ -z "${TIMEOUT}" ] || [ -z "${NC_HOST}" ] || [ -z "${NC_PORT}" ]; then
      exit 1;
    fi;

    # Main loop
    until ! nc ${NC_HOST} ${NC_PORT} -w 1 || [ ${TIMEOUT} -eq 0 ];
    do
      echo "System ${NC_HOST} not yet ready, sleeping... (${TIMEOUT} attempts remaining)";
      sleep 2;
      TIMEOUT=$(( ${TIMEOUT} - 1 ));
    done;
    if [ ${TIMEOUT} -eq 0 ]; then exit 1 ; fi;
}

# Waits for libvirtd to report active
function wait_libvirtd_active() {
    while getopts ":t:h:" o; do
        case "${o}" in
            t)
                TIMEOUT=${OPTARG}
                ;;
            h)
                HVM=${OPTARG}
                ;;
            *)
                echo "Usage $0 [-t <timeout>] [-h <hostname>]" 1>&2; exit 1;
                ;;
        esac;
    done;
    shift $((OPTIND-1));

    # Sanity check
    if [ -z "${TIMEOUT}" ] || [ -z "${HVM}" ]; then
      exit 1;
    fi;

    # main loop
    until ssh -q -t ${SSH_USER}@${HVM} /usr/bin/systemctl -q is-active libvirtd || [ $TIMEOUT -eq 0 ];
    do
        echo "Libvirtd on ${HVM} not yet ready, sleeping... (${TIMEOUT} attempts remaining)";
        sleep 2;
        TIMEOUT=$(( $TIMEOUT - 1 ));
    done;
    if [ ${TIMEOUT} -eq 0 ]; then exit 1 ; fi;
}

# Waits for vcs to report RUNNING
function wait_vcs_running() {
    while getopts ":t:h:" o; do
        case "${o}" in
            t)
                TIMEOUT=${OPTARG}
                ;;
            h)
                HVM=${OPTARG}
                ;;
            *)
                echo "Usage $0 [-t <timeout>] [-h <hostname>]" 1>&2; exit 1;
                ;;
        esac;
    done;
    shift $((OPTIND-1));

    # Sanity check
    if [ -z "${TIMEOUT}" ] || [ -z "${HVM}" ]; then
      exit 1;
    fi;

    # main loop
    until ssh -q -t ${SSH_USER}@${HVM} sudo /opt/VRTSvcs/bin/haclus -state -localclus|grep -q RUNNING || [ $TIMEOUT -eq 0 ];
    do
        echo "VCS Cluster on ${HVM} not yet RUNNING, sleeping... (${TIMEOUT} attempts remaining)";
        sleep 2;
        TIMEOUT=$(( $TIMEOUT - 1 ));
    done;
    if [ ${TIMEOUT} -eq 0 ]; then exit 1 ; fi;
}

# Waits for docker to report active
function wait_docker_active() {
    while getopts ":t:h:" o; do
        case "${o}" in
            t)
                TIMEOUT=${OPTARG}
                ;;
            h)
                HVM=${OPTARG}
                ;;
            *)
                echo "Usage $0 [-t <timeout>] [-h <hostname>]" 1>&2; exit 1;
                ;;
        esac;
    done;
    shift $((OPTIND-1));

    # Sanity check
    if [ -z "${TIMEOUT}" ] || [ -z "${HVM}" ]; then
      exit 1;
    fi;

    # main loop
    until ssh -q -t ${SSH_USER}@${HVM} /usr/bin/systemctl -q is-active docker || [ $TIMEOUT -eq 0 ];
    do
        echo "Docker daemon on ${HVM} not yet ready, sleeping... (${TIMEOUT} attempts remaining)";
        sleep 2;
        TIMEOUT=$(( $TIMEOUT - 1 ));
    done;
    if [ ${TIMEOUT} -eq 0 ]; then exit 1 ; fi;
}

# wait for logfile to show up and then tail -f it...
function wait_and_tail_log() {
    max_tail_timeout=3000s
    while getopts ":t:h:f:" o; do
        case "${o}" in
            t)
                TIMEOUT=${OPTARG}
                ;;
            h)
                HVM=${OPTARG}
                ;;
            f)
                LOGFILE=${OPTARG}
                ;;
            *)
                echo "Usage $0 [-t <timeout>] [-h <hostname>] [-f <logfile>]" 1>&2; exit 1;
                ;;
        esac;
    done;
    shift $((OPTIND-1));

    # Sanity check
    if [ -z "${TIMEOUT}" ] || [ -z "${HVM}" ]; then
      exit 1;
    fi;

    # main loop
    until ssh -q -t ${SSH_USER}@${HVM} test -f ${LOGFILE} || [ $TIMEOUT -eq 0 ];
    do
        echo "Logfile ${LOGFILE} on ${HVM} not yet ready, sleeping... (${TIMEOUT} attempts remaining)";
        sleep 2;
        TIMEOUT=$(( $TIMEOUT - 1 ));
    done;
    if [ ${TIMEOUT} -eq 0 ]; then exit 1 ; fi;
    #
    echo "Logfile ready, tailing for ${max_tail_timeout}..."
    ssh -q -t ${SSH_USER}@${HVM} timeout ${max_tail_timeout} tail -f ${LOGFILE} || /bin/true
    # Override exit code
    exit 0
}

# Waits for ironic to report active
function wait_ironic_active() {
    while getopts ":t:h:p:" o; do
        case "${o}" in
            t)
                TIMEOUT=${OPTARG}
                ;;
            h)
                INSTACK_DNS=${OPTARG}
                ;;
            *)
                echo "Usage $0 [-t <timeout>] [-h <hostname>] [-p <port>]" 1>&2; exit 1;
                ;;
        esac;
    done;
    shift $((OPTIND-1));

    # Sanity check
    if [ -z "${TIMEOUT}" ] || [ -z "${INSTACK_DNS}" ]; then
      exit 1;
    fi;

    case $(ssh -q -t stack@${INSTACK_DNS} uname -r|cut -d- -f1) in
       3.10.0)
           mysvc="openstack-ironic-conductor"
           ;;
       4.18.0)
           mysvc="tripleo_ironic_inspector"
           ;;
       *)
           mysvc="openstack-ironic-conductor"
           ;;
    esac;
    # main loop
    until ssh -q -t stack@${INSTACK_DNS} /usr/bin/systemctl -q is-active ${mysvc} || [ $TIMEOUT -eq 0 ];
    do
        echo "${mysvc} on ${INSTACK_DNS} not yet ready, sleeping... (${TIMEOUT} attempts remaining)";
        sleep 2;
        TIMEOUT=$(( $TIMEOUT - 1 ));
    done;
    if [ ${TIMEOUT} -eq 0 ]; then exit 1 ; fi;
}

# Load redis keyvalue pairs
function load_redis_keys() {
    unset REDIS_HVM_FAQ REDIS_OSP_FAQ REDIS_SIZE_FAQ REDIS_SNAP_FAQ REDIS_INSTACK_GUEST_FAQ REDIS_INSTACK_DNS_FAQ
    unset REDIS_INSTACK_RHEL_FAQ REDIS_KEEP_FAQ REDIS_REBUILD_FAQ REDIS_NEUTRON_DRIVER_FAQ REDIS_OCP_FAQ REDIS_SDN_DRIVER_FAQ
    REDIS_CMD="redis-cli --no-auth-warning --raw -h ${REDIS_HOST} -p ${REDIS_PORT}"
    # Hypervisor
    REDIS_HVM_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_HVM)"
    if [ ! -z "${REDIS_HVM_FAQ}" ]; then
        export HVM="${REDIS_HVM_FAQ}" ;
      else
        if [ ! -z "${HVM}" ]; then
          ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_HVM "${HVM}" ;
        fi ;
    fi
    # OSP version
    REDIS_OSP_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_OSP)"
    if [ ! -z "${REDIS_OSP_FAQ}" ]; then
        export OSP="${REDIS_OSP_FAQ}";
        export RHEL="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_INSTACK_RHEL)";
      else
        if [ ! -z "${OSP}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_OSP "${OSP}" ; fi ;
    fi
    # Deploy size
    REDIS_SIZE_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_SIZE)"
    if [ ! -z "${REDIS_SIZE_FAQ}" ]; then
        export SIZE="${REDIS_SIZE_FAQ}";
      else
        if [ ! -z "${SIZE}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_SIZE "${SIZE}" ; fi ;
    fi
    # Restore snapshots ?
    REDIS_SNAP_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_SNAP)"
    if [ ! -z "${REDIS_SNAP_FAQ}" ]; then
        export SNAP="${REDIS_SNAP_FAQ}";
      else
        if [ ! -z "${SNAP}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_SNAP "${SNAP}" ; fi ;
    fi
    # Instack VM guest
    REDIS_INSTACK_GUEST_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_INSTACK_GUEST)"
    if [ ! -z "${REDIS_INSTACK_GUEST_FAQ}" ]; then
        export INSTACK_GUEST="${REDIS_INSTACK_GUEST_FAQ}";
      else
        if [ ! -z "${INSTACK_GUEST}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_INSTACK_GUEST "${INSTACK_GUEST}" ; fi ;
    fi
    # Instack VM Hostname
    REDIS_INSTACK_DNS_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_INSTACK_DNS)"
    if [ ! -z "${REDIS_INSTACK_DNS_FAQ}" ]; then
        export INSTACK_DNS="${REDIS_INSTACK_DNS_FAQ}";
      else
        if [ ! -z "${INSTACK_DNS}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_INSTACK_DNS "${INSTACK_DNS}" ; fi ;
    fi
    # Rebuild undercloud VM?
    REDIS_REBUILD_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_REBUILD)"
    if [ ! -z "${REDIS_REBUILD_FAQ}" ]; then
        export REBUILD="${REDIS_REBUILD_FAQ}";
      else
        if [ ! -z "${REBUILD}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_REBUILD "${REBUILD}" ; fi ;
    fi
    # RHEL Version ( 7 or 8 )
    REDIS_INSTACK_RHEL_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_INSTACK_RHEL)"
    if [ ! -z "${REDIS_INSTACK_RHEL_FAQ}" ]; then
        export INSTACK_RHEL="${REDIS_INSTACK_RHEL_FAQ}";
      else
        if [ ! -z "${INSTACK_RHEL}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_INSTACK_RHEL "${INSTACK_RHEL}" ; fi ;
    fi
    # KEEP environment ( 'yes' or 'no' )
    REDIS_KEEP_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_KEEP)"
    if [ ! -z "${REDIS_KEEP_FAQ}" ]; then
        export KEEP="${REDIS_KEEP_FAQ}";
      else
        if [ ! -z "${KEEP}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_KEEP "${KEEP}" ; fi ;
    fi
    # Neutron Driver ( ovs or ovn )
    REDIS_NEUTRON_DRIVER_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_NEUTRON_DRIVER)"
    if [ ! -z "${REDIS_NEUTRON_DRIVER_FAQ}" ]; then
        export NEUTRON_DRIVER="${REDIS_NEUTRON_DRIVER_FAQ}";
      else
        if [ ! -z "${NEUTRON_DRIVER}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_NEUTRON_DRIVER "${NEUTRON_DRIVER}" ; fi ;
    fi
    # Neutron Driver ( ovs or ovn )
    REDIS_OCP_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_OCP)"
    if [ ! -z "${REDIS_OCP_FAQ}" ]; then
        export OCP="${REDIS_OCP_FAQ}";
      else
        if [ ! -z "${OCP}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_OCP "${OCP}" ; fi ;
    fi
    # SDN Driver ( ovs or ovn )
    REDIS_SDN_DRIVER_FAQ="$(${REDIS_CMD} get KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_SDN_DRIVER)"
    if [ ! -z "${REDIS_SDN_DRIVER_FAQ}" ]; then
        export SDN_DRIVER="${REDIS_SDN_DRIVER_FAQ}";
      else
        if [ ! -z "${SDN_DRIVER}" ]; then ${REDIS_CMD} set KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_SDN_DRIVER "${SDN_DRIVER}" ; fi ;
    fi
}


# Purge redis keyvalue pairs
function purge_redis_keys() {
    # Clean redis (disabled 20211101)
    if [[ 0 -eq 1 ]]; then
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_HVM"
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_OSP"
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_SIZE"
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_SNAP"
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_KEEP"
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_INSTACK_DNS"
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_INSTACK_GUEST"
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_NEUTRON_DRIVER"
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_OCP"
        ${REDIS_CMD} del "KRYNN_${CI_PROJECT_ID}_${CI_PIPELINE_ID}_SDN_DRIVER"
    fi
}

# Print banners
function print_banner_rhosp() {
    echo "############################################";
    echo "# Will execute on HVM = ${HVM}";
    echo "# RHOSP version = v.${OSP}";
    echo "# size = ${SIZE}";
    echo "# guest = ${INSTACK_GUEST}, RHEL = RHELv${RHEL}, dns hostname = ${INSTACK_DNS}";
    echo "# neutron driver = ${NEUTRON_DRIVER}";
    echo "# Restore undercloud snapshot = ${SNAP}";
    echo "# Keep overcloud nodes after deploy = ${KEEP}";
    echo "############################################";
}

# Push cluster credentials to imladris as soon as they appear (runs in background)
function push_credentials_async() {
    local OPTIND OPTARG
    local DIGIT="" REMOTE_USER="raistlin" REMOTE_HOST="imladris.lasthome.solace.krynn"
    local TIMEOUT=3600

    while getopts ":d:t:" o; do
        case "${o}" in
            d)
                DIGIT=${OPTARG}
                ;;
            t)
                TIMEOUT=${OPTARG}
                ;;
            *)
                echo "Usage: push_credentials_async -d <digit> [-t <timeout>]" 1>&2; return 1;
                ;;
        esac;
    done;
    shift $((OPTIND-1));

    # Sanity check
    if [ -z "${DIGIT}" ]; then
        echo "ERROR: push_credentials_async requires -d <digit>" 1>&2
        return 1
    fi

    local CLUSTER_AUTH="${HOME}/.kcli/clusters/ocp4${DIGIT}/auth"
    local REMOTE_AUTH="${REMOTE_USER}@${REMOTE_HOST}:/export/home/${REMOTE_USER}/.kcli/clusters/ocp4${DIGIT}/auth"

    (
        echo "Waiting for cluster credentials (ocp4${DIGIT}) to appear ..."
        while [[ ! -f ${CLUSTER_AUTH}/kubeconfig ]] && [[ ${TIMEOUT} -gt 0 ]]; do
            sleep 10
            TIMEOUT=$(( TIMEOUT - 10 ))
        done
        if [[ -f ${CLUSTER_AUTH}/kubeconfig ]]; then
            echo "Credentials found, pushing to ${REMOTE_HOST} ..."
            ssh ${REMOTE_USER}@${REMOTE_HOST} \
                "mkdir -p /export/home/${REMOTE_USER}/.kcli/clusters/ocp4${DIGIT}/auth" 2>/dev/null
            scp -q ${CLUSTER_AUTH}/kubeconfig ${REMOTE_AUTH}/kubeconfig && \
                echo "  kubeconfig pushed to ${REMOTE_HOST}"
            scp -q ${CLUSTER_AUTH}/kubeadmin-password ${REMOTE_AUTH}/kubeadmin-password 2>/dev/null && \
                echo "  kubeadmin-password pushed to ${REMOTE_HOST}"
        else
            echo "WARNING: Timed out waiting for credentials (ocp4${DIGIT})"
        fi
    ) &
}

function print_banner_ocp() {
    echo "############################################";
    echo "# Will execute on HVM = ${HVM}";
    echo "# OCP version = v${OCP}";
    echo "# size = ${SIZE}";
    echo "# SDN driver = ${SDN_DRIVER}";
    echo "# Keep OCP nodes after deploy = ${KEEP}";
    echo "############################################";
}

