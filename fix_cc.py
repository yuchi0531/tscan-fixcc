#!/usr/bin/env python3
"""
fix_cc.py — TS continuity_counter repair for streams with PCR-only packets

[English]
Some CATV (transmodulation) head-ends transmit the PCR in packets where
adaptation_field_control == 2 (adaptation field only, no payload), and in that
case they do NOT increment continuity_counter. This is correct per
ISO/IEC 13818-1 ("the continuity_counter shall not be incremented when the
adaptation_field_control of the packet equals '00' or '10'").

Amatsukaze's PesParser::onTsPacket compares the counter on every packet,
including adaptation-only ones:

    int cc = packet.continuity_counter();
    if (cc != contCounter) { buffer.clear(); }   // false positive
    contCounter = (cc + 1) & 0xF;                // increments even for afc=2

So each PCR-only packet is treated as a continuity error and the partially
assembled PES is thrown away. The sequence_header at the head of an I-frame
PES is lost, the video format is never determined, the detected resolution
stays 0x0, and Amatsukaze rejects the recording with

    映像が小さすぎます(0,0)   ("video is too small (0,0)")   -> PreFailed

Streams that carry the PCR inside payload packets (afc=3), as terrestrial,
BS/CS and most pass-through feeds do, are unaffected.

[日本語]
一部の CATV(トランスモジュレーション)のヘッドエンドは PCR を
adaptation_field_control=2 (アダプションフィールドのみ / ペイロードなし) の
パケットで送出し、そのとき continuity_counter を進めない。これは
ISO/IEC 13818-1 どおりの正しい動作。

Amatsukaze の PesParser::onTsPacket は afc=2 のパケットでも CC が進む前提で
比較するため、PCR 専用パケットを「連続性エラー」と誤判定して PES バッファを
破棄する。結果 I フレーム PES 先頭の sequence_header が失われ、解像度が
0x0 のままになり「映像が小さすぎます(0,0)」で PreFailed になる。

--- 対処 / What it does ---
PMT から映像 PID を特定し、その PID の continuity_counter だけを
「全パケットで +1」の連番に付け直す。他のバイトは一切変更しないので
PAT/PMT/NIT/SDT/EIT/TOT/EMM/ECM/音声/字幕/データは完全に保持される。

Only the continuity_counter of the video PID(s) listed in the PMT is
renumbered. Every other byte is left untouched, so PSI/SI (PAT, PMT, CAT,
NIT, SDT, BAT, EIT, TDT/TOT) and all non-video PIDs are preserved bit for bit.

使い方 / Usage:
  fix_cc.py --auto <in.ts> <out.ts>      repair only if a stall is detected
  fix_cc.py <in.ts> <out.ts> <pidhex>    unconditionally renumber one PID

--auto の終了コード / exit codes:
  0 = repaired
  3 = nothing to do (safe to skip; no output file is created)
  other = error
"""
import os
import sys

PKT = 188
SAMPLE_BYTES = 200 * 1024 * 1024
PSI_SCAN_BYTES = 40 * 1024 * 1024

# stream_type values that carry a video elementary stream
VIDEO_STREAM_TYPES = (0x01, 0x02, 0x1B, 0x24, 0x42)


def _sections(chunks):
    """Reassemble PSI sections from [(pid, afc, pusi, payload)] tuples."""
    buf = {}
    for pid, afc, pusi, pay in chunks:
        if not (afc & 1):
            continue
        if pusi:
            if not pay:
                continue
            ptr = pay[0]
            buf[pid] = bytearray(pay[1 + ptr:])
        else:
            if pid in buf:
                buf[pid] += pay
        d = buf.get(pid)
        if d is None or len(d) < 3:
            continue
        total = 3 + (((d[1] & 0x0F) << 8) | d[2])
        if len(d) >= total:
            yield pid, bytes(d[:total])
            del buf[pid]


def _packets(path, limit=None):
    """Yield (pid, afc, pusi, payload) for every packet, optionally capped."""
    with open(path, "rb") as f:
        data = f.read(limit) if limit else None
        if data is None:
            f.seek(0)
            while True:
                data = f.read(PKT * 8192)
                if not data:
                    return
                for item in _iter(data):
                    yield item
        else:
            for item in _iter(data):
                yield item


def _iter(buf):
    for k in range(len(buf) // PKT):
        p = buf[k * PKT:(k + 1) * PKT]
        if p[0] != 0x47:
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        afc = (p[3] >> 4) & 3
        q = 4
        if afc & 2:
            q = 5 + p[4]
        if not (afc & 1) or q >= PKT:
            yield pid, afc, bool(p[1] & 0x40), None
            continue
        yield pid, afc, bool(p[1] & 0x40), p[q:PKT]


def find_video_pids(path):
    """Return the set of elementary PIDs declared as video in the PMT."""
    pmt_pids = set()

    pat = [(pid, afc, pusi, pay)
           for pid, afc, pusi, pay in _packets(path, PSI_SCAN_BYTES)
           if pid == 0 and pay is not None]
    for _, sec in _sections(pat):
        if sec[0] != 0x00:
            continue
        end = len(sec) - 4
        i = 8
        while i + 4 <= end:
            program = (sec[i] << 8) | sec[i + 1]
            pmt = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
            if program != 0:
                pmt_pids.add(pmt)
            i += 4

    if not pmt_pids:
        return set()

    pmts = [(pid, afc, pusi, pay)
            for pid, afc, pusi, pay in _packets(path, PSI_SCAN_BYTES)
            if pid in pmt_pids and pay is not None]
    vids = set()
    for _, sec in _sections(pmts):
        if sec[0] != 0x02:
            continue
        pil = ((sec[10] & 0x0F) << 8) | sec[11]
        i = 12 + pil
        end = len(sec) - 4
        while i + 5 <= end:
            stream_type = sec[i]
            elementary_pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
            es_info_len = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
            if stream_type in VIDEO_STREAM_TYPES:
                vids.add(elementary_pid)
            i += 5 + es_info_len
    return vids


def _windows(path):
    size = os.path.getsize(path)
    if size <= SAMPLE_BYTES:
        return [(0, size)]
    half = SAMPLE_BYTES // 2
    return [(0, half), (size // 2, size // 2 + half)]


def stalled_pids(path):
    """PIDs that carry afc=2 packets without incrementing the counter."""
    bad = set()
    for start, end in _windows(path):
        prev = {}
        with open(path, "rb") as f:
            f.seek(start)
            remain = end - start
            while remain > 0:
                chunk = f.read(min(PKT * 8192, remain))
                if not chunk:
                    break
                remain -= len(chunk)
                for k in range(len(chunk) // PKT):
                    p = chunk[k * PKT:(k + 1) * PKT]
                    if p[0] != 0x47:
                        continue
                    pid = ((p[1] & 0x1F) << 8) | p[2]
                    cc = p[3] & 0x0F
                    afc = (p[3] >> 4) & 3
                    pc = prev.get(pid)
                    if pc is not None and afc == 2 and cc == pc:
                        bad.add(pid)
                    prev[pid] = cc
    return bad


def detect(path, warn=True):
    """Return the video PIDs that need renumbering."""
    bad = stalled_pids(path)
    if not bad:
        return set()

    vids = find_video_pids(path)
    if vids:
        return bad & vids

    if warn:
        sys.stderr.write(
            "fix_cc: WARNING: afc=2 continuity stall on PID(s) %s but no video "
            "PID could be identified from the PMT; skipped\n"
            % [hex(x) for x in sorted(bad)])
    return set()


def renumber(path, out, pids):
    counters = {p: 0 for p in pids}
    n = 0
    with open(path, "rb") as f, open(out, "wb") as g:
        while True:
            chunk = f.read(PKT * 8192)
            if not chunk:
                break
            buf = bytearray(chunk)
            for k in range(0, len(buf) - PKT + 1, PKT):
                if buf[k] != 0x47:
                    continue
                pid = ((buf[k + 1] & 0x1F) << 8) | buf[k + 2]
                if pid not in counters:
                    continue
                buf[k + 3] = (buf[k + 3] & 0xF0) | counters[pid]
                counters[pid] = (counters[pid] + 1) & 0x0F
                n += 1
            g.write(bytes(buf))
    return n


def main(argv):
    args = argv[1:]
    if len(args) == 3 and args[0] == "--auto":
        src, dst = args[1], args[2]
        pids = detect(src)
        if not pids:
            print("fix_cc: ok, nothing to repair: %s" % src)
            return 3
        pids = sorted(pids)
        n = renumber(src, dst, pids)
        print("fix_cc: renumbered %d packets of video PID(s) %s -> %s"
              % (n, [hex(p) for p in pids], dst))
        return 0

    if len(args) == 3:
        src, dst, pidhex = args[0], args[1], args[2]
        try:
            pid = int(pidhex, 16)
        except ValueError:
            sys.stderr.write("fix_cc: invalid PID: %s\n" % pidhex)
            return 2
        n = renumber(src, dst, [pid])
        print("fix_cc: renumbered %d packets of PID 0x%04x -> %s"
              % (n, pid, dst))
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
