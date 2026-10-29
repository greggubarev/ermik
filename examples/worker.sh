#!/bin/sh
set -eu

# A deterministic demo task. The controller mounts the checkout at /work and
# the selected issue at /task/issue.md. This script reads the issue but has no
# GitHub credential or publication responsibility.
test -s /task/issue.md
grep -Fq 'src/example.py' /task/issue.md
cd /work
mkdir -p src
if grep -Fq '## Review correction' /task/issue.md; then
  printf '%s\n' 'def greeting():' \
    '    """Return the example greeting."""' \
    '    return "Hello from the example project"' > src/example.py
else
  printf '%s\n' 'def greeting():' \
    '    return "Hello from the example project"' > src/example.py
fi
