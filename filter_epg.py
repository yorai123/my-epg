"""Build my small EPG: primary source + extra sources that extend the days ahead.
Writes epg.xml.gz, report.txt and extra_channels.txt"""
import gzip, re, shutil, sys, tempfile, urllib.request, datetime as dt
import xml.etree.ElementTree as ET
from collections import defaultdict

PRIMARY = "https://epg.team/3.1.xml"
EXTRAS = ["https://iptv-epg.org/files/epg-il.xml"]   # sources used only to extend the days ahead
OUT, REPORT, EXTRA_LIST = "epg.xml.gz", "report.txt", "extra_channels.txt"
KEEP_PAST_HOURS = 3
MAX_DAYS_AHEAD = 4
MERGE_GAP_SECONDS = 120

# our channel name (normalised) -> name used in the extra sources (normalised)
ALIAS = {"sport5": "5sport", "sport5plus": "5plus", "sport5live": "5live", "sport5gold": "5gold",
         "sport5stars": "5stars", "sport5max": "5max", "sport54k": "5sport4k"}
DROP = {"hd", "fhd", "il", "channel", "tv", "the", "uk", "us", "ca", "cz"}

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

def norm_key(name):
    s = re.sub(r"\[.*?\]|\(.*?\)", " ", name.lower().replace("+", " plus "))
    s = re.sub(r"^\s*(il|uk|us|ca|cz)\s*:", "", s)
    words = [w for w in re.sub(r"[^a-z0-9\u0590-\u05ff]+", " ", s).split() if w not in DROP]
    return "".join(words)

def to_file(src):
    """download (or copy) a source into a temp file and return its path (handles .gz)"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xml")
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
    c = Chain(head, raw)
    src_stream = gzip.GzipFile(fileobj=c) if head == b"\x1f\x8b" else c
    shutil.copyfileobj(src_stream, tmp, 1024 * 1024)
    tmp.close()
    return tmp.name

def text_of(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None else ""

def signature(p):
    return (text_of(p, "title"), text_of(p, "sub-title"), text_of(p, "desc"))

def merge_programmes(items):
    items.sort(key=lambda x: (x[0], x[1]))
    out = []
    for start, stop, el in items:
        if out:
            pstart, pstop, pel = out[-1]
            if start == pstart:
                if stop > pstop:
                    out[-1] = (pstart, stop, pel)
                continue
            if signature(el) == signature(pel) and (start - pstop).total_seconds() <= MERGE_GAP_SECONDS:
                out[-1] = (pstart, max(pstop, stop), pel)
                continue
        out.append((start, stop, el))
    return out

def read_source(path, keep_channel, keep_programme_channel, lo, hi):
    """one pass: returns (channels{id:[names]}, programmes{id:[(s,e,el)]}, total_seen)"""
    channels, progs, seen = {}, defaultdict(list), 0
    ctx = ET.iterparse(path, events=("start", "end"))
    _, root = next(ctx)
    for ev, el in ctx:
        if ev != "end" or el.tag not in ("channel", "programme"):
            continue
        if el.tag == "channel":
            cid = el.get("id")
            names = [(d.text or "").strip() for d in el.findall("display-name")]
            if keep_channel(cid, names):
                channels[cid] = (names, el)
        else:
            seen += 1
            cid = el.get("channel")
            if keep_programme_channel(cid):
                try:
                    s = parse_time(el.get("start")); e = parse_time(el.get("stop") or el.get("start"))
                    if e >= lo and s <= hi:
                        progs[cid].append((s, e, el))
                except Exception:
                    pass
        root.clear()
    return channels, progs, seen

def main(src=PRIMARY, out=OUT, ids_file="channels.txt", now=None, report=REPORT, extras=None, extra_list=EXTRA_LIST):
    extras = EXTRAS if extras is None else extras
    ids = load_ids(ids_file)
    now = now or dt.datetime.now(dt.timezone.utc)
    lo = now - dt.timedelta(hours=KEEP_PAST_HOURS); hi = now + dt.timedelta(days=MAX_DAYS_AHEAD)
    path = to_file(src)
    channels, progs, seen = read_source(path, lambda c, n: c in ids, lambda c: c in ids, lo, hi)
    merged = {cid: merge_programmes(list(progs.get(cid, []))) for cid in ids}
    names = {cid: (channels[cid][0][0] if cid in channels and channels[cid][0] else "") for cid in ids}
    lines = [f"built: {now:%Y-%m-%d %H:%M} UTC | primary programmes seen: {seen}"]
    # ---- extra sources: extend the days ahead
    our_keys = {cid: norm_key(names[cid]) for cid in ids if names[cid]}
    ext_note, listing, extended = [], [], 0
    for ex in extras:
        try:
            epath = to_file(ex)
            ech, eprog, _ = read_source(epath, lambda c, n: True, lambda c: True, lo, hi)
        except Exception as e:
            ext_note.append(f"extra source FAILED: {ex} ({type(e).__name__}: {str(e)[:80]})"); continue
        by_key = {}
        for ecid, (enames, _) in ech.items():
            listing.append(f"{ecid} | {' / '.join(enames)}")
            for k in {norm_key(n) for n in enames if n} | {norm_key(ecid.split('.')[0])}:
                if k: by_key.setdefault(k, ecid)
        matched = 0
        for cid, k in our_keys.items():
            ecid = by_key.get(ALIAS.get(k, k))
            if not ecid or not eprog.get(ecid):
                continue
            last = max((m[1] for m in merged[cid]), default=None)
            add = []
            for s, e, el in eprog[ecid]:
                if last is not None:
                    if e <= last: continue
                    if s < last: s = last
                if e > s:
                    el.set("channel", cid); add.append((s, e, el))
            if add:
                merged[cid] = merge_programmes(merged[cid] + add); matched += 1; extended += 1
        ext_note.append(f"extra source ok: {ex} | channels in it: {len(ech)} | my channels extended: {matched}")
    lines += ext_note
    lines.append(f"{'channel':10} {'name':32} {'progs':>5}  coverage")
    kept = after = 0
    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="my-filtered-epg">\n')
        for cid in sorted(ids, key=lambda s: (len(s), s)):
            if cid in channels:
                f.write(ET.tostring(channels[cid][1], encoding="unicode"))
        for cid in sorted(ids, key=lambda s: (len(s), s)):
            items = merged[cid]; kept += len(progs.get(cid, [])); after += len(items)
            for s, e, el in items:
                el.set("start", fmt_time(s)); el.set("stop", fmt_time(e)); el.set("channel", cid)
                f.write(ET.tostring(el, encoding="unicode"))
            cov = f"{items[0][0]:%m-%d %H:%M} -> {items[-1][1]:%m-%d %H:%M}" if items else "NO PROGRAMMES"
            lines.append(f"{cid:10} {names[cid][:32]:32} {len(items):5}  {cov}")
        f.write("</tv>\n")
    lines.append(f"total: {kept} kept from primary, {after} after merging/extending")
    open(report, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    if listing:
        open(extra_list, "w", encoding="utf-8").write("\n".join(sorted(listing)) + "\n")
    print("\n".join(lines[:4]), "...", lines[-1], sep="\n")
    if after == 0:
        sys.exit("ERROR: no programmes kept - not publishing an empty EPG")

if __name__ == "__main__":
    main(*(sys.argv[1:2] or []))
