#!/bin/bash
# Starts the Arkenfall client from its own folder, wherever the folder happens to be.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec ./arkenfall "$@"
