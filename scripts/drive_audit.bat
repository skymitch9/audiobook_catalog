@echo off
rem Weekly Drive duplicate audit (Task Scheduler: "AudiobookDriveAudit").
rem Report lands in docs\DRIVE_AUDIT_REPORT.md; console output in
rem output_files\drive_audit.log.
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
echo ================= %date% %time% ================= >> output_files\drive_audit.log
python scripts\drive_audit.py >> output_files\drive_audit.log 2>&1
