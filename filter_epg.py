"""Download the big EPG, keep only my channels, merge repeated programmes, write epg.xml.gz + report.txt"""
import gzip, sys, urllib.request, datetime as dt
import xml.etree.ElementTree as ET
from collections import defaultdict

SRC = "https://epg.team/3.1.xml"
OUT = "epg.xml.gz"
REPORT = "report.txt"
KEEP_PAST_HOURS = 3      # programmes that ended more than this long ago are dropped
MAX_DAYS_AHEAD = 4       # today + 3 days
MERGE_GAP_SECONDS = 120  # same programme within this gap is merged into one

def load_ids(path="channels.txt"):
    return {l.strip() for l in open(path, encoding="utf-8") if l.strip()}

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

def fmt_time(t):
    return t.strftime("%Y%m%d%H%M%S %z")

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

def text_of(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None else ""

def signature(p):
    return (text_of(p, "title"), text_of(p, "sub-title"), text_of(p, "desc"))

def merge_programmes(items):
    """items: list of (start, stop, element) for ONE channel -> merged list, no duplicates"""
    items.sort(key=lambda x: (x[0], x[1]))
    out = []
    for start, stop, el in items:
        if out:
            pstart, pstop, pel = out[-1]
            if start == pstart:                      # exact duplicate start -> keep first
                if stop > pstop:
                    out[-1] = (pstart, stop, pel)
                continue
            if signature(el) == signature(pel) and (start - pstop).total_seconds() <= MERGE_GAP_SECONDS:
                out[-1] = (pstart, max(pstop, stop), pel)   # same programme back to back -> one longer entry
                continue
        out.append((start, stop, el))
    return out

def main(src=SRC, out=OUT, ids_file="channels.txt", now=None, report=REPORT):
    ids = load_ids(ids_file)
    now = now or dt.datetime.now(dt.timezone.utc)
    lo = now - dt.timedelta(hours=KEEP_PAST_HOURS)
    hi = now + dt.timedelta(days=MAX_DAYS_AHEAD)
    channels, progs = [], defaultdict(list)
    seen_pr = 0
    ctx = ET.iterparse(open_source(src), events=("start", "end"))
    _, root = next(ctx)
    for ev, el in ctx:
        if ev != "end" or el.tag not in ("channel", "programme"):
            continue
        if el.tag == "channel":
            if el.get("id") in ids:
                channels.append(el)
        else:
            seen_pr += 1
            cid = el.get("channel")
            if cid in ids:
                try:
                    s = parse_time(el.get("start")); e = parse_time(el.get("stop") or el.get("start"))
                    if e >= lo and s <= hi:
                        progs[cid].append((s, e, el))
                except Exception:
                    pass
        root.clear()
    kept_pr = merged_pr = 0
    lines = [f"built: {now:%Y-%m-%d %H:%M} UTC | source programmes seen: {seen_pr}",
             f"{'channel':10} {'name':32} {'progs':>5} {'distinct titles':>15}  coverage (UTC offset as in source)"]
    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="my-filtered-epg">\n')
        for ch in channels:
            f.write(ET.tostring(ch, encoding="unicode"))
        names = {c.get("id"): (text_of(c, "display-name") or "") for c in channels}
        for cid in sorted(ids, key=lambda s: (len(s), s)):
            items = progs.get(cid, [])
            merged = merge_programmes(items)
            kept_pr += len(items); merged_pr += len(merged)
            for s, e, el in merged:
                el.set("start", fmt_time(s)); el.set("stop", fmt_time(e))
                f.write(ET.tostring(el, encoding="unicode"))
            if merged:
                titles = {signature(el)[0] for _, _, el in merged}
                lines.append(f"{cid:10} {names.get(cid,'')[:32]:32} {len(merged):5} {len(titles):15}  {merged[0][0]:%m-%d %H:%M} -> {merged[-1][1]:%m-%d %H:%M}")
            else:
                lines.append(f"{cid:10} {names.get(cid,'')[:32]:32} {0:5} {0:15}  NO PROGRAMMES")
        f.write("</tv>\n")
    lines.append(f"total: {kept_pr} kept, {merged_pr} after merging")
    open(report, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines[:3]), "...", lines[-1], sep="\n")
    if merged_pr == 0:
        sys.exit("ERROR: no programmes kept - not publishing an empty EPG")

if __name__ == "__main__":
    main(*(sys.argv[1:2] or []))
