"""Summarize a tape-test run (sanity checks on sign conventions and coverage)."""
import csv, glob, sys
root = sys.argv[1]
for f in sorted(glob.glob(f"{root}/*/tape/*.csv")):
    tok = f.split("/")[-1][:-4]
    for r in csv.DictReader(open(f)):
        print(f"{tok:5} {r['venue']:12} {r['bucket']:9} n={r['n']:>5} ${float(r['notional_usd'])/1e6:7.2f}M "
              f"cost med {r['cost_med_bps']:>7} p90 {r['cost_p90_bps']:>7} | imp10 {r['imp10_med_bps']:>7} imp60 {r['imp60_med_bps']:>7} mo60 {r['mo60_med_bps']:>7}")
neg = tot = 0
for f in glob.glob(f"{root}/*/tape_large/*.csv"):
    for r in csv.DictReader(open(f)):
        tot += 1; neg += float(r["cost_bps"]) < -0.5
print(f"large parents: {tot}, with cost < -0.5 bps: {neg}")
for f in sorted(glob.glob(f"{root}/*/tape_large/*.csv"))[:3]:
    print(f); print("".join(open(f).readlines()[:4]))
