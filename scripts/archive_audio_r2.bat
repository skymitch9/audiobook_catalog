@echo off
rem DISASTER-RECOVERY ARCHIVE of the audiobook library into Cloudflare R2.
rem Task Scheduler: "AudiobookArchiveR2", HOURLY, via archive_audio_r2_hidden.vbs.
rem
rem Owner's order, 2026-08-18: "do it, setup blob storage for all author folders.
rem we lose this data we lose it all and the server isnt ready yet."
rem
rem WHY HOURLY FOR A JOB THAT TAKES DAYS.
rem The seed is ~685 GB over a household uplink -- days of upload. A single
rem "run it once and hope" would be lost to the first reboot, network drop or
rem killed process, and nothing would restart it. Instead the task is DUMB and
rem fires every hour; the script is idempotent (manifest-driven skip on
rem size+mtime) and single-flight (output_files\audio_archive.lock, reclaimed
rem automatically when its holder pid is dead). So:
rem   * while the seed is running, each hourly fire finds the lock held by a
rem     LIVE pid, prints one line and exits 0 within seconds;
rem   * if the seed died for any reason, the next fire finds a dead holder,
rem     reclaims the lock and picks up exactly where the manifest left off;
rem   * once the seed is finished, the same task becomes the ONGOING SYNC that
rem     carries newly-purchased books off-site within the hour.
rem Self-healing, survives reboots, needs nobody watching it.
rem
rem !! archive/ IS NOT A CACHE. Everything this writes goes to
rem estate-audio under the "archive/" prefix and NOTHING MAY EVER EVICT IT --
rem see the script's module docstring and docs\access\AUDIO_ARCHIVE.md.
rem
rem !! BANDWIDTH ONLY. No GPU, no ffmpeg, no Whisper. It is safe to run beside
rem the transcription chain and the 00:00-08:00 Phoenix ingest window; it takes
rem uplink from them and nothing else. It also does NOT touch the pipeline lock
rem (output_files\pipeline.lock) -- it has its own, so an archive run and a
rem catalog run never block each other.
cd /d "C:\Users\nbasl\OneDrive\Documents\vs-code-repos\bookbuddy\audiobook_catalog"
set PYTHONIOENCODING=utf-8
echo ================= %date% %time% ================= >> output_files\audio_archive.log
python -m scripts.archive_audio_r2 --commit >> output_files\audio_archive.log 2>&1
exit /b %ERRORLEVEL%
