# fix_cc.py

Repair MPEG-TS continuity counters that stall on PCR-only packets, so
[Amatsukaze](https://github.com/nekopanda/Amatsukaze) can read the stream.

## Symptom

Recordings from a CATV (transmodulation) feed fail in Amatsukaze with

```
映像が小さすぎます(0,0)
```

("video is too small (0,0)"), state `PreFailed`. Terrestrial, BS/CS and
pass-through streams are fine.

## Root cause

Some CATV head-ends put the PCR in packets with
`adaptation_field_control == 2` (adaptation field only, no payload) and do
**not** increment `continuity_counter` for them. That is correct per
ISO/IEC 13818-1 §2.4.3.4:

> The continuity_counter shall not be incremented when the
> adaptation_field_control of the packet equals `'00'` or `'10'`.

Amatsukaze's `PesParser::onTsPacket` (`Amatsukaze/Mpeg2TsParser.hpp`) compares
the counter on every packet of a PID, including adaptation-only ones:

```cpp
int cc = packet.continuity_counter();
if (cc != contCounter) {
    buffer.clear();          // false positive
}
contCounter = (cc + 1) & 0xF;   // increments even for afc=2
```

So every PCR-only packet looks like a continuity error and the partially
assembled PES is discarded. The `sequence_header` (`0x000001B3`) at the head of
an I-frame PES is lost, `MPEG2VideoParser::inputFrame` never settles on a
format, `videoFormat.width` stays `0`, and `QueueManager.cs:388` trips the
one-seg check (`width <= 320 || height <= 260`).

Streams that carry the PCR inside payload packets (`afc=3`) — terrestrial,
BS/CS, most pass-through feeds — never hit this path.

### Measured on one failing recording

| stream | CC stalls on video PID | result |
| --- | --- | --- |
| CATV original | 942 / 179,821 (0.52%) | `PreFailed (0,0)` |
| CATV after `fix_cc.py` | **0** | 1440x1080 detected, encode succeeded |
| J:COM pass-through | 0 | works |
| BS | 0 (PCR on a separate PID) | works |

All stalls were `afc=2` packets whose counter equalled the previous packet's,
i.e. spec-compliant. Every other PID (PAT, PMT, CAT, NIT, SDT, EIT, TDT/TOT,
EMM, ECM, audio) had zero stalls.

## What it does

1. Reads the PAT and PMT to find the elementary PIDs declared as video.
2. Samples the first and the middle 200 MB looking for `afc=2` packets that
   did not advance the counter.
3. If any of those PIDs is a video PID, rewrites **only** the low nibble of
   byte 3 (the continuity counter) of that PID, keeping a running 0-15 count.

Nothing else changes. PSI/SI (`PAT`, `PMT`, `CAT`, `NIT`, `SDT`, `BAT`, `EIT`,
`TDT/TOT`), EMM/ECM, audio, subtitles and data are preserved byte for byte, and
the file size is identical.

Verified against a 2.2 GB stream that needed no repair, forced through the
renumbering path: 9,972,590 packets differed, all of them by the CC nibble
alone, 0 unexpected byte changes.

## Usage

```sh
# repair only when a stall is actually detected (recommended)
python3 fix_cc.py --auto input.ts output.ts

# unconditionally renumber one PID
python3 fix_cc.py input.ts output.ts 1440
```

Exit codes for `--auto`:

| code | meaning |
| --- | --- |
| `0` | repaired, `output.ts` written |
| `3` | nothing to do, **no output file created** |
| other | error |

`--auto` is idempotent: running it again on an already repaired file returns
`3` and touches nothing.

## Scope

Only video PIDs listed in the PMT are ever rewritten. If the PMT cannot be
parsed, the tool does nothing and prints a warning — it will not guess a PID.

Streams from terrestrial, BS/CS or pass-through feeds report `exit 3` and are
left completely untouched.

## Wiring it into a recording pipeline

See [`hook-example.sh`](hook-example.sh). With EpgTimer, point `BatFilePath`
at the hook so the repair runs before the file is queued:

```ini
[REC_DEF]
BatFilePath=/path/to/amatsukaze-addtask.sh
```

The example preserves ownership and mode when replacing the original, and
still queues the recording even if the repair fails.

## Related note

If encodes abort with `並列パイプへの書き込みに失敗` (`Encoder.cpp:768`,
`Amatsukaze.exe` exit code 1) on a small GPU, check `EncoderParallel` in the
profile. Three parallel NVENC sessions exceed what some cards allow; two is
safe on a Quadro P400.
