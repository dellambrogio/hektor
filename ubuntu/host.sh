# Sourced by deploy.sh and claude-token.sh, from the repo root: which machine is Hektor.

# setting NAME: $NAME from the environment, else from deploy.conf; stops the script if it's in neither
setting() {
    local v=${!1}
    [[ $v ]] || v=$(sed -n "s/^$1=//p" deploy.conf 2>/dev/null | tail -1)
    [[ $v ]] || { echo "no $1: copy deploy.conf.example to deploy.conf and fill it in" >&2; exit 1; }
    echo "$v"
}

# host [ARG]: ARG if given, else $HEKTOR_HOST
host() {
    if [[ $1 ]]; then echo "$1"; else setting HEKTOR_HOST; fi
}
