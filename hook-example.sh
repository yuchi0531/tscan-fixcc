#!/bin/bash
# Example hook: run fix_cc.py before handing a recording to Amatsukaze.
#
# EpgTimer (and similar recorders) can call a batch file after a recording
# finishes, passing the recorded TS path. Wire this script in there:
#
#   [REC_DEF] BatFilePath=/path/to/amatsukaze-addtask.sh
#
# Set FIX_CC below to wherever you installed fix_cc.py.
#
# fix_cc.py --auto exits 0 when it repaired something and 3 when there was
# nothing to do. Any other status is an error; the recording is still queued
# so a repair failure never costs you the file.

set -u

FIX_CC="/usr/local/bin/fix_cc.py"

file="${FilePath:-${1:-}}"
if [ -z "$file" ] || [ ! -f "$file" ]; then
    echo "amatsukaze-addtask: no such file: ${file:-<empty>}" >&2
    exit 1
fi

tmp="${file}.ccfix.tmp"
python3 "$FIX_CC" --auto "$file" "$tmp"
rc=$?

case $rc in
    0)
        # Repaired: replace the original, keeping its ownership and mode.
        chmod --reference="$file" "$tmp" 2>/dev/null
        chown --reference="$file" "$tmp" 2>/dev/null
        mv -f "$tmp" "$file"
        echo "amatsukaze-addtask: repaired continuity_counter in $file" >&2
        ;;
    3)
        # Nothing to repair: fix_cc.py created no output file.
        rm -f "$tmp"
        ;;
    *)
        rm -f "$tmp"
        echo "amatsukaze-addtask: fix_cc.py failed (rc=$rc); queueing as-is" >&2
        ;;
esac

exec AmatsukazeAddTask \
    -f "$file" \
    -ip 127.0.0.1 -p 32768 \
    -o "/path/to/encoded" \
    -s "nvenc-h265-tsreplace" \
    --priority 0 --no-move
