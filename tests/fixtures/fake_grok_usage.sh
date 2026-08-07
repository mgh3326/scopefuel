#!/bin/sh
printf '╭────────╮\r\n│ ❯      │\r\n╰────────╯\r\n'
printf 'Grok\r\nShift+Tab:mode │ Ctrl+.:shortcuts\r\n'
IFS= read -r command
[ "$command" = '/usage' ] || exit 9
printf 'Session usage is unavailable until the session starts.\r\n'
printf 'Weekly limit: 37%%\r\nNext reset: August 14, 04:58\r\n'
