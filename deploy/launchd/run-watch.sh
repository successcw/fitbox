#!/bin/sh
set -eu
# config.env 是用户自己维护的 shell 赋值文件，不能加载不可信下载文件。
export FITBOX_CONFIG='__HOME__/.config/fitbox/config.env'
set -a
. "__HOME__/.config/fitbox/config.env"
set +a
exec "__PYTHON__" "__PROJECT__/fitbox/watch.py"
