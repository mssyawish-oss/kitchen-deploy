#!/bin/bash
# Double-click this after editing the dashboard on the Mac.
# It copies your latest dashboard files into the repo and pushes them to GitHub;
# the shop server pulls them automatically within a few minutes.
cd "$(dirname "$0")" || exit 1
cp ~/Desktop/dashboard_app.py ~/Desktop/dashboard_ui.html ./ 2>/dev/null
git add dashboard_app.py dashboard_ui.html
if git diff --cached --quiet; then
  echo "Nothing changed since last publish."
else
  git commit -m "Update $(date '+%Y-%m-%d %H:%M')" >/dev/null
  if git push; then
    echo ""
    echo "Published. The shop server will pick it up within ~3 minutes."
    echo "(Screen changes go live automatically; backend changes apply on the next restart.)"
  else
    echo ""
    echo "PUSH FAILED. Check your internet, or sign in again with:  gh auth login"
  fi
fi
echo ""
read -n1 -s -p "Press any key to close."
echo
