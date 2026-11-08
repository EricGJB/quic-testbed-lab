"""Plot the evidence used in the V3 report."""

import argparse
import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path
from statistics import median

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

COLORS = ["#657186", "#968153", "#147b8e", "#b65e43", "#78498e"]


def style(ax, title, ylabel):
    ax.set_title(title, loc="left", fontweight="bold")
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=.23)
    ax.spines[["right","top"]].set_visible(False)


def figure(out, name, fig):
    fig.savefig(out/(name+".png"),dpi=160,bbox_inches="tight",facecolor="white")
    plt.close(fig)


def plot_a(out, runs, long, summary):
    names = ["random", "equal20", "gold", "slow"]
    labels = ["10/30\n5–10 ms", "Flat\n20", "28/30\n5–10 ms", "10/30\n120–180 ms"]
    active = [[r for r in runs if r["case"] == "A1load_"+case] for case in names]
    fig, ax = plt.subplots(2,2,figsize=(11,7),layout="constrained")
    for a, field, heading, unit in ((ax[0,0],"pace","A. Pacing target, saturated replay","Mbit/s"),
                                   (ax[0,1],"mismatch","B. |Pacing target − capacity|","Mbit/s")):
        for i,rows in enumerate(active):
            if not rows:
                continue
            lo,p50,hi = (median(r[field][j] for r in rows) for j in (0,1,2))
            a.errorbar(i,p50,yerr=[[p50-lo],[hi-p50]],fmt="o",color=COLORS[i],capsize=6)
        a.set_xticks(range(len(names)),labels)
        style(a,heading,unit)
    for case,c in zip(names,COLORS):
        xs = [r for r in long if r["case"] == "A1load_"+case and r["repeat"] == "0"]
        if xs:
            x = [float(r["time_ms"])/1000 for r in xs]
            y = [float(r["btlbw_mbps"]) for r in xs]
            ax[1,0].plot(x,y,color=c,alpha=.85,label=case,linewidth=1)
    style(ax[1,0],"C. BBR bandwidth estimate, example run","Mbit/s")
    ax[1,0].set_xlabel("Time from experiment origin (s)")
    ax[1,0].legend(fontsize=8,ncol=2)
    periodic = [summary["A1_"+case,"all"] for case in names[:3]]
    bars = ax[1,1].bar(range(3),[100*int(r["on_time"])/int(r["tasks"]) for r in periodic],color=COLORS[:3])
    ax[1,1].bar_label(bars,fmt="%.1f%%")
    ax[1,1].set_xticks(range(3),labels[:3]); ax[1,1].set_ylim(0,113)
    style(ax[1,1],"D. 40 kB every 20 ms, upload by deadline","% of tasks")
    tail = ax[1,1].twinx()
    tail.plot(range(3),[float(r["token_p99"]) for r in periodic],"k:o",label="Receipt P99")
    tail.set_ylabel("Receipt P99 (ms)")
    tail.set_ylim(0,32)
    tail.spines["top"].set_visible(False)
    tail.legend(loc="lower right",fontsize=8)
    figure(out,"A1",fig)


def aggregate(curves, case, size, field):
    by_ms = defaultdict(list)
    for r in curves[case,size]:
        by_ms[int(float(r["ms"]))].append(float(r[field]))
    return [(t,median(v)) for t,v in sorted(by_ms.items())]


def plot_b(out, curves, summary):
    long_rtt = ("B1_rtt_fast", "200000") in summary
    fig, ax = plt.subplots(3 if long_rtt else 2,2,figsize=(11,9 if long_rtt else 7),layout="constrained")
    names = ["fixed","variable","elastic","fast"]
    for idx,name in enumerate(names):
        a = ax[0,0]
        summary_rows = [summary["B1_"+name,str(size)] for size in (40000,200000,1000000)]
        a.bar([i+(idx-1.5)*.18 for i in range(3)],
              [100*int(r["relaxed_on_time"])/int(r["tasks"]) for r in summary_rows],.17,
              color=COLORS[idx],label=name)
    ax[0,0].set_xticks(range(3),["40 kB", "200 kB", "1 MB"])
    ax[0,0].legend(fontsize=8,ncol=2); ax[0,0].set_ylim(0,113)
    style(ax[0,0],"A. Upload by adjusted 24 / 70 / 310 ms budgets","% of tasks")
    for a,field,unit,title,scale in ((ax[0,1],"pacing_mbps","Mbit/s","B. Pacing target, 200 kB",1),
        (ax[1,0],"cwnd_bytes","kB","C. Congestion window, 200 kB",.001),
        (ax[1,1],"queue","kB","D. Router backlog, 200 kB",.001)):
        for idx,name in enumerate(names[2:],2):
            pts=aggregate(curves,"B1_"+name,200000,field)
            a.plot([t for t,v in pts if t<=30],[v*scale for t,v in pts if t<=30],color=COLORS[idx],label=name)
        if field == "pacing_mbps": a.axhline(30,color="black",linestyle=":",label="30 Mbit/s grant")
        a.legend(fontsize=8); a.set_xlabel("Time since task submitted (ms)")
        style(a,title,unit)
    if long_rtt:
        for idx, name in enumerate(("B1_rtt_elastic", "B1_rtt_fast"),2):
            pts = aggregate(curves,name,200000,"cwnd_bytes")
            ax[2,0].plot([t for t,v in pts],[v*.001 for t,v in pts],color=COLORS[idx],label=name[7:])
            row = [summary[name,str(size)] for size in (40000,200000,1000000)]
            p50=[float(r["upload_p50"]) for r in row]
            p99=[float(r["upload_p99"]) for r in row]
            ax[2,1].bar([i+(idx-2.5)*.28 for i in range(3)],p50,.25,color=COLORS[idx],label=name[7:],
                        yerr=[[0]*3,[b-a for a,b in zip(p50,p99)]],capsize=4)
        ax[2,0].legend(fontsize=8); ax[2,0].set_xlabel("Time since task submitted (ms)")
        ax[2,1].legend(fontsize=8); ax[2,1].set_xticks(range(3),["40 kB","200 kB","1 MB"])
        style(ax[2,0],"E. Window response at 12 ms base RTT, 200 kB","kB")
        style(ax[2,1],"F. Upload completion at 12 ms base RTT","P50 with P99 cap (ms)")
    figure(out,"B1",fig)


def plot_c(out, summary):
    fig, ax = plt.subplots(2,2,figsize=(11,7),layout="constrained")
    ax = ax.flatten()
    names = ["reactive","oracle","80pct"]
    for cap,offset in ((10,-.16),(30,.16)):
        rows = [summary["C1_"+case,str(cap)] for case in names]
        x=[i+offset for i in range(3)]
        ax[0].bar(x,[int(r["on_time"]) for r in rows],.29,color=COLORS[0 if cap==10 else 2],label=f"{cap} Mbit/s")
        bars=ax[1].bar(x,[int(r["correct20"]) for r in rows],.29,color=COLORS[0 if cap==10 else 2])
        ax[1].bar_label(bars,fontsize=8)
    for a,title in zip(ax[:2],["A. Upload completed within 20 ms","B. On-time uploads with correct local model answer"]):
        a.set_xticks(range(3),["Reactive","Oracle","80% predictor"]); a.set_ylim(0,82)
        style(a,title,"Tasks (72 per capacity and policy)")
    ax[0].legend(fontsize=8)
    low = [summary["C1_"+name,"10"] for name in names]
    for a,field,title,unit,scale in ((ax[2],"upload_p50","C. Upload at 10 Mbit/s","Median (ms)",1),
            (ax[3],"queue20_p50","D. Peak queue at 10 Mbit/s","Median (kB)",.001)):
        bars=a.bar(range(3),[float(r[field])*scale for r in low],color=COLORS[:3])
        a.bar_label(bars,fmt="%.1f",padding=2); a.margins(y=.2)
        a.set_xticks(range(3),["Reactive","Oracle","80% predictor"])
        style(a,title,unit)
    ax[2].axhline(20,color="black",linestyle=":")
    figure(out,"C1",fig)


def plot_d(out, curves, summary):
    fig, ax = plt.subplots(3,2,figsize=(11,9),layout="constrained")
    names=["native","high","pattern","low","equal"]
    for a,field,unit,title,scale in ((ax[0,0],"pacing_mbps","Mbit/s","A. Pacing target, 50 kB",1),
        (ax[0,1],"tx","Mbit/s","B. Actual offered IP rate, 50 kB (3 ms mean)",.008),
        (ax[1,0],"queue","kB","C. Router backlog, 50 kB",.001),
        (ax[2,0],"cwnd_bytes","kB","E. Congestion window, 50 kB",.001),
        (ax[2,1],"rx","Mbit/s","F. Actual egress IP rate, 50 kB (3 ms mean)",.008)):
        for idx,name in enumerate(names):
            pts=aggregate(curves,"D1_"+name,50000,field)
            if field in ("tx","rx"):
                pts=[(t,sum(v for _,v in pts[i:i+3])/3) for i,(t,_) in enumerate(pts[:-2])]
            a.step([t for t,v in pts],[v*scale for t,v in pts],where="post",color=COLORS[idx],label=name,linewidth=1.4)
        if field in ("pacing_mbps","tx","rx"):
            a.step([0,10,32],[30,15,15],where="post",color="black",linestyle=":",label="capacity")
        a.axvline(20,linestyle="--",color="gray",alpha=.65); a.set_xlim(0,32)
        a.set_xlabel("Time since task submitted (ms)")
        style(a,title,unit)
    ax[0,0].legend(fontsize=7,ncol=2)
    a=ax[1,1]
    for i,name in enumerate(names):
        row=summary["D1_"+name,"50000"]
        a.plot([float(row["upload_p50"]),float(row["upload_p95"])],[i,i],color=COLORS[i],marker="o")
    a.set_yticks(range(len(names)),names); a.axvline(20,color="gray",linestyle="--")
    a.set_xlabel("Upload completion (ms)")
    style(a,"D. Completion P50–P95, 50 kB","")
    figure(out,"D1",fig)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("out",type=Path)
    out=p.parse_args().out
    def rows(name):
        with (out/name).open() as file:
            return list(csv.DictReader(file))
    summary={(r["case"],r["group"]):r for r in rows("task-summary.csv")}
    runs=json.loads((out/"runs.json").read_text())
    curves=defaultdict(list)
    with gzip.open(out/"BD-curves.csv.gz","rt") as handle:
        for r in csv.DictReader(handle):
            curves[r["case"],int(r["size"])].append(r)
    plot_a(out,runs,rows("A1-trace.csv"),summary)
    plot_b(out,curves,summary)
    plot_c(out,summary)
    plot_d(out,curves,summary)


if __name__ == "__main__":
    main()
