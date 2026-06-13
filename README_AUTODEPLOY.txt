==========================================================
 AUTO-DEPLOY: edit on the Mac -> shop server updates itself
 Updated 14 June 2026
==========================================================

HOW IT WORKS (pull model, works even when the Mac is off):
  1. You edit the dashboard on the Mac.
  2. You double-click  publish.command  -> it pushes the code to GitHub.
  3. The shop server checks GitHub every ~3 minutes and pulls the update
     in automatically -- after validating it and backing up the old files.

  - Screen changes (dashboard_ui.html): go live on the next page refresh,
    no restart.
  - Backend changes (dashboard_app.py): downloaded automatically, but only
    take effect after the dashboard is restarted or the PC reboots. (A file
    called BACKEND-UPDATE-PENDING.txt appears in C:\KitchenDashboard when
    one is waiting.) This is deliberate so an auto-update can never crash
    the live shop screen.
  - Your data/secrets (kitchen_data.json) are NEVER synced -- they stay
    only on the server.

----------------------------------------------------------
ONE-TIME SETUP
----------------------------------------------------------
ON THE MAC (done with help, once):
  - GitHub account + sign in:   gh auth login
  - Create the repo + first push (Claude does this for you).

ON THE SHOP SERVER PC (once, must be at the PC):
  1. Copy  setup-autodeploy.bat  onto the PC (e.g. to the Desktop).
  2. Right-click it > "Run as administrator".
  3. It installs git (if needed), clones the deploy folder to
     C:\KitchenDashboard-sync, and registers the 3-minute auto task.
  That's it -- never needed again.

----------------------------------------------------------
EVERYDAY USE (after setup)
----------------------------------------------------------
  1. Edit dashboard_ui.html / dashboard_app.py on the Mac (on the Desktop).
  2. Double-click  publish.command .
  3. Wait a few minutes; refresh the dashboard on the tablet/phone.
  For a backend change: also restart the dashboard on the PC once
  (or it applies on the next reboot).

----------------------------------------------------------
SAFETY / ROLLBACK
----------------------------------------------------------
  - Every update backs up the previous files to C:\KitchenDashboard\backup\.
  - A bad UI file (truncated/empty) or an app.py that doesn't compile is
    REJECTED automatically -- the live files are left untouched.
  - To roll back: copy the files from the newest backup\ folder back into
    C:\KitchenDashboard (or revert the change on the Mac and publish again).
  - Activity is logged to  C:\KitchenDashboard\autodeploy.log .
==========================================================
