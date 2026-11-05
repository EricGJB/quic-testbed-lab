"""Plot V2 paired QoE, short service budgets, and a representative flight."""

import argparse
import csv
import gzip
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def read(path):
    with path.open() as handle:
        return list(csv.DictReader(handle))


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=155, facecolor="white")
    plt.close(fig)


def qoe(root, primary):
    ordered = ("steady", "steady_reno", "steady_cap8", "rate10_high", "rate10_low", "rate50_high",
               "rate50_low", "rate100_high", "rate100_low", "rate50_matched", "rate50_opposite",
               "rate50_reno", "delay10", "delay50", "ack_variation", "loss1", "urban", "congested")
    fig, (lat, sla) = plt.subplots(1,2, figsize=(11.5,7.4), sharey=True,
                                  gridspec_kw={"width_ratios":[2.2,1]})
    for idx,case in enumerate(ordered):
        if case not in primary:
            continue
        row = primary[case]
        if row["ttft_ms_p50"]:
            median, tail = float(row["ttft_ms_p50"])/1000,float(row["ttft_ms_p95"])/1000
            lat.hlines(idx,median,tail,color="#849baa",lw=2)
            lat.scatter(median,idx,color="#176287",s=30,zorder=3)
            lat.scatter(tail,idx,facecolors="white",edgecolors="#176287",s=27,zorder=3)
        else:
            lat.text(.035,idx,"no visible answer",transform=lat.get_yaxis_transform(),
                     va="center",fontsize=9,color="#8b4a41")
        sla.barh(idx,int(row["correct_within_2000ms"]),height=.54,color="#278878")
        sla.text(12.3,idx,row["visible"]+"/"+row["n"],va="center",fontsize=8)
    lat.set(yticks=list(range(len(ordered))),yticklabels=ordered,xlabel="TTFT among visible answers (s)",
            title="TTFT median and P95")
    lat.scatter([],[],color="#176287",label="Median")
    lat.scatter([],[],facecolors="white",edgecolors="#176287",label="P95")
    lat.legend(loc="upper right",fontsize=8)
    lat.invert_yaxis()
    sla.set(xlim=(0,14),xticks=(0,3,6,9,12),xlabel="Correct within 2 s (out of 12)",
            title="QoE deadline and visible count")
    for ax in (lat,sla):
        ax.grid(axis="x",alpha=.25)
    save(fig,root/"qoe.png")


def short_service(root, primary, repeat):
    profiles = ("steady","rate10_high","rate10_low","rate50_high","rate50_low","rate100_high","rate100_low",
                "rate50_matched","rate50_opposite","grant_replay","grant_inverse")
    rows = [primary.get(case,repeat.get(case)) for case in profiles]
    fig, (rate, latency) = plt.subplots(1,2,figsize=(12,5.6),sharey=True)
    for index,row in enumerate(rows):
        if not row:
            continue
        if row["service_50ms_mbps_p50"]:
            rate.barh(index-.16,float(row["service_50ms_mbps_p50"]),height=.28,color="#9bb7c4",
                      label="Configured service" if index==0 else None)
        if row["upt_50ms_mbps_p50"]:
            rate.barh(index+.16,float(row["upt_50ms_mbps_p50"]),height=.28,color="#176287",
                      label="Delivered stream bytes" if index==0 else None)
        latency.barh(index,float(row["upload_ms_p50"]),height=.5,color="#268778")
    rate.set(yticks=list(range(len(profiles))),yticklabels=profiles,xlabel="Mbit/s",
             title="First 50 ms, service versus actual UPT")
    rate.invert_yaxis()
    rate.legend(fontsize=8)
    latency.set(xlabel="Milliseconds",title="Upload completion, median")
    for ax in (rate,latency):
        ax.grid(axis="x",alpha=.2)
    save(fig,root/"short-service.png")


def trace(root):
    with gzip.open(root/"traffic_trace.jsonl.gz","rt") as source:
        chosen = {row["run"]:row for row in map(json.loads,source) if row["run"] in
                  ("rate50_high__01_1","rate50_low__01_1","rate50_matched__01_1","rate50_opposite__01_1")}
    fig, axes = plt.subplots(2,2,figsize=(11,7),sharex=True)
    for panel,(name,row) in zip(axes.flat,sorted(chosen.items())):
        samples=row["samples"]
        panel.plot([s[0] for s in samples],[s[1]/1024 for s in samples],label="cwnd",color="#176287")
        panel.plot([s[0] for s in samples],[s[2]/1024 for s in samples],label="in flight",color="#b45928")
        panel.plot([p[0] for p in row["arrivals"]],[p[1]/1024 for p in row["arrivals"]],
                   label="delivered",color="#268778")
        for _,_,commit,kind,rate in row["switches"]:
            if kind=="rate":
                panel.axvline(commit,color="#c1a157" if rate==14 else "#b7c7cf",alpha=.75)
        panel.set(title=name.split("__")[0],xlabel="Milliseconds from submit",ylabel="KiB")
        panel.grid(alpha=.2)
    axes[0,0].legend(fontsize=8)
    fig.suptitle("Same video question. Vertical lines mark applied capacity transitions.")
    save(fig,root/"trace-example.png")


def pacing(root):
    names=("steady__01_1","steady_cap8__01_1","rate50_matched__01_1","rate50_opposite__01_1")
    with gzip.open(root/"traffic_trace.jsonl.gz","rt") as source:
        lookup={row["run"]:row for row in map(json.loads,source) if row["run"] in names}
    fig,axes=plt.subplots(2,2,figsize=(11,6.5),sharex=True,sharey=True)
    for ax,name in zip(axes.flat,names):
        row=lookup[name]
        end=row["arrivals"][-1][0]
        samples=[s for s in row["samples"] if s[4]]
        ax.plot([s[0] for s in samples],[1200*8/1e6/s[4] for s in samples],color="#176287",
                label="Native pacer target")
        slots=[(0,14 if row["switches"] else 8)]
        slots.extend((s[2],s[4]) for s in row["switches"] if s[2]<end)
        slots.append((end,slots[-1][1]))
        ax.step(*zip(*slots),where="post",color="#c18930",label="Applied network rate")
        if "cap8" in name:
            ax.hlines(8,0,end,color="#278878",linestyles=":",label="Sender cap")
        if "matched" in name or "opposite" in name:
            phase=[14,2] if "matched" in name else [2,14]
            cap=[(i*50,phase[i%2]) for i in range(int(end//50)+1)]
            cap.append((end,cap[-1][1]))
            ax.step(*zip(*cap),where="post",linestyle=":",color="#278878",label="Sender cap")
        ax.set(title=name.split("__")[0],xlabel="Milliseconds from submit",ylabel="Mbit/s")
        ax.grid(alpha=.25)
    axes[0,1].legend(fontsize=8)
    fig.suptitle("Native pacing target, actual network rate changes and optional cap")
    save(fig,root/"pacing.png")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results",type=Path)
    args = parser.parse_args()
    root=args.results
    primary={r["case"]:r for r in read(root/"profiles.csv")}
    repeat={r["case"]:r for r in read(root/"repeat/profiles.csv")}
    qoe(root,primary)
    short_service(root,primary,repeat)
    trace(root)
    pacing(root)


if __name__=="__main__":
    main()
