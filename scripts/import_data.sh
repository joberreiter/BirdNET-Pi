#!/usr/bin/env bash
# Import BirdNET data

my_dir=$HOME/BirdNET-Pi/scripts

if [ "$EUID" == 0 ]
  then echo "Please run as a non-root user."
  exit
fi

usage() { echo "Usage: $0 -h <old_home>" 1>&2; exit 1; }

unset -v OLD_HOME
while getopts ":h:" o; do
  case "${o}" in
    h)
      OLD_HOME=$(echo ${OPTARG} | sed 's/\/$//')
      ;;
    *)
      usage
      ;;
  esac
done

[ -z "$OLD_HOME" ] && usage && exit 1
[ "$HOME" == "$OLD_HOME" ] && echo "Source and destination are the same" && exit 1
[ ! -d "${OLD_HOME}/BirdNET-Pi" ] || echo "${OLD_HOME}/BirdNET-Pi" not found && exit 1
[ -d "${OLD_HOME}/BirdSongs" ] || echo "${OLD_HOME}/BirdSongs" not found && exit 1

PHP_SERVICE=$(systemctl list-unit-files -t service --output json --no-pager php*-fpm.service | jq --raw-output '.[0].unit_file')
[ -z "$PHP_SERVICE" ] || [ "$PHP_SERVICE" == 'null' ] && echo "Could not determine the php service name, this is most likely a bug." && exit 1

"$my_dir/stop_core_services.sh"
dirs=("BirdSongs/Extracted/By_Date"
"BirdSongs/Extracted/Charts")
dbs=("BirdNET-Pi/BirdDB.txt"
"BirdNET-Pi/scripts/birds.db")

set -x # Debugging

echo "Starting data copy"
# these will take the bulk of the time, so do them first
for dir in  "${dirs[@]}";do
  cp -a -v "${OLD_HOME}/${dir}" "${HOME}/${dir}"
done

sudo systemctl stop "$PHP_SERVICE"
for db in  "${dbs[@]}";do
  cp -a -v "${OLD_HOME}/${db}" "${HOME}/${db}"
done
echo "Data copy done"

sudo systemctl restart "$PHP_SERVICE"
sudo systemctl restart caddy.service
"$my_dir/restart_services.sh"
