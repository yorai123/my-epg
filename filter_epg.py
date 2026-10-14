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

# YES channels: take the schedule ONLY from the extra (Hebrew) sources, not from the primary feed
EXTRA_FIRST = {"ch1148", "ch1149", "ch1150", "ch1151", "ch1158", "ch1159", "ch1160",
               "ch3030", "ch1915", "ch4033", "ch4034", "ch3174", "ch3175"}
# names for channels that the primary feed does not carry (used to match them in the extra sources)
NAME_FALLBACK = {
    "ch1148": "IL: YES Movies Action HD", "ch1149": "IL: YES Movies Comedy HD",
    "ch1150": "IL: YES Movies Drama HD", "ch1151": "IL: YES Movies Kids HD",
    "ch1158": "IL: YES TV Action HD", "ch1159": "IL: YES TV Comedy HD",
    "ch1160": "IL: YES TV Drama HD", "ch3030": "IL: YES Plus Diamonds HD",
    "ch1915": "IL: YES Plus Cinema HD", "ch4033": "IL: YES Plus Love HD",
