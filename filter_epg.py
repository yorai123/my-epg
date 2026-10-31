"""Build my small EPG: primary source + extra sources that extend the days ahead.
Writes epg.xml.gz, report.txt and extra_channels.txt"""
import copy, difflib, gzip, re, shutil, sys, tempfile, urllib.request, datetime as dt
import xml.etree.ElementTree as ET
from collections import defaultdict

PRIMARY = "https://epg.team/3.1.xml"
# sources used only to extend the days ahead (a source that fails is skipped and shown in report.txt)
EXTRAS = ["https://iptv-epg.org/files/epg-il.xml",
          "https://iptv-epg.org/files/epg-us.xml",
          "https://iptv-epg.org/files/epg-ca.xml"]
OUT, REPORT, EXTRA_LIST = "epg.xml.gz", "report.txt", "extra_channels.txt"
KEEP_PAST_HOURS = 3
MAX_DAYS_AHEAD = 7
WANT_DAYS = 6.5        # channels that end earlier than this are listed as SHORT in report.txt
MERGE_GAP_SECONDS = 120

# YES channels: schedule only from the extra (Hebrew) sources; names used for matching
YES_DATA = "ch1148=IL: YES Movies Action HD;ch1149=IL: YES Movies Comedy HD;ch1150=IL: YES Movies Drama HD;ch1151=IL: YES Movies Kids HD;ch1158=IL: YES TV Action HD;ch1159=IL: YES TV Comedy HD;ch1160=IL: YES TV Drama HD;ch3030=IL: YES Plus Diamonds HD;ch1915=IL: YES Plus Cinema HD;ch4033=IL: YES Plus Love HD;ch4034=IL: YES Plus Dark HD;ch3174=IL: YES World Action HD;ch3175=IL: YES World Action 2 HD"
NAME_FALLBACK = dict(x.split("=") for x in YES_DATA.split(";"))
EXTRA_FIRST = set(NAME_FALLBACK)

# our channel name -> possible names in the extra sources (compared after normalising, word order ignored)
ALIAS_RAW = {
    "Sport 5": ["5sport"], "Sport 5 Plus": ["5plus"], "Sport 5 Live": ["5live"], "Sport 5 Gold": ["5gold"],
    "Sport 5 Stars": ["5stars"], "Sport 5 MAX": ["5max"], "Sport 5 4K": ["5sport 4k"],
    "Sport 1": ["ספורט 1"], "Sport 2": ["ספורט 2"], "Sport 3": ["ספורט 3"], "Sport 4": ["ספורט 4"],
    "Channel 12": ["קשת", "קשת 12", "ערוץ 12"], "Channel 13": ["רשת", "רשת 13", "ערוץ 13"],
    "Channel 11": ["כאן 11", "כאן"], "Channel 14": ["ערוץ 14"],
    "Channel 24": ["ערוץ החדשות 24", "חדשות 24"], "Kan Chinuchit 23": ["ערוץ חינוכית 23", "חינוכית 23"],
    "Humor Channel": ["ערוץ ההומור"], "Health Channel": ["ערוץ הבריאות"],
    "History": ["היסטוריה", "ערוץ ההיסטוריה"], "Travel Channel": ["ערוץ הטיולים"], "Food Channel": ["ערוץ האוכל"],
    "Good Life TV": ["החיים הטובים"], "Reality": ["ערוץ הריאליטי"], "Disney Channel": ["דיסני"],
    "Discovery Channel": ["דיסקברי"], "Nick": ["ניקלודיאון"], "Baby TV": ["בייבי"], "Hop": ["הופ!"],
    "Yam Tihoni": ["ים תיכוני"], "Yam Tihoni Plus": ["הערוץ הים תיכוני+"],
    "Turkish Drama Plus": ["ערוץ הדרמות הטורקיות+"], "Turkish Drama 2": ["2 ערוץ הדרמות הטורקיות"],
    "Turkish Drama 3": ["3 ערוץ הדרמות הטורקיות"], "Wiz": ["ערוץ WiZ"], "Nat Geo": ["National Geographic"],
    "Nat Geo Wild": ["NG WILD"], "Eurosport 1": ["EUROSPORT"], "Eurosport 2": ["EUROSPORT 2"],
    "YES Docu": ["yes דוקו"], "YES Israeli Cinema": ["yes קולנוע ישראלי"], "Bollywood": ["yesBollywood", "BollyShow"],
    "Luli": ["HOT Luli"],
}
DROP = {"hd", "fhd", "sd", "il", "channel", "tv", "the", "uk", "us", "ca", "cz"}

# Tennis Channel 1 is added by hand (no need to list it in channels.txt): id -> (display name, regex for its name in the extra source, part of the source URL)
EXTRA_ONLY = {
    "ch1623": ("US: Tennis Channel 1 HD", r"^(us\s*:\s*)?tennis\s*channel(\s*1)?(\s*hd)?$", "epg-us"),
}
# Tennis Channel 1 and 2: put the tournament (first line of the description) in the title
RETITLE = {"ch1623", "ch1624"}
GENERIC_TITLES = {"tennis today", "tennis today live", "second serve"}

def retitle(el):
    t, d = el.find("title"), el.find("desc")
    if t is None or d is None or not (d.text or "").strip():
        return
    parts = [x.strip() for x in d.text.strip().split("\n", 1)]
    if len(parts) < 2 or not parts[0]:
        return
    first, rest = parts
    title = (t.text or "").strip()
    if first.lower() in title.lower():
        return
    if title.lower() in GENERIC_TITLES:
        new_title = f"Live: {first}" if title.lower().endswith("live") and not re.search(r"\blive\b", first, re.I) else first
    else:
        new_title = f"{title}: {first}"
    t.text = new_title
    d.text = rest if rest and rest.lower() != title.lower() else first

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
    return "".join(sorted(words))

ALIAS = {}
def _build_alias():
    for k, vs in ALIAS_RAW.items():
        ALIAS.setdefault(norm_key(k), []).extend(norm_key(v) for v in vs)

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
    ids = load_ids(ids_file) | set(EXTRA_ONLY)
    now = now or dt.datetime.now(dt.timezone.utc)
    lo = now - dt.timedelta(hours=KEEP_PAST_HOURS); hi = now + dt.timedelta(days=MAX_DAYS_AHEAD)
    path = to_file(src)
    channels, progs, seen = read_source(path, lambda c, n: c in ids, lambda c: c in ids, lo, hi)
    merged = {cid: merge_programmes(list(progs.get(cid, []))) for cid in ids}
    names = {cid: (channels[cid][0][0] if cid in channels and channels[cid][0] else NAME_FALLBACK.get(cid, "")) for cid in ids}
    for cid, (nm, _, _) in EXTRA_ONLY.items():
        names[cid] = names[cid] or nm
    lines = [f"built: {now:%Y-%m-%d %H:%M} UTC | primary programmes seen: {seen}"]
    # ---- extra sources: extend the days ahead
    _build_alias()
    our_keys = {cid: norm_key(names[cid]) for cid in ids if names[cid]}
    ext_note, listing, extended_ids, extra_keys = [], [], set(), set()
    cleared = set()
    for ex in extras:
        try:
            epath = to_file(ex)
            ech, _, _ = read_source(epath, lambda c, n: True, lambda c: False, lo, hi)   # pass 1: channel names only
            by_key = {}
            for ecid, (enames, _) in ech.items():
                listing.append(f"{ecid} | {' / '.join(enames)}")
                for k in {norm_key(n) for n in enames if n} | {norm_key(ecid.split('.')[0])}:
                    if k: by_key.setdefault(k, []).append(ecid); extra_keys.add(k)
            plan = {}   # our id -> candidate extra ids (best guesses first)
            for cid, k in our_keys.items():
                cands = []
                for kk in [k] + ALIAS.get(k, []):
                    cands += [e for e in by_key.get(kk, []) if e not in cands]
                if cands: plan[cid] = cands
            for cid, (_, rx, hint) in EXTRA_ONLY.items():   # Tennis Channel 1: match by name in the chosen source
                if hint in ex:
                    hits = [e for e, (en, _) in ech.items() if any(re.match(rx, n.strip(), re.I) for n in en + [e])]
                    ext_note.append(f"EXTRA_ONLY {cid} <- {ex}: " + (", ".join(hits) if hits else "NO MATCH (look in extra_channels.txt)"))
                    if hits: plan[cid] = hits
            wanted = {e for c in plan.values() for e in c}
            _, eprog, _ = read_source(epath, lambda c, n: False, lambda c: c in wanted, lo, hi)   # pass 2: only matched channels
        except Exception as e:
            ext_note.append(f"extra source FAILED: {ex} ({type(e).__name__}: {str(e)[:80]})"); continue
        matched = 0
        for cid, cands in plan.items():
            cands = [e for e in cands if eprog.get(e)]
            if not cands: continue
            if cid in EXTRA_FIRST and cid not in cleared:
                merged[cid] = []; cleared.add(cid)      # drop the primary feed for these channels
            ecid = max(cands, key=lambda e: max(x[1] for x in eprog[e]))   # the candidate that reaches furthest
            last = max((m[1] for m in merged[cid]), default=None)
            add = []
            for s, e, el in eprog[ecid]:
                el = copy.deepcopy(el)
                if last is not None:
                    if e <= last: continue
                    if s < last: s = last
                if e > s:
                    el.set("channel", cid); add.append((s, e, el))
            if add:
                merged[cid] = merge_programmes(merged[cid] + add); matched += 1; extended_ids.add(cid)
        ext_note.append(f"extra source ok: {ex} | channels in it: {len(ech)} | my channels extended: {matched}")
    lines += ext_note
    lines.append(f"{'channel':10} {'name':32} {'progs':>5}  coverage")
    kept = after = 0
    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="my-filtered-epg">\n')
        for cid in sorted(ids, key=lambda s: (len(s), s)):
            if cid in channels:
                f.write(ET.tostring(channels[cid][1], encoding="unicode"))
            elif cid in EXTRA_ONLY:
                f.write(f'<channel id="{cid}"><display-name>{EXTRA_ONLY[cid][0]}</display-name></channel>\n')
        for cid in sorted(ids, key=lambda s: (len(s), s)):
            items = merged[cid]; kept += len(progs.get(cid, [])); after += len(items)
            for s, e, el in items:
                if cid in RETITLE: retitle(el)
                el.set("start", fmt_time(s)); el.set("stop", fmt_time(e)); el.set("channel", cid)
                f.write(ET.tostring(el, encoding="unicode"))
            cov = f"{items[0][0]:%m-%d %H:%M} -> {items[-1][1]:%m-%d %H:%M}" if items else "NO PROGRAMMES"
            lines.append(f"{cid:10} {names[cid][:32]:32} {len(items):5}  {cov}")
        f.write("</tv>\n")
    short = [(cid, merged[cid][-1][1] if merged[cid] else now) for cid in ids if not merged[cid] or merged[cid][-1][1] < now + dt.timedelta(days=WANT_DAYS)]
    lines.append(f"SHORT (< {WANT_DAYS} days ahead): {len(short)} of {len(ids)} channels")
    for cid, end in sorted(short, key=lambda x: x[1]):
        hint = difflib.get_close_matches(our_keys.get(cid, ""), list(extra_keys), n=2, cutoff=0.6) if extra_keys else []
        lines.append(f"  short {cid:8} {names[cid][:30]:30} ends {end:%m-%d %H:%M}" + (f" | similar in extras: {hint}" if hint else ""))
    lines.append(f"total: {kept} kept from primary, {after} after merging/extending")
    open(report, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    if listing:
        open(extra_list, "w", encoding="utf-8").write("\n".join(sorted(listing)) + "\n")
    print("\n".join(lines[:4]), "...", lines[-1], sep="\n")
    if after == 0:
        sys.exit("ERROR: no programmes kept - not publishing an empty EPG")

if __name__ == "__main__":
    main(*(sys.argv[1:2] or []))
