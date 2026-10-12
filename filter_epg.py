"""Download the big EPG, keep only my channels, drop old programmes, write epg.xml.gz"""
import gzip, sys, urllib.request, datetime as dt
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

SRC = "https://epg.team/3.1.xml"
OUT = "epg.xml.gz"
KEEP_PAST_HOURS = 3      # programmes that ended more than this long ago are dropped
MAX_DAYS_AHEAD = 14

def load_ids(path="channels.txt"):
    return {l.strip() for l in open(path, encoding="utf-8") if l.strip()}

def parse_time(s):
    # XMLTV: 20261011180000 +0300
    s = s.strip()
    base = dt.datetime.strptime(s[:14], "%Y%m%d%H%M%S")
    tz = dt.timezone.utc
    if len(s) > 14:
        off = s[14:].strip()
        if off and off[0] in "+-" and len(off) >= 5:
            sign = 1 if off[0] == "+" else -1
            tz = dt.timezone(sign * dt.timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
    return base.replace(tzinfo=tz)

def open_source(src):
    if src.startswith("http"):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=300)
    else:
        raw = open(src, "rb")
    head = raw.read(2)
    class Chain:
        def __init__(self, first, rest): self.first, self.rest = first, rest
        def read(self, n=-1):
            if self.first:
                d, self.first = self.first, b""
                return d + (self.rest.read(n - len(d)) if n and n > 0 else self.rest.read())
            return self.rest.read(n)
    chained = Chain(head, raw)
    return gzip.GzipFile(fileobj=chained) if head == b"\x1f\x8b" else chained

def main(src=SRC, out=OUT, ids_file="channels.txt", now=None):
    ids = load_ids(ids_file)
    now = now or dt.datetime.now(dt.timezone.utc)
    lo = now - dt.timedelta(hours=KEEP_PAST_HOURS)
    hi = now + dt.timedelta(days=MAX_DAYS_AHEAD)
    kept_ch = kept_pr = seen_pr = 0
    ctx = ET.iterparse(open_source(src), events=("start", "end"))
    _, root = next(ctx)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="my-filtered-epg">\n')
        for ev, el in ctx:
            if ev != "end" or el.tag not in ("channel", "programme"):
                continue
            if el.tag == "channel":
                if el.get("id") in ids:
                    f.write(ET.tostring(el, encoding="unicode")); kept_ch += 1
            else:
                seen_pr += 1
                if el.get("channel") in ids:
                    try:
                        if parse_time(el.get("stop") or el.get("start")) >= lo and parse_time(el.get("start")) <= hi:
                            f.write(ET.tostring(el, encoding="unicode")); kept_pr += 1
                    except Exception:
                        pass
            root.clear()
        f.write("</tv>\n")
    print(f"channels kept: {kept_ch} / ids wanted: {len(ids)} | programmes kept: {kept_pr} of {seen_pr}")
    if kept_pr == 0:
        sys.exit("ERROR: no programmes kept - not publishing an empty EPG")

if __name__ == "__main__":
    main(*(sys.argv[1:2] or []))
