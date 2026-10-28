#!/bin/zsh
set -u
SCRIPT="${0:A:h}/apply_storage_rules.py"
if [[ ! -f "$SCRIPT" ]]; then
  SCRIPT="${0:A:h:h}/tools/apply_storage_rules.py"
fi
print 'Model Monitor — Apply storage rules'
print 'The managed proxy will briefly restart on the same ports. Existing history and backups are preserved.'
/usr/bin/python3 "$SCRIPT"
RESULT=$?
print ''
if [[ $RESULT -ne 0 ]]; then
  print 'The update needs attention. Read the message above before retrying.'
fi
read -r 'REPLY?Press Return to close this window.'
exit $RESULT
