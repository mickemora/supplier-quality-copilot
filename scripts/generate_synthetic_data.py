#!/usr/bin/env python3
"""Generate a synthetic NCR / 8D corpus for the supplier quality copilot.

All suppliers, programs, parts, and people are fictional.

Built-in storyline (what the demo shows)
- Pattern A, "gold plating thin": three connector suppliers send plating to the same
  tier-2 shop (Coastal Finishing Co.). A closed 8D at SUP-0145 a year ago fixed it
  with per-shift bath titration and XRF thickness checks. SUP-0107 has two recent
  open NCRs. ``data/demo/`` holds a new NCR from SUP-0112: uploading it during the
  demo makes the pattern span two active suppliers, and the detector raises an alert.
- Pattern B, "BGA voiding": two PCBA suppliers changed solder paste lots without
  re-validating the reflow profile. Active from the start so alerts aren't empty.
- Background: ordinary, unrelated NCRs across 18 months.

Usage:  python scripts/generate_synthetic_data.py [--today 2026-10-15] [--seed 7]
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUPPLIERS = [
    # id, name, commodity, tier, location, approval, spend
    ("SUP-0107", "Ardent Interconnect", "connectors", "1", "Tempe, AZ", "approved", "$5M-$10M"),
    ("SUP-0112", "Mesa Contact Systems", "connectors", "1", "Mesa, AZ", "approved", "$1M-$5M"),
    ("SUP-0131", "Northgate Connector Works", "connectors", "2", "Wichita, KS", "approved", "$1M-$5M"),
    ("SUP-0145", "Pinnacle Harness Assemblies", "cable assemblies", "1", "Huntsville, AL", "approved", "$5M-$10M"),
    ("SUP-0203", "Brazos Electronics Assembly", "pcba", "1", "Waco, TX", "approved", "$10M-$25M"),
    ("SUP-0219", "Lone Star Circuit Works", "pcba", "1", "Plano, TX", "conditional", "$5M-$10M"),
    ("SUP-0301", "Red River Castings", "castings", "2", "Texarkana, TX", "approved", "$1M-$5M"),
    ("SUP-0322", "Caddo Precision Machining", "machined parts", "1", "Shreveport, LA", "approved", "$5M-$10M"),
    ("SUP-0340", "Pecos Aero Machining", "machined parts", "2", "Odessa, TX", "probation", "$1M-$5M"),
    ("SUP-0410", "Ironclad Fastener Supply", "fasteners", "2", "Tulsa, OK", "approved", "<$1M"),
    ("SUP-0455", "Skyline Composites", "composites", "1", "Fort Worth, TX", "approved", "$5M-$10M"),
    ("SUP-0502", "Trinity Wire & Cable", "wire and cable", "2", "Dallas, TX", "approved", "$1M-$5M"),
]
SUPPLIER = {s[0]: s for s in SUPPLIERS}

PROGRAMS = ["Program ATLAS", "Program BRAVO-7", "Program CORSAIR", "Program DELTA RISE"]
INSPECTORS = ["J. Alvarez", "K. Nguyen", "R. Patel", "S. Okafor", "M. Brennan", "T. Lindqvist"]
SQES = ["D. Whitfield (SQE)", "A. Romero (SQE)", "L. Chen (SQE)"]

PARTS = {
    "connectors": [("CN-44871-03", "Circular connector, 19-pin, gold-plated contacts"),
                   ("CN-51220-11", "Micro-D connector, 25-position"),
                   ("CN-38016-07", "RF coaxial connector, SMA bulkhead")],
    "cable assemblies": [("CA-70215-02", "Harness assembly, guidance section"),
                         ("CA-70388-05", "Umbilical cable assembly")],
    "pcba": [("PA-90412-01", "Processor card assembly"), ("PA-90577-04", "Power conditioning board")],
    "castings": [("CS-21044-00", "Aluminum A356 housing casting")],
    "machined parts": [("MP-60311-09", "Actuator housing, 7075-T73"), ("MP-60498-02", "Bulkhead ring")],
    "fasteners": [("FS-11720-16", "Hi-lok pin, titanium")],
    "composites": [("CM-33015-01", "Radome panel, quartz/cyanate ester")],
    "wire and cable": [("WC-80110-22", "Shielded twisted pair, 22 AWG")],
}


@dataclass
class Template:
    key: str
    commodity: str
    defect_code: str
    detection_point: str
    defects: list[str]
    category: str
    causes: list[str]
    containment: str
    corrective: str
    severity: str = "major"
    disposition: str = "return to supplier"
    extra: list[str] = field(default_factory=list)


BACKGROUND = [
    Template("burrs", "machined parts", "DIM-14", "receiving inspection",
             ["Burrs on internal cross-drilled ports exceeding 0.005 in. allowance",
              "Raised burrs found at the intersection of cross-drilled passages"],
             "Method", ["The deburr operation was skipped after a router change merged two operations; the traveler no longer called out deburr inspection.",
                        "Operation 40 deburr step was removed during a routing update and the operator was not alerted."],
             "100% re-inspection of stock with borescope; suspect lots quarantined.",
             "Restored the deburr operation to the routing with an inspection buyoff; routing changes now need quality sign-off."),
    Template("porosity", "castings", "MAT-03", "in-process",
             ["Subsurface porosity revealed after machining sealing surface",
              "Gas porosity exposed on O-ring groove after finish machining"],
             "Material", ["Melt degassing time was shortened during a furnace rebuild and hydrogen levels were not verified with reduced pressure testing.",
                          "Rotary degassing cycle was cut short after the furnace rebuild; density index checks were not performed."],
             "X-ray of all castings in WIP; 6 rejected.",
             "Reinstated degassing cycle with density index check per heat; added reduced pressure test records to CoC."),
    Template("torque", "fasteners", "MEA-07", "final test",
             ["Installed fasteners below minimum torque on audit",
              "Torque audit found collars under minimum installation torque"],
             "Measurement", ["Torque wrench used on the line was 4 months past calibration and read 12% high.",
                             "Out-of-calibration torque tool reading high; calibration recall system missed the tool."],
             "Re-torque audit of all assemblies built with the tool.",
             "Tool recalled and calibrated; calibration system now locks out expired tools at check-out.", severity="critical",
             disposition="rework"),
    Template("revision", "machined parts", "DOC-02", "receiving inspection",
             ["Parts machined to superseded drawing revision C instead of revision D",
              "Hole pattern matches obsolete drawing revision"],
             "Method", ["Supplier's document control pulled the drawing from a local copy instead of the controlled portal.",
                        "Programmer used an uncontrolled local drawing file that was one revision behind."],
             "Lot rejected and returned; replacement parts expedited.",
             "Removed local drawing copies; CNC programs now reference the controlled revision number checked at first article."),
    Template("fod", "cable assemblies", "FOD-01", "in-process",
             ["Foreign object debris (wire clippings) found inside connector backshell",
              "Loose wire strands found trapped in harness backshell"],
             "Man", ["Assembler did not perform the FOD inspection before closing the backshell; workstation lacked a FOD mat.",
                     "Backshell closed without FOD check during an overtime shift; FOD control area was not enforced."],
             "Opened and inspected all backshells in the lot.",
             "Added FOD inspection stamp before backshell closure and retrained assemblers.", severity="critical",
             disposition="rework"),
    Template("esd", "pcba", "ESD-05", "final test",
             ["Board failed functional test; ESD damage on input buffer",
              "Intermittent failure traced to ESD damage on an input IC"],
             "Environment", ["Relative humidity in the kitting area fell below 30% and ionizers were not operating.",
                             "Ionizer failure in the kitting area combined with low humidity; wrist strap checks were not logged."],
             "Retest of all boards from the same kit date.",
             "Ionizer preventive maintenance added and humidity alarms installed in kitting."),
    Template("delam", "composites", "MAT-11", "receiving inspection",
             ["Delamination detected by ultrasonic inspection near panel edge",
              "Ultrasonic C-scan shows a disbond along the panel edge"],
             "Method", ["Autoclave cure ran with a thermocouple lagging the part, so the dwell started before the laminate reached temperature.",
                        "Lagging thermocouple caused an early start of the cure dwell; the laminate was under-cured."],
             "Quarantine of panels from the same cure load.",
             "Lagging thermocouple now controls dwell start; cure records reviewed before release.", severity="critical",
             disposition="scrap"),
    Template("nicks", "wire and cable", "WRK-09", "in-process",
             ["Insulation nicks found on conductors after stripping",
              "Conductor strand damage and insulation nicks at strip location"],
             "Machine", ["Stripping blades were worn past their replacement interval; blade change was tracked by memory, not counts.",
                         "Worn stripper blades with no cycle-count based replacement."],
             "100% visual inspection under magnification of affected lot.",
             "Cycle-count based blade replacement with automatic stop at limit.", disposition="scrap"),
    Template("coc", "fasteners", "DOC-06", "receiving inspection",
             ["Certificate of conformance missing lot traceability to mill heat",
              "CoC did not include heat lot traceability"],
             "Method", ["Distributor changed its CoC template and dropped the heat lot field.",
                        "New distributor paperwork omitted mill heat numbers."],
             "Lot held until traceable certifications were provided.",
             "Supplier added CoC field checklist; receiving now verifies heat traceability before acceptance.",
             severity="minor", disposition="pending"),
    Template("bore", "machined parts", "DIM-21", "receiving inspection",
             ["Bore diameter oversize by 0.0008 in. on parts machined late in shift",
              "Bore drifted oversize over the course of the production run"],
             "Machine", ["Spindle thermal growth on a long run; the machine's thermal compensation was disabled after a control update.",
                         "Thermal compensation turned off after a controller software update, letting the bore grow during long runs."],
             "Sorted lot with air gauging; oversize parts rejected.",
             "Re-enabled thermal compensation and added an in-process bore check every 10 parts."),
    Template("corrosion", "castings", "ENV-02", "receiving inspection",
             ["White corrosion product on castings after shipment",
              "Surface corrosion found on unpacked castings"],
             "Environment", ["Parts were packed while still damp from washing and sealed without desiccant.",
                             "Packaging step sealed parts with residual moisture and no desiccant."],
             "Cleaned and re-inspected affected parts.",
             "Added drying oven step and desiccant to the packaging instruction.", severity="minor", disposition="rework"),
    Template("coating", "pcba", "WRK-15", "in-process",
             ["Conformal coating voids and bubbles near connector headers",
              "Coating voids around tall components and connector headers"],
             "Method", ["Spray coating viscosity drifted high and the cure ramp was too fast, trapping solvent.",
                        "Coating material viscosity out of range and fast cure ramp trapped solvent bubbles."],
             "Rework of coating on the lot.",
             "Viscosity check per shift and slower cure ramp added to the process sheet.", disposition="rework"),
    Template("marking", "connectors", "MRK-03", "receiving inspection",
             ["Part marking shows wrong dash number",
              "Laser marking dash number does not match the packing slip"],
             "Man", ["Operator selected the wrong marking file from the drop-down list.",
                     "Wrong marking program selected; no verification step against the work order."],
             "Lot quarantined and re-marked after verification.",
             "Marking program now selected by barcode scan of the work order.", severity="minor", disposition="rework"),
    Template("crimp", "cable assemblies", "WRK-04", "final test",
             ["Contact pull test failure on crimped contacts",
              "Crimped contacts failed pull-out force test"],
             "Machine", ["Crimp tool die was the wrong size for the contact after a tool swap.",
                         "Incorrect crimp die installed after maintenance; first-piece pull test was not done."],
             "All contacts crimped since the tool swap were pull-tested.",
             "First-piece pull test required after any crimp tool change.", severity="critical", disposition="rework"),
]

PLATING = Template(
    "plating", "connectors", "FIN-12", "receiving inspection",
    ["Gold plating thickness on contacts measured 18-24 microinches against a 50 microinch minimum (XRF)",
     "XRF shows contact gold plating at 20 microinches, below the 50 microinch minimum",
     "Contacts measured thin on gold: 15-30 microinches vs 50 minimum; nickel underplate exposed at mating end",
     "Thin gold plating on socket contacts (avg 22 microinches; spec 50 min) found at receiving XRF check"],
    "Material",
    ["Plating is subcontracted to the tier-2 finisher Coastal Finishing Co. Gold bath chemistry drifted low because bath titration was done weekly instead of per shift, and the line ran a higher rack loading after a capacity increase.",
     "Tier-2 plater Coastal Finishing Co. allowed gold bath concentration to drift below range; titration interval was extended to weekly when they added a second shift for higher volume.",
     "Root cause traced to the sub-tier plating source, Coastal Finishing Co.: depleted gold bath (titration weekly, not per shift) and increased rack loading reduced deposit thickness.",
     "Suspected low gold concentration in the plating bath at sub-tier Coastal Finishing Co.; bath analysis frequency was reduced after a volume ramp. Investigation open."],
    "Lot quarantined; 100% XRF thickness check on stock and WIP; supplier notified to hold shipments.",
    "Coastal Finishing Co. moved to per-shift bath titration with control-chart limits and returned to qualified rack loading; supplier added XRF sampling (5 per lot) at its receiving before assembly.",
    severity="critical", disposition="return to supplier",
)

VOIDING = Template(
    "voiding", "pcba", "SLD-08", "final test",
    ["X-ray shows BGA solder voiding of 32-40% on the processor package (limit 25%)",
     "BGA voids exceed 25% limit on X-ray; intermittent failure at thermal cycling",
     "Excessive BGA voiding (up to 38%) found by X-ray after a thermal cycling failure"],
    "Method",
    ["Solder paste lot changed to a new formulation and the reflow profile was not re-validated; soak time was too short for the new flux to outgas.",
     "New solder paste lot with different flux chemistry was used on the old reflow profile; no profile re-validation after a paste change.",
     "Reflow profile not re-profiled after switching solder paste lots; insufficient soak traps flux volatiles in BGA joints."],
    "100% X-ray of BGAs on all boards built since the paste change; failing boards reworked.",
    "Paste lot or formulation changes now trigger a reflow profile re-validation with X-ray first article; soak extended by 20 seconds.",
    severity="critical", disposition="rework",
)


@dataclass
class Ncr:
    ncr_id: str
    supplier_id: str
    template: Template
    detected: date
    status: str
    variant: int
    part: tuple[str, str]
    program: str
    qty: int


# Supplier responsiveness: (days to 8D response, days to closure) ranges.
# A couple of suppliers are deliberately slow so the scorecards have a story.
RESPONSE_PROFILE = {
    "SUP-0340": ((28, 50), (70, 120)),   # on probation, slow to respond and close
    "SUP-0219": ((20, 42), (50, 90)),
    "SUP-0107": ((14, 36), (40, 70)),
}
DEFAULT_PROFILE = ((5, 21), (20, 50))
RESPONSE_DUE_DAYS = 30


def render(n: Ncr, rng: random.Random, today: date | None = None) -> str:
    today = today or date.today()
    sid, name, commodity, tier, location, *_ = SUPPLIER[n.supplier_id]
    (r_lo, r_hi), (c_lo, c_hi) = RESPONSE_PROFILE.get(sid, DEFAULT_PROFILE)
    responded = n.detected + timedelta(days=rng.randint(r_lo, r_hi))
    closed = max(responded + timedelta(days=3),
                 n.detected + timedelta(days=rng.randint(c_lo, c_hi)))
    if closed >= today:
        closed = today - timedelta(days=1)
    due = n.detected + timedelta(days=RESPONSE_DUE_DAYS)
    t = n.template
    defect = t.defects[n.variant % len(t.defects)]
    cause = t.causes[n.variant % len(t.causes)]
    if n.status == "open" and "Investigation open" not in cause:
        cause_line = f"Suspected root cause (investigation in progress): {cause}"
    else:
        cause_line = f"Root cause: {cause}"
    lines = [
        "NONCONFORMANCE REPORT",
        f"NCR No.: {n.ncr_id}",
        f"Date detected: {n.detected.isoformat()}",
        f"Supplier: {name} ({sid}), {location}, tier {tier}",
        f"Commodity: {commodity}",
        f"Part number: {n.part[0]} - {n.part[1]}",
        f"Program: {n.program}",
        f"Detection point: {t.detection_point}",
        f"Defect code: {t.defect_code}",
        f"Quantity inspected / nonconforming: {n.qty * rng.randint(3, 10)} / {n.qty}",
        f"Severity: {t.severity}",
        f"Reported by: {rng.choice(INSPECTORS)}",
        "",
        "DESCRIPTION OF NONCONFORMANCE",
        defect + ".",
        "",
        "CONTAINMENT",
        t.containment,
        "",
        "SUPPLIER RESPONSE",
        f"8D response due: {due.isoformat()}",
    ]
    if n.status == "closed" or responded < today:
        lines.append(f"8D response received: {min(responded, closed if n.status == 'closed' else responded).isoformat()}")
    else:
        lines.append("8D response received: not yet received")
    lines.append("")
    if n.status == "closed":
        lines += [
            "8D SUMMARY",
            f"D1 Team: supplier quality, manufacturing engineering, {rng.choice(SQES)}",
            f"D2 Problem: {defect}.",
            f"D3 Containment: {t.containment}",
            f"D4 {cause_line}",
            f"D5 Permanent corrective action: {t.corrective}",
            f"D6 Verification: next {rng.choice([3, 4, 5])} lots inspected with zero rejects; effectiveness verified.",
            "D7 Prevention: process FMEA and control plan updated.",
            f"D8 Closure: SCAR closed {closed.isoformat()}.",
            "",
            f"Disposition: {t.disposition}",
            "Status: CLOSED",
        ]
    else:
        lines += [
            "INVESTIGATION",
            cause_line,
            "",
            "Corrective action: pending supplier 8D response.",
            "Disposition: pending",
            "Status: OPEN",
        ]
    return "\n".join(lines) + "\n"


def build(today: date, seed: int) -> tuple[list[Ncr], Ncr]:
    rng = random.Random(seed)
    events: list[tuple[str, Template, date, str, int]] = []

    def days_ago(d: int) -> date:
        return today - timedelta(days=d)

    # Pattern A history and active NCRs (SUP-0112 demo NCR is created separately).
    events += [
        ("SUP-0145", PLATING, days_ago(395), "closed", 0),
        ("SUP-0107", PLATING, days_ago(33), "open", 1),
        ("SUP-0107", PLATING, days_ago(6), "open", 2),
    ]
    # Pattern B
    events += [
        ("SUP-0203", VOIDING, days_ago(250), "closed", 0),
        ("SUP-0203", VOIDING, days_ago(41), "open", 1),
        ("SUP-0219", VOIDING, days_ago(17), "open", 2),
    ]

    by_commodity: dict[str, list[str]] = {}
    for s in SUPPLIERS:
        by_commodity.setdefault(s[2], []).append(s[0])

    # Background history: older than the recent window, any template, any supplier.
    for _ in range(58):
        t = rng.choice(BACKGROUND)
        sid = rng.choice(by_commodity[t.commodity])
        events.append((sid, t, days_ago(rng.randint(75, 540)), "closed", rng.randint(0, 3)))
    # Recent background: one NCR per distinct template so nothing unrelated clusters.
    for t in rng.sample(BACKGROUND, 8):
        sid = rng.choice(by_commodity[t.commodity])
        events.append((sid, t, days_ago(rng.randint(2, 55)),
                       rng.choice(["open", "open", "closed"]), rng.randint(0, 3)))

    events.sort(key=lambda e: e[2])
    seq: dict[int, int] = {}
    ncrs = []
    for sid, t, detected, status, variant in events:
        seq[detected.year] = seq.get(detected.year, 0) + 1
        ncrs.append(Ncr(
            ncr_id=f"NCR-{detected.year}-{seq[detected.year]:04d}",
            supplier_id=sid, template=t, detected=detected, status=status, variant=variant,
            part=rng.choice(PARTS[SUPPLIER[sid][2]]), program=rng.choice(PROGRAMS),
            qty=rng.randint(2, 60),
        ))

    demo_year = today.year
    seq[demo_year] = seq.get(demo_year, 0) + 1
    demo = Ncr(
        ncr_id=f"NCR-{demo_year}-{seq[demo_year]:04d}", supplier_id="SUP-0112", template=PLATING,
        detected=today - timedelta(days=1), status="open", variant=3,
        part=PARTS["connectors"][0], program="Program ATLAS", qty=48,
    )
    return ncrs, demo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--today", default=date.today().isoformat())
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default=str(ROOT / "data"))
    args = parser.parse_args()

    today = date.fromisoformat(args.today)
    rng = random.Random(args.seed + 1)
    ncrs, demo = build(today, args.seed)

    out = Path(args.out)
    seed_dir, demo_dir = out / "seed" / "ncr", out / "demo"
    for d in (seed_dir, demo_dir):
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.txt"):
            old.unlink()

    for n in ncrs:
        (seed_dir / f"{n.ncr_id}.txt").write_text(render(n, rng, today))
    (demo_dir / f"{demo.ncr_id}.txt").write_text(render(demo, rng, today))

    with open(out / "seed" / "suppliers.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["supplier_id", "supplier_name", "commodity", "tier", "location",
                         "approval_status", "annual_spend_band"])
        writer.writerows(SUPPLIERS)

    # The detector's "recent window" is relative to this date; load_data.py passes it.
    (out / "seed" / "manifest.json").write_text(json.dumps(
        {"as_of": today.isoformat(), "seed": args.seed, "ncr_count": len(ncrs),
         "demo_ncr": demo.ncr_id}, indent=2) + "\n")

    print(f"Wrote {len(ncrs)} seed NCRs to {seed_dir}")
    print(f"Wrote demo NCR {demo.ncr_id} ({SUPPLIER[demo.supplier_id][1]}) to {demo_dir}")


if __name__ == "__main__":
    main()
