"""Check how many days ahead each candidate EPG source really covers. Writes probe.txt"""
import gzip, sys, time, urllib.request, datetime as dt
import xml.etree.ElementTree as ET

SOURCES = [
    "https://epg.team/3.1.xml",
    "http://iptv-content.rv77.pw/epg.xml.gz",
    "http://epg.one/epg.xml.gz",
    "https://iptv.pm/epg/epg.xml.gz",
    "https://epg.lat/files/il.xml.gz",
    "https://iptv-epg.org/files/epg-il.xml",
]
TIME_LIMIT = 240  # seconds per source

def parse_time(s):
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
        raw = urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"}), timeout=120)
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
    c = Chain(head, raw)
    return gzip.GzipFile(fileobj=c) if head == b"\x1f\x8b" else c

def probe(src, now):
    t0 = time.time(); chans = set(); with_prog = set(); n = 0; first = last = None
    try:
        ctx = ET.iterparse(open_source(src), events=("start", "end"))
        _, root = next(ctx)
        for ev, el in ctx:
            if ev != "end" or el.tag not in ("channel", "programme"):
                continue
            if el.tag == "channel":
                chans.add(el.get("id"))
            else:
                n += 1; with_prog.add(el.get("channel"))
                try:
                    s = parse_time(el.get("start")); e = parse_time(el.get("stop") or el.get("start"))
                    first = s if first is None or s < first else first
                    last = e if last is None or e > last else last
                except Exception:
                    pass
            root.clear()
            if time.time() - t0 > TIME_LIMIT:
                return f"{src}\n   STOPPED after {TIME_LIMIT}s (file too slow/large) - programmes read so far: {n}"
    except Exception as ex:
        return f"{src}\n   FAILED: {type(ex).__name__}: {str(ex)[:120]}"
    if not n:
        return f"{src}\n   OK but contains no programmes (channels: {len(chans)})"
    days = (last - now).total_seconds() / 86400
    return (f"{src}\n   channels: {len(chans)} | channels with programmes: {len(with_prog)} | programmes: {n}\n"
            f"   covers {first:%Y-%m-%d %H:%M} -> {last:%Y-%m-%d %H:%M}  =>  about {days:.1f} days ahead of now")

def main(sources=None, out="probe.txt"):
    now = dt.datetime.now(dt.timezone.utc)
    lines = [f"probe run: {now:%Y-%m-%d %H:%M} UTC", ""]
    for s in (sources or SOURCES):
        lines.append(probe(s, now)); lines.append("")
    open(out, "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines))

if __name__ == "__main__":
    main(sys.argv[1:] or None)
