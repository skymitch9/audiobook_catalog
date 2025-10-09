# app/core/keys.py

# MP4/iTunes atoms
K_TITLE  = "\xa9nam"   # Title
K_ARTIST = "\xa9ART"   # Author(s)
K_WRITER = "\xa9wrt"   # Narrator
K_DAY    = "\xa9day"   # Year/Date
K_GENRE  = "\xa9gen"   # Genre

# Vendor atoms (Audible-style)
K_SERIES_VENDOR = "SRNM"  # Series Name
K_INDEX_VENDOR  = "SRSQ"  # Series Sequence (e.g., 2.1)

# Free-form keys (----:com.apple.iTunes:*)
FREEFORM_HINTS = {
    "series": ["series", "book series", "audible:series", "audible:seriesname"],
    "series_index": ["series index", "series_index", "audible:seriessequence", "series number", "series_no"],
}
