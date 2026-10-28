#!/bin/zsh
set -u
SCRIPT="${0:A:h}/cleanup_storage.py"
if [[ ! -f "$SCRIPT" ]]; then
  SCRIPT="${0:A:h:h}/tools/cleanup_storage.py"
fi
print 'Model Monitor — Clean stored traffic'
print 'Stop the proxy in Model Monitor before continuing.'
print 'This keeps Astra/auto-review summaries, preserves other models in full, and removes only verified duplicate recovery copies and regenerable packet previews.'
print 'Model counts, timestamps, settings and certificates are preserved.'
/usr/bin/python3 "$SCRIPT"
RESULT=$?
print ''
if [[ $RESULT -ne 0 ]]; then
  print 'Cleanup needs attention. Read the message above; do not delete files manually.'
else
  print 'Cleanup completed. New archives use lossless compression.'
fi
read -r 'REPLY?Press Return to close this window.'
exit $RESULT
