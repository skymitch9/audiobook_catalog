# app/core/index_utils.py
from typing import Optional
import re

_WORD_NUM = {
    "one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
    "eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,
    "eighteen":18,"nineteen":19,"twenty":20,"thirty":30
}
_ROMAN_MAP = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}

def _word_to_num(tok: str) -> Optional[int]:
    t = tok.lower().strip().replace("-", " ")
    if t in _WORD_NUM: return _WORD_NUM[t]
    parts = t.split()
    if len(parts) == 2 and parts[0] in _WORD_NUM and parts[1] in _WORD_NUM and _WORD_NUM[parts[0]] % 10 == 0:
        return _WORD_NUM[parts[0]] + _WORD_NUM[parts[1]]
    return None

def _roman_to_int(s: str) -> Optional[int]:
    s = s.upper()
    if not s or not all(c in _ROMAN_MAP for c in s): return None
    total, prev = 0, 0
    for ch in reversed(s):
        val = _ROMAN_MAP[ch]
        if val < prev: total -= val
        else: total += val; prev = val
    return total

def normalize_index(tok: str) -> str:
    if not tok: return ""
    t = tok.strip()
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*$", t)
    if m: return f"{m.group(1)}-{m.group(2)}"
    if re.fullmatch(r"\d+(?:\.\d+)?", t): return t
    r = _roman_to_int(t)
    if r is not None: return str(r)
    w = _word_to_num(t)
    if w is not None: return str(w)
    return t

def sort_key_for_index(display_val: str) -> Optional[float]:
    if not display_val: return None
    s = display_val.strip()
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*$", s)
    if m:
        try: return float(m.group(1))
        except Exception: return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        try: return float(s)
        except Exception: return None
    from_val = _roman_to_int(s)
    if from_val is not None: return float(from_val)
    w = _word_to_num(s)
    if w is not None: return float(w)
    return None
