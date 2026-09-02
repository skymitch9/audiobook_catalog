"""The audiobook catalogue package.

⚠️ ONE IMPORT SIDE EFFECT, AND IT IS DELIBERATE (KI-3).

`force_utf8_stdio()` makes this interpreter's stdout/stderr UTF-8 with
`errors="replace"`, so a `print()` of a non-ASCII book or author name cannot
kill a run on a cp1252 console. It lives here rather than in each script
because the class of bug is "any print, in any entry point, of any library
data" — four incidents, the last one on an author named 猫子 in a HAND-RUN
script that no `.bat` wrapper is on the path of.

It is a no-op when the stream is already UTF-8 (a wrapper set
`PYTHONIOENCODING`) or cannot be reconfigured (pytest's capture objects), and
it never raises. Full reasoning: `app/core/console.py`.
"""

from app.core.console import force_utf8_stdio

force_utf8_stdio()
